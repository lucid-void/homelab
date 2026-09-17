# Agent docs restructure — design

Date: 2026-09-17
Status: approved, not yet implemented

## Problem

`.claude/CLAUDE.md` is 80 KB (~21,540 tokens) and is loaded into every agent
session unconditionally. The `### Kubernetes stack` table alone is 60 KB
(~16,100 tokens), 75% of the file.

The opencode provider config (`~/.config/opencode/opencode.jsonc`) caps the two
local models at `limit.context: 32768`:

```
local-fast   Qwen3.6-35B (fast)    32768
local-smart  Qwen3.6-35B (think)   32768
```

So the always-loaded context is **66% of the local model's window** before the
user types anything. Local models are effectively unusable in this repo, and
frontier models pay ~21.5k tokens per session for knowledge that is relevant to
maybe one task in twenty.

Secondary problems:

- Context lives in `.claude/CLAUDE.md`, a Claude-Code-specific path. opencode
  and Cursor read `AGENTS.md` and see nothing.
- The `i-have-adhd` skill is pasted verbatim into `~/.config/opencode/AGENTS.md`
  (user-machine-local) and is absent from the repo's Claude Code path. A fresh
  clone gets neither.
- `design/` is 601 KB across 21 files with no routing rule, so an agent either
  guesses or reads too much.
- 119 KB of `design/` is proposal and planning narrative, not current state.

## Goals

1. Cut always-loaded context from ~21,540 to ≤2,500 tokens.
2. Make every doc readable by a 32k-context model: one hop, self-contained files.
3. One source of truth that Claude Code, opencode, and Cursor all read.
4. `i-have-adhd` applies by default in Claude Code and opencode, from the repo.
5. `design/` holds current state and load-bearing decisions only.

## Non-goals

- Changing any Kubernetes manifest, Talos config, or CI workflow.
- Adding a build step or generator. Files are hand-maintained.
- Supporting harnesses beyond Claude Code, opencode, and Cursor.

## Architecture

### File layout

```
AGENTS.md                       source of truth, ~150 lines, always loaded
.claude/CLAUDE.md               symlink -> ../AGENTS.md
.claude/skills/                 symlink -> ../.agents/skills/
opencode.json                   NEW, repo-local: skills.paths + instructions
.agents/skills/
  i-have-adhd/SKILL.md          canonical, unchanged
  gitops-*/                     unchanged (vendored from fluxcd/agent-skills)
design/
  README.md                     routing index only
  architecture.md               AI_CONTEXT.md + ARCHITECTURE.md merged, pruned
  runbook.md                    every command kept, prose cut
  docs/
    networking.md  storage.md  secrets.md  gitops.md  services.md
  decisions/
    <24 self-contained topic files>
```

### Why AGENTS.md is the source and CLAUDE.md is a symlink

`AGENTS.md` at repo root is read natively by opencode, Cursor, Codex and Zed.
Claude Code reads `.claude/CLAUDE.md`. A symlink makes both resolve to the same
bytes, so there is exactly one file to maintain and drift is structurally
impossible. Git stores the symlink as a symlink (mode 120000); `core.symlinks`
is default-on for this Linux workstation.

### AGENTS.md contents — the entire always-on budget

Six sections, in this order:

1. **Output shape** — one line: follow `.agents/skills/i-have-adhd/SKILL.md` for
   every response. ~15 tokens always-on; the 1.4k-token skill body loads only
   when the harness's skill loader pulls it.
2. **What this repo is** — 3 lines.
3. **Hard rules** — the ~12 invariants whose violation causes damage or silent
   breakage. Never `Ingress`, never raw `Secret`, never `kubectl apply` for
   config, `nfsvers=4` only, all tools via `mise exec --`, never rolling image
   tags, never Python at 0-indent in a YAML block scalar.
4. **IP map** — 8 lines. Small and referenced constantly, so it earns its place.
5. **Routing table** — ~25 rows, `task touches X → read exactly Y`.
6. **Doc-sync rule** — 3 lines: when IaC changes, update the one design file the
   routing table names.

Budget: ≤2,500 tokens. Measured after writing; if it exceeds, the routing table
rows are the first thing trimmed, never the hard rules.

### Smart loading = the routing table

The loading mechanism is a convention, not tooling: a single table in the
always-loaded file mapping task shape to exactly one file path.

```
| Task touches...                  | Read (only this)                  |
|----------------------------------|-----------------------------------|
| adding or changing a service     | design/docs/gitops.md             |
| backup CronJobs, restic, Gotify  | design/decisions/backups.md       |
| HTTPRoute, DNS, certs, Gateway   | design/docs/networking.md         |
| Postgres, CNPG, DB passwords     | design/decisions/cnpg.md          |
| ...                              |                                   |
```

Two constraints make this work on a small model:

