# Hermes Agent gateway — design

Date: 2026-11-27. Status: approved; built. The implemented state lives in
`design/decisions/hermes.md` — this spec is history.

**Changed at build time:**
- **External models, not the local LLM** (owner). The 64K context gate and the
  `llama-swap` prerequisite are dropped. Hermes reaches the model through a LiteLLM
  alias `hermes`, so the gateway's network policy still has no route to any provider.
- **Own namespace `hermes`, not `ai`** (builder's call), so its network policies and RBAC
  are separate from the LLM stack.
- **gitea-mcp holds no token** (found in its docs): HTTP mode passes the caller's bearer
  through, and `GITEA_TOOLS` gives a server-side tool allowlist.
- **Sync-back disabled** (found in Hermes's source): `terminal.sync_back_max_bytes: 0`
  stops the sandbox writing changed skill files back into the gateway.

## Intent

A personal, always-on agent ([Hermes Agent](https://hermes-agent.nousresearch.com), Nous
Research, MIT) reached over Telegram, running in the cluster against the local LLM. It
keeps memory between conversations, runs scheduled jobs, and **writes code**: it clones
Gitea repos and the GitHub `Homelab` repo, pushes branches, and opens PRs. The owner
reviews and merges every PR; for `Homelab`, merge to `main` is what Flux deploys, so the
owner's approval is the only path from agent output to the cluster.

The agent's shell is the dangerous part. It must be locked down so that a prompt
injection (from a web page, an issue body, a file in a repo) cannot read the agent's own
credentials, change the agent's own config, reach the Kubernetes API or the LAN, or get
code onto `main` without the owner.

## Goals

- Telegram chat with the agent, owner-only.
- Agent memory (built-in) and cron jobs delivering to Telegram.
- Agent can clone, edit, test, push `hermes/*` branches and open PRs on: chosen Gitea
  repos, and the GitHub `Homelab` repo.
- The agent's shell runs in a separate pod that holds no Hermes secrets, has no
  Kubernetes credentials, and has a narrow egress allowlist.
- Everything declarative through Flux, like every other app.

## Non-goals

- No cloud LLM, no cloud sandbox (Modal, Daytona, Vercel), no cloud memory provider.
- No browser tool, no web dashboard/HTTPRoute in v1 (Telegram is the only interface).
- No per-conversation ephemeral sandboxes (option D2 below) in v1.
- No external memory provider (Hindsight, Mem0) in v1.

## Anti-goals (failure even if it "works")

- The agent can merge, or push to `main`, on any repo.
- The agent can modify `.github/workflows/` in `Homelab` (CI has repo secrets).
- The real cluster domain appears in anything the agent can read or write. The repo is
  public and the domain is private; the sandbox never receives `CLUSTER_DOMAIN`.
- The sandbox can read the Telegram bot token, the LiteLLM key, or the gitea-mcp token.
- The agent's PRs are authored by the owner's own GitHub account (then "require one
  approval" cannot be satisfied honestly and the gate becomes habit, not enforcement).
- Hermes starts against a context window it was told is bigger than the server serves
  (the server truncates silently).

## Constraints found during design

1. **Hermes refuses models under 64K context.** `MINIMUM_CONTEXT_LENGTH = 64_000` in
   `agent/model_metadata.py` (v0.23.4); `agent_init.py` raises at startup when the served
   window is smaller. `llama-swap` serves Qwen at `--ctx-size 32768`. Setting
   `model.context_length: 64000` in Hermes while the server serves 32K is an anti-goal
   (silent truncation). **Prerequisite PR: raise `--ctx-size` to `65536`**, following the
   `llm.md` rule — boot once, read the `KV self size` line, confirm it fits inside the
   46Gi request next to Clef. The live log already shows `n_slots = 4` and repeated
   `failed to find free space in the KV cache` warnings, so the builder must also decide
   `--parallel` deliberately (per-slot window must be ≥64K for Hermes; four slots at 64K
   each is likely too much). Client docs/configs that declare `context: 32768` (e.g.
   opencode) get updated in the same PR.
2. **Slow model.** ~8.5 tok/s decode on `local-fast`. Every extra LLM pass Hermes makes
   (background memory review, smart approvals, subagents) queues on the same process.
3. **Telegram: one poller per bot token.** The existing bot (`telegram-secret`, owned by
   `gotify-telegram`) long-polls; Hermes needs its own bot from BotFather.
4. **Talos has no Docker socket**, so Hermes's `docker` terminal backend would need a
   privileged DinD sidecar (breaks PSA `baseline`). Hermes also skips approvals on all
   container backends.
5. **Session cannot push workflow files** (no `workflow` scope) — the owner pushes the
   sandbox image workflow, or it is added by hand.

## Approaches considered

Terminal lockdown:

- **A. Local backend, the Hermes pod is the sandbox.** One pod. Rejected: the shell
  shares the filesystem and uid with Hermes, so it can read `/opt/data/.env` and
  `/proc/<pid>/environ`, and rewrite its own `config.yaml`/skills.
- **B. SSH backend to a hand-rolled sandbox Deployment.** Good separation; superseded by D1.
- **C. A or B under gVisor (`runtimeClassName`).** Needs a Talos system extension and node
  upgrades; kept as a later hardening layer on top of D1, not in v1.
- **D1. `agent-sandbox` `Sandbox` CRD + Hermes's built-in SSH backend. ← chosen.** No
  custom code; stable hostname, PVC, lifecycle from the controller; Hermes keeps
  approvals (SSH is a backend that runs them); Hermes itself needs no Kubernetes RBAC.
- **D2. Custom Hermes terminal plugin claiming a `SandboxClaim` per conversation** via the
  `k8s-agent-sandbox` SDK. Cleanest isolation, but ~150 lines we own on a `v1beta1` API,
  a custom Hermes image or venv, and Hermes gets a Kubernetes token again. Deferred; D1's
  sandbox image and policies carry over if we move to it.
- Rejected outright: `docker` backend (constraint 4), cloud sandboxes (non-goal), terminal
  disabled (owner wants a locked-down terminal, not none).

Git access: **G3, both** — real `git` in the sandbox for code, the official Gitea MCP server
for PRs/issues. Memory: **M1, built-in only** (Hindsight/Mem0 add an LLM pass per turn on
a CPU model).

## Design

### Components (all in namespace `ai`, under `kubernetes/apps/ai/`)

| Component | What | Where it lives |
|---|---|---|
| `agent-sandbox` controller | `kubernetes-sigs/agent-sandbox`, core CRDs only (no extensions needed for D1), pinned release | its own Kustomization; namespace is the builder's call (`agent-sandbox-system` from upstream manifests is fine) |
| `hermes` | `nousresearch/hermes-agent`, pinned `X.Y.Z` (latest stable at writing: v0.23.4 — check the Docker tag format), gateway mode, Telegram | Deployment, 1 replica, `nfs-client` PVC at `/opt/data` |
| `hermes-sandbox` | `Sandbox` object running a custom sshd + dev-tools image | `nfs-client` PVC for `/home/agent` (workspace + clones) |
| `gitea-mcp` | `docker.gitea.com/gitea-mcp-server`, pinned, HTTP mode (`-t http`), `/healthz` probe | Deployment + ClusterIP Service |

Kustomization `dependsOn`: `hermes` depends on `llama-swap` (ctx prerequisite merged),
`litellm`, `gitea-mcp`, and the sandbox. The sandbox depends on the controller.

### Hermes pod

- Runs the image's normal entrypoint (s6 starts as root, drops to uid 10000 `hermes`).
  The builder confirms the minimal `securityContext` that still lets s6 chown the PVC on
  first boot (PSA `baseline` permits it); if `fsGroup`/pre-owned PVC lets it start
  non-root, prefer that. `automountServiceAccountToken: false`.
- Model: `model.provider: custom`, `base_url` = LiteLLM in-cluster service `/v1`, model
  `local-fast`, `api_key` = a **new LiteLLM virtual key** limited to `local-fast` (and
  `local-smart` only if the builder routes approvals/aux there). The key is minted once by
  hand via LiteLLM `/key/generate` and sealed.
- `config.yaml` is rendered from a ConfigMap into `/opt/data/config.yaml` by an
  initContainer on every start, so git is the source of truth (Hermes otherwise rewrites
  its config in place). Secrets come in as env from a SealedSecret (`hermes-sealed.yml`):
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, LiteLLM key, gitea-mcp bearer, SSH
  private key for the sandbox.
- Key config:
  - `terminal.backend: ssh`, `TERMINAL_SSH_HOST` = the Sandbox's stable service name,
    `TERMINAL_SSH_USER: agent`, `TERMINAL_SSH_KEY` path, persistent shell on.
  - `approvals.mode: manual`, `approvals.approvers: [<owner telegram id>]`,
    `approvals.timeout: 300`.
  - `approvals.cron_mode: approve` (owner decision: a job the owner scheduled is
    pre-approved). The owner also wants "unless it's unrelated to the task" — Hermes cannot
    enforce that: `cron_mode` is binary (`deny | approve`, `tools/approval_context.py`
    v0.23.4) and the smart guardian does not run in cron context. So it is held by layers,
    not by Hermes: (1) `approvals.deny` still hard-blocks in cron; (2) each job's prompt is
    written to state its scope and to stop and report instead of acting outside it (soft —
    a model instruction, not enforcement); (3) every job delivers its output to Telegram, so
    the owner sees what ran; (4) network policy and server-side branch rules bound what an
    off-task command can reach. Residual risk, recorded: a job that reads untrusted input
    (feeds, issue bodies, web pages) can be prompt-injected into an unrelated command that
    runs auto-approved inside the sandbox. If that proves real, a later `pre_tool_call`
    plugin can add an LLM relatedness check for cron (out of scope for v1).
  - `approvals.deny`: at least `"kubectl*"`, `"talosctl*"`, `"*169.254.169.254*"`,
    `"git push*--force*"`, `"git push*main*"`, `"rm -rf /*"`. Defence in depth only — the
    real enforcement is server-side (branch rules) and network policy.
  - `unauthorized_dm_behavior: ignore`.
  - `memory.write_approval: false` (owner decision: memory saves are automatic; Hermes
    still scans entries for injection/exfiltration patterns). `skills.write_approval: true`
    (designer's call: a skill is standing instructions the agent follows on every
    matching task, closer to code than to a note).
  - `auxiliary.background_review.enabled: false` (or pointed at `local-fast`; builder's
    call after measuring).
  - Delegation concurrency 1.
  - Toolsets: disable `browser`; keep `terminal`, `file`, `memory`, `cron`, `delegation`,
    MCP. Web search: off in v1 unless a self-hosted search backend is wired (degoog
    exists in the cluster — out of scope here).
  - `mcp_servers.gitea`: `url` = gitea-mcp service `/mcp`, bearer header, `tools.include`
    allowlist: read repo/file/branch/commit, list/create/comment issues, list/create/
    comment PRs. **Exclude** anything that merges, deletes, or touches admin, org,
    webhooks, collaborators, or repo settings. Builder enumerates the real tool names
    from the pinned gitea-mcp version.
  - Repo rules as context: the `Homelab` repo's `AGENTS.md`/`.claude/CLAUDE.md` hard rules
    shipped as a Hermes skill (or `SOUL.md` section) so the agent follows them: sealed
    secrets, `${CLUSTER_DOMAIN}`, no `Ingress`, run `validate-manifests.sh`.
- Port 8642 API server: off in v1 (`API_SERVER_ENABLED` unset). No HTTPRoute.

### Sandbox pod (`Sandbox` CRD)

- Image `ghcr.io/lucid-void/hermes-sandbox`, built by a new workflow modelled on
  `.github/workflows/backup-tools.yml` from `kubernetes/images/hermes-sandbox/`. Contents:
  openssh-server (key-only, no password, no root login, listening on a non-privileged
  port), git, `mise` (so the agent can install `kubeconform`/`kubeseal`… on demand into
  its home), `bash`, `curl`, `jq`, Python, Node. Runs sshd as a non-root user `agent`
  (sshd as non-root on a high port, `UsePrivilegeSeparation` as the image allows).
  The builder decides Debian vs Alpine; if Alpine, the repo's `apk` rules apply at build
  time, not runtime.
- `automountServiceAccountToken: false`, dedicated ServiceAccount with no bindings,
  `runAsNonRoot`, drop all capabilities, `allowPrivilegeEscalation: false`,
  `seccompProfile: RuntimeDefault`. Resource limits set (it shares nodes with real apps);
  scheduled on control-plane nodes, **not** `llm-1`.
- Holds only: the SSH `authorized_keys` (public key), and git credentials:
  - **Gitea:** token for a dedicated `hermes` Gitea user, collaborator (write) only on
    the repos the owner lists. Clones via the in-cluster `gitea-http.gitea:3000` URL, so
    the domain is never needed.
  - **GitHub:** **classic** PAT of a **separate machine account** (owner creates it),
    scope `public_repo` only — **no** `workflow`, no `repo`, no admin scopes. The account
    is a collaborator (write) on `Homelab` and nothing else, so `public_repo` reaches only
    that repo. (A fine-grained PAT cannot target a repo owned by another personal
    account, hence classic — builder's/designer's call, verify when creating it.)
  - Both injected as a git credential helper file from a SealedSecret mounted read-only.
    The agent can read them — that is accepted, because their blast radius is bounded
    server-side (below).
- Never receives: `cluster-secrets`, `CLUSTER_DOMAIN`, kubeconfig, SOPS/age keys, the
  Telegram token, the LiteLLM key, the gitea-mcp token.

### Server-side guarantees (the real enforcement)

- **Gitea:** on each enabled repo, branch protection on `main`: no direct push, require
  PR, require 1 approval from the owner, the `hermes` user is not in the push/merge
  whitelist. Optionally restrict `hermes` pushes to `hermes/*` via protected-branch
  patterns.
- **GitHub `Homelab`:** a ruleset on `main` — require PR, 1 approving review, no bypass
  actors except the owner; block force-push and deletion. Without the `workflow` scope
  GitHub rejects any push that modifies `.github/workflows/`.
- **Domain tripwire (accepted risk, owner decision).** The model *will* see the real
  domain: gitea-mcp responses carry Gitea's `ROOT_URL`, and private Gitea repo content may
  contain it. A `hermes/*` branch on GitHub is public the instant it is pushed, before
  review, and PR refs need GitHub Support to purge. The owner accepts this risk with a
  detector, not a preventer: a workflow `.github/workflows/hermes-domain-tripwire.yml` on
  `push` to `hermes/**` (and `pull_request` from those branches) greps the pushed diff and
  commit messages for the domain, read from a repo Actions secret (`CLUSTER_DOMAIN`), and
  fails loudly — never echoing the match. The agent cannot neuter it: without the
  `workflow` scope it cannot change workflow files, and a push event runs the workflow as
  committed on the pushed ref, which the agent cannot alter. Failure alerts reach the owner
  via GitHub's failed-run notification; the builder may add a Telegram message using the
  existing bot's token as an Actions secret. On a hit the owner deletes the branch and
  follows the domain-leak procedure (`project_domain_private` memory / Support purge). The
  owner pushes this workflow file (session has no `workflow` scope).
- These are configured by hand by the owner (UI), documented in the decision file.

### Network policy (CiliumNetworkPolicy, pattern from `restore-drill`)

- `hermes`: egress DNS; LiteLLM service; gitea-mcp service; sandbox SSH port;
  `api.telegram.org:443` (`toFQDNs`). Ingress: none.
- `gitea-mcp`: ingress only from `hermes`; egress DNS + `gitea-http.gitea:3000`.
- `hermes-sandbox`: ingress only from `hermes` on the SSH port. Egress DNS;
  `gitea-http.gitea:3000`; `toFQDNs` 443 for `github.com`, `api.github.com`,
  `codeload.github.com`, `objects.githubusercontent.com`, PyPI (`pypi.org`,
  `files.pythonhosted.org`), npm (`registry.npmjs.org`), Go proxy (`proxy.golang.org`),
  and the mise/aqua download hosts the builder finds necessary. **No** `world` entity, no
  LAN (`172.16.20.0/24`), no kube-apiserver, no LiteLLM.
- Builder verifies Cilium's DNS proxy is active for `toFQDNs` (not configured anywhere in
  the repo today) and adds the `dns` rule `matchPattern: "*"` that FQDN policies require.

### Telegram

- New bot via BotFather (owner), token sealed. `TELEGRAM_ALLOWED_USERS` = owner's numeric
  id. Polling (no webhook, so no inbound route).

### Memory and state

- Built-in `MEMORY.md`/`USER.md` and the SQLite session store on the `hermes` PVC.
- Backups: both PVCs follow the repo's restic pattern (`backups.md`); the sandbox PVC is
  disposable and may be excluded — builder's call, recorded in the decision file.

### Docs to update

- New `design/decisions/hermes.md` (current state + rules/gotchas), routed from the
  `.claude/CLAUDE.md` "Which file to read" list.
- `design/decisions/llm.md` for the ctx-size change.
- `design/docs/services.md` inventory entry.

## Order of work

1. LLM prerequisite PR: `--ctx-size 65536` (+ `--parallel` decision), measured.
   **Go/no-go gate (owner decision):** if a ≥64K per-slot window only fits at
   `--parallel 1`, or does not fit inside the 46Gi request beside Clef at all, **stop the
   project here** and report back — do not serialise every LLM client behind Hermes, do not
   squeeze Clef, do not add a second model. Revisit with a GPU on `llm-1` or a different
   model. Only proceed to step 2 with ≥2 slots at ≥64K each.
2. agent-sandbox controller.
3. Sandbox image + workflow (owner pushes the workflow file).
4. Owner-side manual steps: BotFather bot, Gitea `hermes` user + token + branch
   protection, GitHub machine account + PAT + ruleset, LiteLLM virtual key.
5. gitea-mcp, sandbox `Sandbox`, Hermes, network policies; sealed secrets.
6. Verify: Telegram reply; approval prompt arrives and deny works; sandbox cannot reach
   `kubernetes.default`, `172.16.20.2`, or `example.com`; agent opens a PR on a test
   Gitea repo and on `Homelab`; push to `main` and to `.github/workflows/` are rejected.

## Left to the builder

- Exact image tags/digests; Hermes Docker tag format.
- `--parallel` value and whether 64K fits (measure, don't guess).
- Hermes pod securityContext details compatible with s6.
- Exact gitea-mcp tool allowlist names.
- Sandbox base distro and the mise download host list.
- Whether the background review runs on `local-fast` or is off.

## Owner decisions recorded

- Terminal: D1 (agent-sandbox `Sandbox` + SSH backend).
- Repos: Gitea (owner picks which) and GitHub `Homelab`.
- Memory: built-in only for v1 (my recommendation, not objected to); writes automatic.
- Cron jobs: auto-approved (`cron_mode: approve`), since the owner scheduled them.
- Separate Telegram bot; locked-down terminal with manual approvals.
- Public-push domain risk: accepted, with the tripwire workflow (option A; staging on a
  private Gitea copy and excluding `Homelab` were offered and declined).
- Context: stop the project if ≥64K only fits at one slot.

## Designer's calls (owner to check)

- Classic `public_repo` PAT rather than fine-grained (fine-grained can't target another
  user's personal repo).
- Hermes `config.yaml` rendered from a ConfigMap on every start, so git wins over
  Hermes's in-place config rewrites.
- Skill writes still need approval (memory writes are automatic, per owner).
- "Unrelated to the task" for cron is held by job-prompt scoping + deny list + Telegram
  delivery + network/branch limits, not by Hermes (it has no such check).
- Sandbox on control-plane nodes, not `llm-1`; sandbox PVC may skip backups.
- No HTTPRoute, API server, browser, or web search in v1.
