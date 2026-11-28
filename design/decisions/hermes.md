# Hermes Agent

**Read before editing:** `kubernetes/apps/hermes/`, `kubernetes/apps/agent-sandbox/`,
`kubernetes/images/hermes-sandbox/`, `.github/workflows/hermes-sandbox.yml`

**Contents:** Current state · Owner setup · Rules · Verify

## Current state

[Hermes Agent](https://hermes-agent.nousresearch.com) (Nous Research) runs as a Telegram
gateway in namespace `hermes`. The owner talks to it on a **dedicated** bot (not the
alerts bot). It keeps memory, runs cron jobs, and writes code in **Gitea repos only**,
by pushing branches and opening PRs as the `hermes` user. The owner merges every PR. It
has no GitHub credentials, so it cannot push to this repo (`lucid-void/homelab`) at all.
GitHub access was designed (a machine-account PAT, a `main` ruleset and a domain tripwire
workflow, see the spec) and dropped before deploy.

Three pods, split so that the one running commands holds nothing worth stealing:

| Pod | Holds | Reaches |
|---|---|---|
| `hermes` (gateway, app-template) | Telegram token, LiteLLM key, Gitea token, sandbox SSH client key | DNS, LiteLLM `:4000`, gitea-mcp `:8080`, sandbox `:2222`, `api.telegram.org:443` |
| `hermes-sandbox` (`Sandbox` CR, agent-sandbox) | `hermes` Gitea token (git credential store), SSH host key | DNS, Gitea `:3000`, HTTPS by FQDN to public download hosts: GitHub, PyPI, npm, Go proxy, mise/toolchain hosts |
| `gitea-mcp` (app-template) | nothing — HTTP mode passes the caller's bearer through to Gitea | DNS, Gitea `:3000` |

- **Model:** `model.provider: custom` → LiteLLM `/v1`, model name **`hermes`**, an alias
  the owner points at an external model in LiteLLM's config. Hermes's network policy
  never reaches a provider directly. Hermes cannot read a context window for an alias and
  falls back to 256,000 (measured); set `model.context_length` once the model is chosen.
  Hermes refuses any model under 64,000 (`MINIMUM_CONTEXT_LENGTH`, enforced at startup),
  which is why the local CPU Qwen at 32K cannot drive it.
- **Terminal:** `terminal.backend: ssh` to `hermes-sandbox.hermes.svc:2222` (the
  `Sandbox`'s headless Service). Shell commands, file tools **and `execute_code` scripts**
  all run there (checked in v0.23.4's `code_execution_tool.py`). The sandbox image is
  Debian slim with a **non-root sshd** on 2222 (key-only, `AllowUsers agent`, no
  forwarding), git, mise, Python, Node; read-only root, all capabilities dropped.
- **Approvals:** `mode: manual`, `approvers: []` (requester + admins; the allowlist admits
  only the owner), `cron_mode: approve` (owner: a job they scheduled is pre-approved),
  plus an `approvals.deny` glob list that applies in every mode. Memory writes are
  automatic; **skill** writes need approval.
- **Config is rendered from git on every start:** the `config` initContainer installs
  `config/config.yaml` into `/opt/data`, so `/model`,
  "Always Approve" and `hermes config set` are lost on restart — edit the file in git.
- **State:** `hermes-data` PVC (`nfs-client`) at `/opt/data`: memories, `state.db`
  (journal mode pinned `delete` for NFS), sessions, cron jobs, skills.
- **Server-side gates (configured by hand, the real enforcement):** the `hermes` Gitea
  user is a non-admin SSO account, a *Write* collaborator only on the repos chosen for it,
  and branch protection on each one's `main` keeps it out of `main`.

## Owner setup

Done by hand, once, before the first deploy. Everything sealed goes through
`kubernetes/apps/hermes/seal-secrets.sh`, which prompts with hidden input.

1. **Telegram:** create a new bot with @BotFather; note your numeric user id.
2. **LiteLLM:** add a `model_name: hermes` entry to
   `kubernetes/apps/ai/litellm/app/config-configmap.yml` pointing at the external model
   (provider key sealed into `litellm-secret`), then mint a virtual key allowed only
   `hermes`: `POST /key/generate` with `{"models": ["hermes"], "key_alias": "hermes"}`.
3. **Gitea, through Keycloak:** the `hermes` Gitea account is an SSO account like every
   other (Gitea allows only external registration).
   1. Keycloak `homelab` realm → Users → Add user: username **`hermes`** (fixed in
      `seal-secrets.sh`), an email address you own that no other Gitea account uses,
      *Email verified* on. Credentials → set a long password, *Temporary* off; keep it
      in your password manager. Hermes never gets this password.
   2. Groups → join **`/gitea/user` only**. Not `/gitea/admin` (the admin flag follows
      `gitea_roles` at each login), and no other group: every other client's
      browser-flow gate then refuses it.
   3. In a private window, sign in to Gitea with Keycloak as `hermes`. If Keycloak asks
      for OTP setup, do it and store the seed with the password. If Gitea offers a
      registration form, keep the username `hermes`.
   4. As `hermes`: Settings → Applications → generate a token with
      `write:repository`, `write:issue`, `read:user`. That token is `GITEA_TOKEN`.
   5. As yourself: add `hermes` as a *Write* collaborator on the chosen repos only, and
      protect `main` on each (no direct push, PR required, 1 approval, `hermes` not in
      the push/merge whitelist).
4. **Seal:** `kubernetes/apps/hermes/seal-secrets.sh`, commit both `*-sealed.yml`.
5. **Image:** push `.github/workflows/hermes-sandbox.yml` yourself (agent sessions have
   no `workflow` scope), run the image build, and make the
   `ghcr.io/lucid-void/hermes-sandbox` package **public** (new GHCR packages start
   private and the cluster has no pull secret).

## Rules

- **Never give the sandbox anything from the Hermes pod's secret** — the split is the
  whole design. The sandbox may hold only credentials whose reach is capped server-side.
- **Never set `terminal.sync_back_max_bytes` above 0** — Hermes pushes its skills into
  the sandbox and by default syncs changed files back into `/opt/data`; that would let
  anything running in the sandbox rewrite a skill the agent then follows. At 0 every
  sync-back logs `cap 0 … skipping extraction` and applies nothing.
- **Never add `postBuild.substituteFrom` to the `hermes` Kustomization** — `config.yaml`
  carries `${GITEA_TOKEN}`-style references that Hermes expands itself; Flux would
  substitute them with empty strings.
- **Never use the Docker terminal backend** — Talos has no Docker socket, a DinD sidecar
  needs `privileged`, and Hermes skips approvals on every container backend.
- **Never give gitea-mcp a token** — HTTP mode is credential passthrough; a static
  `GITEA_ACCESS_TOKEN` would let anything that reaches the pod act as that user.
- **`GITEA_TOOLS` is the tool allowlist, enforced server-side** — add a tool there, not in
  Hermes. `pull_request_write` includes `merge`; branch protection is what stops it.
- **The Hermes image must start as root** — s6-overlay remaps and chowns `/opt/data`,
  then runs the gateway as uid 10000. Starting as 10000 or any other uid fails in
  `stage2-hook.sh`. The capability set in the HelmRelease is the measured minimum
  (`CHOWN DAC_OVERRIDE FOWNER SETUID SETGID KILL`, `allowPrivilegeEscalation: false`).
- **A `Sandbox` podTemplate change does not roll the pod** — after bumping the sandbox
  image, delete the pod (`kubectl -n hermes delete pod hermes-sandbox`); the controller
  recreates it and the `home` PVC survives.
- **On a Hermes image bump, copy `_config_version`** — boot the new image once, read
  `_config_version` from `/opt/data/config.yaml`, put it in `config/config.yaml`; otherwise
  Hermes migrates (rewrites) the rendered file on every boot.
- **Rotating the sandbox host key breaks the gateway's `known_hosts`** — Hermes uses
  `StrictHostKeyChecking=accept-new` with `~/.ssh/known_hosts` on `hermes-data`. After
  re-running `seal-secrets.sh`, delete `/opt/data/.ssh/known_hosts` in the gateway pod.
- **"Unrelated to the task" is not enforced for cron jobs** — Hermes's `cron_mode` is
  binary. The stand-ins are: the deny list, each job's prompt stating its scope, every
  job delivering to Telegram, and the network and branch limits. A job reading
  untrusted input (feeds, issues, pages) can be prompt-injected into an auto-approved
  command inside the sandbox; a `pre_tool_call` plugin is the escalation if that bites.
- **A new download host for the sandbox goes into its CiliumNetworkPolicy** — not
  `toEntities: world`. `toFQDNs` needs the `dns` rule with `matchPattern: "*"` on the
  DNS egress entry, or every FQDN rule silently matches nothing.
- **gitea-mcp needs `command: ["/app/gitea-mcp"]`** — the image sets only `CMD`, no
  `ENTRYPOINT`, so `args` alone replace the binary and the pod fails with
  `exec: "-t": executable file not found`.
- **Hermes parks an MCP server it cannot reach at boot** — `failed initial connection
  after 3 attempts, parking` in `/opt/data/logs/errors.log`, and the Gitea tools stay
  missing until the gateway restarts (`kubectl -n hermes rollout restart deploy/hermes`).
  Expect it on a cold start, when gitea-mcp comes up after Hermes.
- **`TERMINAL_CWD does not exist: /home/agent` in the gateway log is harmless** — Hermes
  checks the path on its own filesystem; with the SSH backend commands still run in
  `/home/agent` on the sandbox.
- **Never give the sandbox a GitHub token** — this repo is public and the domain is
  private; one pushed commit containing it is public at once. Re-adding GitHub means the
  spec's machine account, `main` ruleset and domain tripwire together, not a PAT alone.

## Verify

```sh
# gateway connected and its policy loaded
kubectl -n hermes logs deploy/hermes | grep -E "telegram|Gateway"
kubectl -n hermes get cnp
# the sandbox: Ready, and no route to the API server, the NAS or the internet at large
kubectl -n hermes get sandbox hermes-sandbox
kubectl -n hermes exec hermes-sandbox -- sh -c 'curl -sm5 https://kubernetes.default.svc || echo blocked'
kubectl -n hermes exec hermes-sandbox -- sh -c 'curl -sm5 http://172.16.20.2 || echo blocked'
kubectl -n hermes exec hermes-sandbox -- sh -c 'curl -sm5 https://example.com || echo blocked'
kubectl -n hermes exec hermes-sandbox -- sh -c 'git ls-remote http://gitea-http.gitea.svc.cluster.local:3000/<owner>/<repo>.git HEAD'
```

In Telegram: a plain question gets an answer; a dangerous command (`rm -rf ~/x`) asks
for approval and `no` denies it; asking it to push to a protected `main` in Gitea fails.