- **One hop.** A routing row names a terminal file, never another index.
- **Self-contained files.** No design file depends on another being read first.
  Where two topics genuinely overlap, the fact is duplicated rather than
  cross-referenced — a duplicated 40-token fact is cheaper than a second file read.

This choice was made over a lookup script and over skill-based routing because
it is the only option that degrades gracefully in Cursor, which has no skill
loader and no way to run a repo script at context-assembly time.

### Vendor wiring

| Harness | Instructions | Skills |
|---|---|---|
| Claude Code 2.1 | `.claude/CLAUDE.md` symlink → `AGENTS.md` | `.claude/skills/` symlink → `.agents/skills/` |
| opencode 1.18.25 | `AGENTS.md` native | `opencode.json` → `skills.paths: [".agents/skills"]` |
| Cursor | `AGENTS.md` native | none; degrades to the routing table |

Verified against the live opencode config schema at `https://opencode.ai/config.json`:
`Config.skills.paths` (array of strings, "Additional paths to skill folders") and
`Config.instructions` (array, "Additional instruction files or patterns") both exist.

Repo-local `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": { "paths": [".agents/skills"] },
  "instructions": ["AGENTS.md"]
}
```

The global `~/.config/opencode/AGENTS.md` (a verbatim copy of the i-have-adhd
skill) is left alone — it is outside this repo. The repo copy makes the behavior
travel with a clone instead of depending on that machine-local file.

### Commands and hooks

`.claude/commands/` (`review-k8s.md`, `update-docs.md`) and
`.claude/hooks/validate-manifests.sh` are Claude-Code-specific and currently
have no opencode equivalent.

- **Commands.** The two command bodies move to `.agents/commands/<name>.md`.
  `.claude/commands/` becomes a symlink to it, and `opencode.json` gains a
  `command` block whose `template` is `{file:.agents/commands/<name>.md}`.
  One body, both harnesses. Cursor gets nothing here and that is accepted.
- **Hooks.** The validation logic moves out of the hook wrapper into
  `.agents/scripts/validate-manifests.sh`, taking a file path as `$1` instead of
  parsing Claude's PostToolUse JSON from stdin. `.claude/hooks/validate-manifests.sh`
  shrinks to a JSON-parsing wrapper that calls it. AGENTS.md's hard rules name
  the script directly, so an agent in any harness can run it after editing a
  manifest even where no hook fires.

No opencode plugin is written. A plugin would duplicate the hook in JavaScript
for one check; naming the script in AGENTS.md achieves the same coverage with
no new runtime.

## Content decisions

### Deleted outright

`git rm` these; git history retains them.

| File | Size | Why |
|---|---|---|
| `design/llm-inference.md` | 36 KB | Proposal for work that shipped; current state now lives in `decisions/llm.md` |
| `design/ups-power.md` | 14 KB | Proposal for work never built |
| `design/monitoring-stabilization.md` | 12 KB | Plan whose outcome is already in the monitoring decisions |

### Rewritten

| File | From | To |
|---|---|---|
| `design/TODO.md` | 57 KB | Short prioritized list of open items. The `Stale / Needs Update`, `Needs Verification` and `Service Candidates` narrative sections are dropped. |
| `design/llm-deployment.md` | 24 KB | → `design/decisions/llm.md`, pruned to current state |
| `design/AI_CONTEXT.md` + `design/ARCHITECTURE.md` | 36 KB | → `design/architecture.md`, merged, duplication removed |
| `design/RUNBOOK.md` | 46 KB | → `design/runbook.md`. **Every command line kept verbatim.** Only prose around them is cut. |
| `design/CLAUDE.md` | 6 KB | Deleted; its content is the basis of the new `AGENTS.md` |
| `design/docs/*.md` | 85 KB | Pruned in place, names unchanged. `services.md` drops per-service prose that duplicates `decisions/`. |

Naming: `README.md` and `TODO.md` keep the uppercase convention. Every other
design file is lowercase, so `AI_CONTEXT.md`/`ARCHITECTURE.md`/`RUNBOOK.md`
become `architecture.md`/`runbook.md`.

### Kubernetes stack table — the 56-row split

Each row moves to exactly one decision file. No row is dropped; each is pruned
per the rules below.

| New file | Rows absorbed |
|---|---|
| `decisions/backups.md` | application backups, backup failure notifications, backup jobs do not retry, every CronJob sets ttlSecondsAfterFinished, backup-tools image contents, CNPG backups are pg_dump only, rclone filen backend |
| `decisions/cnpg.md` | CNPG operator upgrades roll the DB, DB password management, CNPG managed-role race, CNPG replica that can't rejoin, Reflector |
| `decisions/flux.md` | Flux targetNamespace, Flux version pinning, Flux CRD storage-version migration, Job immutability, Flux failure alerting |
| `decisions/helm-charts.md` | app-template drops `optional` on envFrom, app-template service naming, wrong Helm values path is a silent no-op, Reloader annotation goes on the controller, Stakater Reloader image tag |
| `decisions/cilium-gateway.md` | Cilium Gateway ALPN, Cilium MTU, Gateway API version pin |
| `decisions/monitoring.md` | Monitoring stack, platform resource requests grounded in VictoriaMetrics, kubelet_volume_stats reports the backing filesystem, node disk alert, Goldilocks |
| `decisions/gotify.md` | Gotify, gotify-telegram bridge |
| `decisions/zitadel.md` | Zitadel bootstrap RBAC, bootstrap drift + provider pinning, bootstrap secret formats |
| `decisions/gitea.md` | Gitea chart, Gitea SSH, Gitea cache (valkey), Gitea valkey rebuild traps, Gitea OIDC |
| `decisions/security-tooling.md` | security namespace PSA, Trivy Operator config, Falco, K8s-Cleaner, kubent, manifest-scan kube-linter gate, Descheduler |
| `decisions/media-stack.md` | media stack, manga stack, Kavita OIDC |
| `decisions/oidc-apps.md` | FreshRSS OIDC, Paperless OIDC |
| `decisions/jobs-and-scripts.md` | `apk add` must be bounded, YAML block scalar + Python |
| `decisions/images.md` | image pinning incl. linuxserver.io mutable-tag trap |
| `decisions/homebox.md` | Homebox |
| `decisions/minecraft.md` (existing) | + Minecraft monitoring |
| `design/docs/storage.md` (existing) | + static NFS PV nfsvers |

Unchanged existing decision files: `immich.md`, `joplin.md`, `obsidian-livesync.md`,
`plex.md`, `protonmail-bridge.md`, `proxmox-oidc.md`, `romm.md` — pruned in place.

### Pruning rules

Applied to every file that is rewritten rather than deleted.

**Keep:**
- Current state: what is deployed, where, with what settings.
- Exact strings: IPs, ports, versions that are constraints, flags, file paths,
  env var names, command lines, API paths, annotation keys.
- A rule plus a one-line reason. The reason is what stops re-litigation.

**Cut:**
- Dates and incident narrative — "found 2026-08-28", "hit on 2026-08-02",
  "failed for 68 days", blast-radius stories.
- Duplicated content between files.
- Prose that restates an adjacent table.
- Version numbers describing current state rather than a constraint (already
  policy in the existing CLAUDE.md; now enforced everywhere).

**Boundary case:** where a date is the fact — a recorded incident whose recurrence
is the thing being guarded against — keep the rule and drop the date.

## Verification

Loss is the main risk of an aggressive prune, so verification is mechanical, not
a judgment call.

1. **Extract.** Before any edit, run an extractor over the current
   `.claude/CLAUDE.md` + `design/**` that pulls every hard fact into a checklist:
   - IPv4 addresses
   - file paths (`kubernetes/...`, `infra/...`, `design/...`, absolute paths)
   - shell command lines (fenced code and inline backticks starting with a known binary)
   - env var names (`[A-Z][A-Z0-9_]{3,}`)
   - flags (`--[a-z-]+`)
   - k8s annotation and label keys (`*.io/*`)
   - quoted version constraints
   Write to `docs/superpowers/specs/2026-09-17-facts-before.txt`.
2. **Prune.** Perform the rewrite.
3. **Diff.** Re-extract over the new tree into `facts-after.txt`; report every
   string present before and absent after.
4. **Adjudicate.** For each dropped string, classify as intentional (narrative,
   a deleted proposal doc's internals) or load-bearing (restore it). Report the
   full list; do not silently accept drops.

Acceptance criteria:

- `AGENTS.md` measures ≤2,500 tokens (`wc -c` / 3.7).
- Every routing-table target path exists.
- `.claude/CLAUDE.md` resolves to `AGENTS.md`; `.claude/skills/i-have-adhd/SKILL.md`
  resolves through the symlink.
- `opencode.json` validates against the published schema.
- Zero load-bearing strings dropped, or each one restored.
- `.agents/scripts/validate-manifests.sh <path>` exits 0 on a known-good manifest
  and reports on a known-bad one, invoked directly with no hook involved.
- No file under `kubernetes/`, `infra/`, or `.github/` is modified.

## Risks

| Risk | Mitigation |
|---|---|
| A pruned fact turns out to be load-bearing | Fact-extraction diff; git history retains everything |
| Symlink breaks on a non-Linux checkout | Documented in AGENTS.md; only this Linux workstation uses the repo today |
| A small model ignores the routing table and reads nothing | Hard rules live in the always-loaded file, so the damage-preventing subset is never behind a hop |
| Routing table rots as services are added | Doc-sync rule in AGENTS.md makes updating it part of the change that adds a service |

## Out of scope, flagged

`.claude/settings.local.json` contains a live GitHub personal access token in
plaintext (`github_pat_11AR37...`). The file is untracked so it never entered git,
but it is readable by any process running as this user. Rotating it is a separate
task.
