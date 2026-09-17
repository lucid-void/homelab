# Agent Docs Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut always-loaded agent context from ~21,540 to ≤2,500 tokens, make the repo's agent docs vendor-agnostic across Claude Code / opencode / Cursor, and route design docs on demand instead of loading them eagerly.

**Architecture:** `AGENTS.md` at repo root becomes the single always-loaded file (hard rules + IP map + a routing table); `.claude/CLAUDE.md` becomes a symlink to it. The 60 KB `### Kubernetes stack` table is split into 14 new self-contained files under `design/decisions/`, each reachable in exactly one hop from the routing table. `i-have-adhd` becomes a repo skill both harnesses load. Every prune is verified by a mechanical fact-extraction diff, not by judgment.

**Tech Stack:** Markdown, bash, git symlinks, `opencode.json` (schema: https://opencode.ai/config.json), Claude Code hooks (PostToolUse + PreToolUse), `mise exec` for k8s tooling.

**Spec:** `docs/superpowers/specs/2026-09-17-agent-docs-restructure-design.md`

## Global Constraints

- **Never modify** any file under `kubernetes/`, `infra/`, or `.github/`. This plan is docs and agent config only.
- **`AGENTS.md` final budget: ≤2,500 tokens**, measured as `wc -c < AGENTS.md` divided by 3.7. Current `.claude/CLAUDE.md` baseline: 79,701 B ≈ 21,540 tokens.
- **Local model context ceiling is 32,768 tokens** (`~/.config/opencode/opencode.jsonc`, both `local-fast` and `local-smart`). Every design file must be readable standalone inside that window.
- **One hop.** A routing-table row names a terminal file, never another index. No design file may require another to be read first; duplicate a short fact rather than cross-reference.
- **Prune rules** (from spec, applied to every rewritten file):
  - KEEP: current state; exact strings (IPs, ports, flags, file paths, env var names, command lines, API paths, annotation keys, version *constraints*); a rule plus a one-line reason.
  - CUT: dates and incident narrative ("found 2026-08-28", "failed for 68 days"); blast-radius stories; duplication between files; prose restating an adjacent table; version numbers describing current state rather than a constraint.
  - BOUNDARY: where a date is the fact, keep the rule and drop the date.
- **Naming:** `README.md` and `TODO.md` stay uppercase. Every other design file is lowercase.
- **Supersedes:** `.claude/TODO.md` is a prior plan for this same work whose contract was "relocate verbatim, zero information loss". This plan's contract is **relocate + aggressively prune** (user decision, 2026-09-17). Where the two disagree, this plan wins. `.claude/TODO.md` is deleted in Task 7.
- **Commit after every task.** Never batch two tasks into one commit.
- All k8s tooling is invoked as `mise exec -- <tool>`; it is not on `PATH`.

---

### Task 1: Fact extractor and baseline snapshot

Nothing else in this plan is safe without this. It is the only mechanism that proves an aggressive prune did not drop something load-bearing.

**Files:**
- Create: `.agents/scripts/extract-facts.sh`
- Create: `docs/superpowers/specs/2026-09-17-facts-before.txt`

**Interfaces:**
- Consumes: nothing.
- Produces: `bash .agents/scripts/extract-facts.sh [path...]` → sorted, deduped facts on stdout, exit 0. With no args it scans `AGENTS.md`, `.claude/CLAUDE.md` and `design/**/*.md`, whichever exist. Every later task's verification step calls this.

- [ ] **Step 1: Write the extractor**

Create `.agents/scripts/extract-facts.sh`:

```bash
#!/usr/bin/env bash
# Extract every hard fact from the agent-facing docs into a sorted, deduped checklist.
#
# Usage: .agents/scripts/extract-facts.sh [path...]
#   No args: scans AGENTS.md, .claude/CLAUDE.md and design/**/*.md, whichever exist.
# Output:  one fact per line on stdout, sorted -u.
#
# Each pattern runs as its OWN grep pass. A single multi-pattern grep consumes
# characters left-to-right and never rescans them, so a broad pattern starves a
# narrow one that overlaps it (measured: 253 facts combined vs 982 separated).
set -euo pipefail

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  mapfile -t FILES < <(
    { ls AGENTS.md .claude/CLAUDE.md 2>/dev/null
      find design -name '*.md' 2>/dev/null
    } | sort -u
  )
fi
[[ ${#FILES[@]} -eq 0 ]] && { echo "extract-facts: no input files" >&2; exit 1; }

PATTERNS=(
  '\b[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?\b'                       # IPv4 / CIDR
  '\b(kubernetes|infra|design|\.github|\.agents|\.claude)/[A-Za-z0-9_./*-]+' # repo paths
  '/(volume2|var|etc|opt|config|data|media|home|tmp|usr)/[A-Za-z0-9_./-]+'   # absolute paths
  '\b[A-Z][A-Z0-9]*(_[A-Z0-9]+){1,}\b'                                  # ENV_VAR names
  '--[a-z][a-z0-9-]{2,}'                                                # CLI flags
  '\b[a-z0-9.-]+\.(io|sh|dev|cc|com|org|net)/[A-Za-z0-9_./:-]+'         # registries / URLs
  '\b[a-z0-9.-]+\.(io|com|sh)/[a-z0-9._-]+'                             # annotation keys
  '\b(kubectl|flux|talosctl|talhelper|kubeseal|helm|kubeconform|restic|rclone|tofu|mise|promtool|curl|jq|kubent|shred)\b +[a-z][a-z-]*' # command verbs
  '\bv?[0-9]+\.[0-9]+(\.[0-9]+)?([.-][A-Za-z0-9]+)*\b'                  # versions
  '`[^`]{2,60}`'                                                        # anything the author backticked
)

for p in "${PATTERNS[@]}"; do
  cat "${FILES[@]}" 2>/dev/null | grep -ohE -e "$p" || true
done \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s/[.,;:)]+$//' \
  | grep -vE '^[[:space:]]*$' \
  | sort -u
```

Then `chmod +x .agents/scripts/extract-facts.sh`.

- [ ] **Step 2: Verify it runs clean and every pattern class fires**

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts.txt 2>/tmp/facts.err
echo "stderr bytes: $(wc -c < /tmp/facts.err)"   # expect 0
echo "facts: $(wc -l < /tmp/facts.txt)"          # expect ~982
grep -c '^--'        /tmp/facts.txt              # expect >= 20  (flags)
grep -cE '^172\.16'  /tmp/facts.txt              # expect >= 13  (IPs)
grep -cE '^[A-Z_]+$' /tmp/facts.txt              # expect >= 50  (env vars)
grep -c 'kubernetes/' /tmp/facts.txt             # expect >= 70  (repo paths)
```

Expected: stderr 0 bytes, ~982 facts, every count above its floor. A zero in any count means that pattern is broken — fix before proceeding. In particular, `grep -ohE "$p"` without `-e` makes grep parse `--flag` patterns as its own options; the `-e` is load-bearing.

- [ ] **Step 3: Capture the baseline**

```bash
bash .agents/scripts/extract-facts.sh > docs/superpowers/specs/2026-09-17-facts-before.txt
wc -l docs/superpowers/specs/2026-09-17-facts-before.txt
```

- [ ] **Step 4: Record the token baseline in the same file**

```bash
printf '\n# --- baseline metrics (not facts) ---\n# .claude/CLAUDE.md: %s B ~%s tok\n# design/ total: %s B\n' \
  "$(wc -c < .claude/CLAUDE.md)" \
  "$(( $(wc -c < .claude/CLAUDE.md) * 10 / 37 ))" \
  "$(cat design/*.md design/*/*.md | wc -c)" \
  >> docs/superpowers/specs/2026-09-17-facts-before.txt
tail -5 docs/superpowers/specs/2026-09-17-facts-before.txt
```

- [ ] **Step 5: Commit**

```bash
git add .agents/scripts/extract-facts.sh docs/superpowers/specs/2026-09-17-facts-before.txt
git commit -m "docs: add fact extractor and pre-prune baseline

Per-pattern grep passes; a single multi-pattern grep starves narrow
patterns (253 facts vs 982). Baseline is the reference for the
post-prune loss diff.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Vendor wiring — symlinks, opencode.json, adhd skill

Establishes the plumbing before any content moves, so every later task writes into a layout that is already correct. `AGENTS.md` is created as a stub here and filled in Task 8.

**Files:**
- Create: `AGENTS.md` (stub)
- Create: `opencode.json`
- Create symlink: `.claude/skills/i-have-adhd` → `../../.agents/skills/i-have-adhd`
- Delete then re-create as symlink: `.claude/CLAUDE.md` → `../AGENTS.md`

**Interfaces:**
- Consumes: Task 1's extractor (for the no-loss check).
- Produces: a resolvable `AGENTS.md` that every later task edits; `.claude/CLAUDE.md` resolving to the same inode.

- [ ] **Step 1: Preserve the current CLAUDE.md as the seed for AGENTS.md**

The existing file is the source material for Task 8. Move it, do not delete it.

```bash
git mv .claude/CLAUDE.md AGENTS.md
ls -l AGENTS.md && wc -c AGENTS.md   # expect ~79701 B
```

- [ ] **Step 2: Replace `.claude/CLAUDE.md` with a symlink**

Note the existing repo convention: `.claude/skills/` already contains per-item symlinks (`gitops-cluster-debug -> ../../.agents/skills/gitops-cluster-debug`) tracked by git as mode 120000. Follow that convention rather than symlinking whole directories.

```bash
ln -s ../AGENTS.md .claude/CLAUDE.md
git add .claude/CLAUDE.md
git ls-files -s .claude/CLAUDE.md    # expect mode 120000
```

- [ ] **Step 3: Verify the symlink resolves to the same bytes**

```bash
test "$(readlink .claude/CLAUDE.md)" = "../AGENTS.md" && echo "link OK"
cmp .claude/CLAUDE.md AGENTS.md && echo "content identical"
```

Expected: `link OK` and `content identical`.

- [ ] **Step 4: Add the i-have-adhd skill symlink for Claude Code**

```bash
ln -s ../../.agents/skills/i-have-adhd .claude/skills/i-have-adhd
git add .agents/skills/i-have-adhd .claude/skills/i-have-adhd
test -f .claude/skills/i-have-adhd/SKILL.md && echo "skill resolves"
```

Expected: `skill resolves`. Note `.agents/skills/i-have-adhd/` is currently untracked — this `git add` is what commits it.

- [ ] **Step 5: Create `opencode.json`**

Both keys below were verified present in the live schema (`Config.skills.paths`, `Config.instructions`).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["AGENTS.md"],
  "skills": {
    "paths": [".agents/skills"]
  }
}
```

- [ ] **Step 6: Validate `opencode.json` against the published schema**

```bash
curl -s --max-time 20 https://opencode.ai/config.json -o /tmp/oc-schema.json
python3 - <<'EOF'
import json
cfg = json.load(open('opencode.json'))
sch = json.load(open('/tmp/oc-schema.json'))
props = sch['$defs']['Config']['properties']
for k in cfg:
    if k == '$schema': continue
    assert k in props, f"unknown config key: {k}"
assert 'paths' in props['skills']['properties'], "skills.paths missing from schema"
print("opencode.json OK — keys:", sorted(k for k in cfg if k != '$schema'))
EOF
```

Expected: `opencode.json OK — keys: ['instructions', 'skills']`.

- [ ] **Step 7: Confirm opencode discovers the skill**

```bash
opencode --version
ls .agents/skills/           # expect i-have-adhd + the three gitops-* dirs
head -4 .agents/skills/i-have-adhd/SKILL.md
```

Expected: the SKILL.md frontmatter shows `name: i-have-adhd`. If `opencode` offers a skill-listing subcommand in 1.18.25, run it and confirm `i-have-adhd` appears; if it does not, the `ls` check above is sufficient evidence the path is populated.

- [ ] **Step 8: Verify zero fact loss so far**

Content has only moved, not changed, so the fact set must be identical.

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts-t2.txt
diff <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) /tmp/facts-t2.txt \
  && echo "NO FACT CHANGE — correct for a pure move"
```

Expected: `NO FACT CHANGE`. Any diff here means the move corrupted content — stop and investigate.

- [ ] **Step 9: Commit**

```bash
git add AGENTS.md opencode.json .claude/CLAUDE.md .claude/skills/i-have-adhd .agents/skills/i-have-adhd
git commit -m "chore(agents): make AGENTS.md the source, wire opencode and adhd skill

AGENTS.md at root is read natively by opencode and Cursor; .claude/CLAUDE.md
is now a symlink to it so Claude Code reads the same bytes. opencode.json
points skills.paths at .agents/skills so i-have-adhd loads there too.

Content is unchanged in this commit; the prune follows.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Decision-file template and the five platform files

Splits 27 of the 56 `### Kubernetes stack` rows into five platform-layer files. Establishes the template every subsequent decision file follows.

**Files:**
- Create: `design/decisions/flux.md`, `design/decisions/cnpg.md`, `design/decisions/cilium-gateway.md`, `design/decisions/helm-charts.md`, `design/decisions/backups.md`
- Modify: `AGENTS.md` — delete only the rows absorbed by this task

**Interfaces:**
- Consumes: `AGENTS.md` as it stands after Task 2 (still the full 79,701 B original).
- Produces: five files, each conforming to the template in Step 1. Task 8's routing table links to all five by these exact paths.

- [ ] **Step 1: Fix the template**

Every `design/decisions/*.md` file created or rewritten in Tasks 3–7 uses exactly this shape. No file deviates.

```markdown
# <Topic>

**Read before editing:** `kubernetes/apps/<ns>/<app>/`, `<other path>`

## Current state

<What is deployed, where, and with which settings. Present tense. No history.>

## Rules

- **<The rule, imperative>** — <one line: the consequence of breaking it>
- **<Next rule>** — <consequence>

## Verify

```bash
<the command that checks the rule holds>
```
```

Constraints on every such file:
- Self-contained. It never says "see also `design/decisions/x.md`". If a fact is needed in two files, write it in both.
- Under 8 KB, so it fits a 32k window alongside real work.
- The `## Verify` block is required wherever the source row named a check command; omit the section entirely if it did not.

- [ ] **Step 2: Create `design/decisions/flux.md`**

Absorbs these five rows from `AGENTS.md` `### Kubernetes stack`, verbatim source then pruned per the Global Constraints:
`Flux targetNamespace`, `Flux version pinning`, `Flux CRD storage-version migration`, `Job immutability (bootstrap Jobs)`, `Flux failure alerting (flux-notifications)`.

Read before editing: `kubernetes/flux/`, `kubernetes/apps/*/*/ks.yml`, `kubernetes/apps/monitoring/flux-notifications/`, `kubernetes/bootstrap/flux/`.

Facts that must survive (spot-check these exact strings after writing):
`spec.targetNamespace`, `kubernetes/flux/config/flux.yml`, `kubernetes/bootstrap/flux/kustomization.yml`, `?ref=`, `status.storedVersions`, `image.toolkit.fluxcd.io`, `kubectl patch crd`, `--subresource=status`, `force: true`, `ttlSecondsAfterFinished`, `ReCreateJobFromjob`, `--reload-on-create`, `eventSeverity: error`, `monitoring/flux-gotify`, `headers`, `X-Gotify-Key`, `v1beta3`.

- [ ] **Step 3: Create `design/decisions/cnpg.md`**

Absorbs: `CNPG operator upgrades roll the DB`, `CNPG backups are pg_dump only`, `DB password management`, `CNPG managed-role race (new apps)`, `CNPG replica that can't rejoin blocks image rollouts`, `Reflector`.

Read before editing: `kubernetes/apps/postgres/`, `kubernetes/apps/*/database/`, `kubernetes/apps/kube-system/reflector/`.

Facts that must survive: `ENABLE_INSTANCE_MANAGER_IN_PLACE_UPDATES`, `primaryUpdateStrategy: unsupervised`, `primaryUpdateMethod: restart`, `kubectl get cluster postgres -n postgres`, `{app}-role-secret`, `reflector.v1.k8s.emberstack.com`, `secretKeyRef`, `cannotReconcile`, `kubectl annotate cluster postgres`, `reconcile-nudge`, `status.managedRolesStatus`, `28P01`, `pg_rewind`, `pg_basebackup`, `<cluster>-<n>-join`, `nfs-client`, `barmanObjectStore`, `gitea-db-bootstrap`, `GITEA__database__PASSWD`, `extraEnvFrom`.

- [ ] **Step 4: Create `design/decisions/cilium-gateway.md`**

Absorbs: `Cilium Gateway ALPN`, `Cilium MTU (jumbo frames)`, plus the `Gateway API version` and `Ingress` rows from the `### Platform & networking` table.

Read before editing: `kubernetes/apps/kube-system/cilium/`, `kubernetes/apps/gateway/`, `kubernetes/talos/talconfig.yaml`.

Facts that must survive: `gatewayAPI.enableAlpn: true`, `kubernetes/apps/kube-system/cilium/app/helm-values.yml`, `curl -v --http2`, `MTU: 9000`, `ens18`, `wt0`, `cilium_vxlan`, `cilium_wg0`, `talosctl get links`, `infra/terraform/kubernetes.tf`, `host_mtu`, `HasTCPRouteSupport`, `gateway.networking.k8s.io/v1`, `kubectl get tcproutes.v1.gateway.networking.k8s.io`, `GatewayClass.status.supportedFeatures`, `prune: true`, `xbackends`, `<1.7.0`, `1.6.1`.

Note: `1.6.1` and `<1.7.0` are version *constraints*, not current state — the Global Constraints keep them.

- [ ] **Step 5: Create `design/decisions/helm-charts.md`**

Absorbs: `app-template silently drops optional on envFrom`, `app-template service naming`, `A wrong Helm values path is a silent no-op`, `Reloader annotation goes on the controller, not the pod`, `Stakater Reloader image tag`.

Read before editing: any `HelmRelease` under `kubernetes/apps/`, `kubernetes/apps/kube-system/reloader/`.

Facts that must survive: `controllers.<n>.containers.<c>.envFrom[].secretRef.optional`, `CreateContainerConfigError`, `anyOf' failed`, `kubectl get deploy <n> -o jsonpath='{..envFrom}'`, `{release-name}-{service-name}`, `helm template`, `yq`, `kubectl get pod -o jsonpath='{.spec.containers[*].resources}'`, `controller.resources`, `valkey-cluster.valkey.resources`, `reloader.stakater.com/auto`, `spec.template.metadata.annotations`, `controllers.<name>.pod.annotations`, `kubectl get deploy <n> -o jsonpath='{.metadata.annotations}'`, `reloader.deployment.image.tag`, `appVersion: vv1.0.112`, `ImagePullBackOff`.

- [ ] **Step 6: Create `design/decisions/backups.md`**

Absorbs: `application backups`, `backup failure notifications`, `backup jobs do not retry`, `every CronJob sets ttlSecondsAfterFinished`, `backup-tools image contents`, `rclone filen backend`, `CNPG backups are pg_dump only` (duplicated here deliberately — see the one-hop rule), plus the `Offsite backups`, `Application backups`, `PBS / VM backups` and `etcd backup` rows from `### Storage, secrets, backups`.

Read before editing: `kubernetes/apps/*/backup/`, `kubernetes/apps/kube-system/etcd-snapshot/`, `kubernetes/images/backup-tools/`.

Facts that must survive: `ghcr.io/lucid-void/backup-tools`, `rclone:filen:backups/restic/{name}`, `--group-by ''`, `--keep-daily 30`, `--keep-monthly 12`, `--prune`, `trap cleanup EXIT`, `podAffinity`, `immich-library`, `nfs-client`, `openebs-hostpath`, `databases.yml`, `gotify-secret`, `optional: true`, `--exclude=/data/index`, `document_index reindex`, `exec > >(tee "$LOG") 2>&1`, `tail -10`, `http://gotify.monitoring.svc.cluster.local/message`, `notify_gotify`, `>/dev/null || true`, `backoffLimit: 0`, `restartPolicy: Never`, `ttlSecondsAfterFinished: 86400`, `failedJobsHistoryLimit: 3`, `KubeJobFailed`, `ignore-check.kube-linter.io/job-ttl-seconds-after-finished`, `downloads.rclone.org`, `v1.69`, `talosctl etcd snapshot`, `TALOS_VERSION`, `talosctl bootstrap --recover-from`, `restic-secret`, `rclone-secret`, `talosconfig-secret`, the schedule times `02:00 02:30 03:00 03:30 04:00 05:00 06:00 07:00 01:00`.

- [ ] **Step 7: Delete the absorbed rows from AGENTS.md**

Remove exactly the 27 rows named in Steps 2–6 from the `### Kubernetes stack`, `### Platform & networking` and `### Storage, secrets, backups` tables. Do not yet add routing rows — Task 8 rebuilds the tables wholesale.

```bash
wc -c AGENTS.md    # expect a drop of roughly 22,000 B from 79,701
```

- [ ] **Step 8: Verify no fact was lost**

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts-t3.txt
comm -23 <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) /tmp/facts-t3.txt > /tmp/dropped-t3.txt
echo "dropped: $(wc -l < /tmp/dropped-t3.txt)"
cat /tmp/dropped-t3.txt
```

Every line in `/tmp/dropped-t3.txt` must be adjudicated aloud as either intentional (a date, a narrative fragment, a duplicated string) or load-bearing (restore it into the relevant new file and re-run). Do not proceed with any unadjudicated drop.

- [ ] **Step 9: Check every new file fits the window**

```bash
for f in design/decisions/{flux,cnpg,cilium-gateway,helm-charts,backups}.md; do
  printf '%6d B  ~%5d tok  %s\n' "$(wc -c <"$f")" "$(( $(wc -c <"$f") * 10 / 37 ))" "$f"
done
```

Expected: every file under 8,192 B. A file over budget means it should have been two files — split it and note the split.

- [ ] **Step 10: Commit**

```bash
git add design/decisions/ AGENTS.md
git commit -m "docs(design): split platform gotchas out of the always-loaded file

flux, cnpg, cilium-gateway, helm-charts, backups. 27 rows relocated and
pruned per the spec; verified no load-bearing fact dropped.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Application decision files

Splits 16 more rows into seven app-layer files.

**Files:**
- Create: `design/decisions/gitea.md`, `design/decisions/zitadel.md`, `design/decisions/gotify.md`, `design/decisions/media-stack.md`, `design/decisions/oidc-apps.md`, `design/decisions/homebox.md`, `design/decisions/images.md`
- Modify: `AGENTS.md` — delete only the rows absorbed here

**Interfaces:**
- Consumes: the template fixed in Task 3 Step 1; `AGENTS.md` as left by Task 3.
- Produces: seven files at these exact paths, linked by Task 8's routing table.

- [ ] **Step 1: Create `design/decisions/gitea.md`**

Absorbs: `Gitea chart`, `Gitea SSH`, `Gitea cache (valkey)`, `Gitea valkey rebuild traps`, `Gitea OIDC`.

Read before editing: `kubernetes/apps/gitea/`.

Facts that must survive: `gitea-charts/gitea`, `https://dl.gitea.com/charts/`, `postgresql.enabled: false`, `postgresql-ha.enabled: false`, `gitea-http`, port `3000`, `gitea-db-env`, `TCPRoute`, `kubernetes/apps/gitea/gitea/app/tcproute.yml`, `gitea-ssh`, `2222`, `START_SSH_SERVER`, `SSH_DOMAIN`, `SSH_PORT`, `externalTrafficPolicy: Cluster`, `gateway.cilium.io/backend-service`, `kubectl get endpointslice -n gateway`, `CiliumEnvoyConfig`, `enable-gateway-api-proxy-protocol`, `kubectl -n kube-system rollout restart deploy/cilium-operator`, `tcp://gitea.blackcats.cc:22`, `redis-cluster`, `valkey-cluster`, `cache.ADAPTER`, `session.PROVIDER`, `queue.TYPE`, `_helpers.tpl`, `cluster.nodes`, `nodes: 6`, `replicas: 1`, `cluster.update.addNodes: true`, `post-upgrade`, `--wait`, `VALKEY_CLUSTER_CREATOR=yes`, `flux suspend`, `kubectl scale sts`, `nodes.conf`, `CLUSTER REPLICATE`, `valkey-cli --cluster create`, `pending-upgrade`, `https://gitea.blackcats.cc/user/oauth2/Zitadel/callback`, `gitea.oauth`, `autoDiscoverUrl`, `DISABLE_REGISTRATION: false`, `ALLOW_ONLY_EXTERNAL_REGISTRATION: true`, `passwordMode: initialOnlyNoReset`, `keepUpdated`, `initialOnlyRequireReset`.

This file will approach the 8 KB ceiling. If it exceeds, split the valkey material into `design/decisions/gitea-valkey.md` and give it its own routing row.

- [ ] **Step 2: Create `design/decisions/zitadel.md`**

Absorbs: `Zitadel bootstrap RBAC`, `Zitadel bootstrap drift + provider pinning`, `Zitadel bootstrap secret formats`, plus the `SSO provider` and `SSO model` rows from `### Auth & identity`.

Read before editing: `kubernetes/apps/auth/`, `infra/terraform/`.

Facts that must survive: `zitadel.blackcats.cc`, `targetNamespace: auth`, `kubernetes/apps/auth/bootstrap-rbac/`, `ttlSecondsAfterFinished: 3600`, `tofu init -lockfile=readonly`, `tofu plan -out=tfplan`, `tofu show -json tfplan`, `tofu apply tfplan`, `emptyDir`, `backup-tools`, `main.tf`, `run.sh`, `.terraform.lock.hcl`, `configHash`, `auth/zitadel-bootstrap-state`, `.resource_changes[].change.after`, `.address`, `.change.actions`, `auth/gotify-secret`, `required_providers`, `mise exec -- tofu -chdir=<tmpdir> providers lock -platform=linux_amd64`, `data["values.yaml"]`, `valuesFrom`, `valuesKey: values.yaml`, `envFrom: secretRef`.

- [ ] **Step 3: Create `design/decisions/gotify.md`**

Absorbs: `Gotify`, `gotify-telegram bridge`.

Read before editing: `kubernetes/apps/monitoring/gotify/`, `kubernetes/apps/monitoring/gotify-bootstrap/`, `kubernetes/apps/monitoring/gotify-telegram/`.

Facts that must survive: `gotify/server`, `gotify.blackcats.cc`, `gotify-admin-secret`, `gotify-bootstrap`, `GOTIFY_TOKEN`, `monitoring/gotify-client-secret`, `GET /application`, `GET /client`, `POST /application`, `PUT /application/{id}/security`, `{"regenerateToken":true}`, `auth.RequireElevatedClient`, `/client/{id}/elevate`, `PUT /client/{id}`, `expiresAfterInactivitySeconds: 0`, `GOTIFY_SERVER_PORT`, `GOTIFY_DATABASE_*`, `GOTIFY_DEFAULTUSER_*`, `ttlSecondsAfterFinished: 3600`, `app/bootstrap.sh`, `configMapGenerator`, `options.immutable`, `monitoring/gotify-bootstrap-state`, `DRIFT:`, `monitoring/gotify-bootstrap-secret`, `activeDeadlineSeconds: 600`, `until curl .../health`, `/stream?token=CLIENT_TOKEN`, `pip install --target /tmp/pylib`, `PYTHONPATH=/tmp/pylib`, `python -u`, uid `65534`, `/.local`.

- [ ] **Step 4: Create `design/decisions/media-stack.md`**

Absorbs: `media stack`, `manga stack`, `Kavita OIDC`.

Read before editing: `kubernetes/apps/media/` (excluding `plex/`, `romm/`, `minecraft*/`, which keep their own files).

Facts that must survive: `PUID=2202`, `PGID=2200`, `ghcr.io/seerr-team/seerr`, `runAsUser`, `runAsGroup`, `fsGroup`, `media-nfs`, `nfs-client`, `ghcr.io/suwayomi/suwayomi-server`, port `4567`, `ghcr.io/flaresolverr/flaresolverr`, `suwayomi-flaresolverr:8191`, `suwayomi-app`, `BIND_PORT`, `DOWNLOAD_AS_CBZ=true`, `AUTH_MODE=none`, `FLARESOLVERR_ENABLED`, `FLARESOLVERR_URL`, `server-reference.conf`, `/home/suwayomi/.local/share/Tachidesk`, `suwayomi-config`, `EXTENSION_REPOS`, `https://github.com/keiyoushi/extensions/tree/repo`, `/api/account/register`, `kavita-admin-secret`, `/config/appsettings.json`, `OpenIdConnectSettings`, `Authority`, `ClientId`, `Secret`, `https://kavita.blackcats.cc/signin-oidc`, `kavita-oidc-secret`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `oidc-config`, `secretKeyRef`, `POST /api/settings`, `provisionAccounts=true`, `requireVerifiedEmail=false`, `defaultRoles=["Login"]`, `defaultLibraries`, `defaultAgeRestriction=-1`, `defaultIncludeUnknowns=true`, `UpdateSettings`.

Also record the negative decisions so they are not retried: Tranga removed, Kaizoku and mangal archived — do not deploy.

- [ ] **Step 5: Create `design/decisions/oidc-apps.md`**

Absorbs: `FreshRSS OIDC`, `Paperless OIDC`.

Read before editing: `kubernetes/apps/freshrss/`, `kubernetes/apps/paperless/paperless/`.

Facts that must survive: `OIDC_ENABLED=1`, `https://rss.blackcats.cc/i/oidc/`, `OIDC_CLIENT_CRYPTO_KEY`, `OIDC_REMOTE_USER_CLAIM`, `OIDC_X_FORWARDED_HEADERS`, `X-Forwarded-Host`, `freshrss-oidc-secret`, `PAPERLESS_SOCIALACCOUNT_PROVIDERS`, `openid_connect.APPS[].provider_id`, `zitadel`, `https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/`, `PAPERLESS_APPS`, `allauth.socialaccount.providers.openid_connect`, `PAPERLESS_ACCOUNT_DEFAULT_HTTP_PROTOCOL=https`, `PAPERLESS_ACCOUNT_EMAIL_VERIFICATION=none`, `paperless-oidc-secret`, `settings.token_auth_method`, `client_secret_post`, `client_secret_basic`, `OIDC_AUTH_METHOD_TYPE_POST`, `65.16`.

`65.16` is a recorded-incident version the Global Constraints keep: the rule is "pin `token_auth_method` to whatever the Zitadel app is registered as, never what the migration guide says", and the allauth version is what makes that rule non-obvious.

- [ ] **Step 6: Create `design/decisions/homebox.md`**

Absorbs: `Homebox`.

Read before editing: `kubernetes/apps/homebox/`.

Facts that must survive: `replicas: 1`, `NOT NULL constraint failed: new_users.group_users`, `0.25.0`, `v0.26.x`, `HBOX_AUTH_API_KEY_PEPPER`, `homebox-secret`, `secretKeyRef`, and the rule that the pepper must stay stable because rotating it invalidates every issued API key.

- [ ] **Step 7: Create `design/decisions/images.md`**

Absorbs: the `Image pinning` row from `### Storage, secrets, backups`.

Read before editing: `.github/renovate.json` (read-only — this plan must not modify it), any manifest carrying an image tag.

Facts that must survive: `traefik:v3.1`, `latest`, `plex:1.43.3.10896-cb3ebc72d-ls321`, `sabnzbd:5.1.2-ls270`, `imagePullPolicy`, `IfNotPresent`, `prConcurrentLimit: 6`, `.github/renovate.json`, `lscr`, `image-scan`, and the rule that linuxserver.io short `X.Y.Z` tags are mutable so full tags are mandatory.

- [ ] **Step 8: Delete the absorbed rows from AGENTS.md**

```bash
wc -c AGENTS.md   # expect a further drop of roughly 20,000 B
```

- [ ] **Step 9: Verify no fact was lost**

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts-t4.txt
comm -23 <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) /tmp/facts-t4.txt
```

Adjudicate every line as in Task 3 Step 8.

- [ ] **Step 10: Check sizes and commit**

```bash
for f in design/decisions/{gitea,zitadel,gotify,media-stack,oidc-apps,homebox,images}.md; do
  printf '%6d B  %s\n' "$(wc -c <"$f")" "$f"; done
git add design/decisions/ AGENTS.md
git commit -m "docs(design): split application gotchas out of the always-loaded file

gitea, zitadel, gotify, media-stack, oidc-apps, homebox, images.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Operations decision files, and fold monitoring into minecraft.md

Splits the remaining 13 rows.

**Files:**
- Create: `design/decisions/monitoring.md`, `design/decisions/security-tooling.md`, `design/decisions/jobs-and-scripts.md`
- Modify: `design/decisions/minecraft.md` (absorb `Minecraft monitoring`), `design/docs/storage.md` (absorb `static NFS PV nfsvers`), `AGENTS.md`

**Interfaces:**
- Consumes: the Task 3 template; `AGENTS.md` as left by Task 4.
- Produces: three new files plus two amended existing ones; all five linked by Task 8.

- [ ] **Step 1: Create `design/decisions/monitoring.md`**

Absorbs: `Monitoring stack`, `Platform resource requests are grounded in VictoriaMetrics`, `kubelet_volume_stats_* reports the backing filesystem`, `Node disk has its own alert`, `Goldilocks`.

Read before editing: `kubernetes/apps/monitoring/`, any `VMRule` or `VMServiceScrape`, `kubernetes/apps/goldilocks/`.

Facts that must survive: `vm-stack`, `selectAllByDefault`, `commonMetadata`, `app.kubernetes.io/name`, `monitoring.blackcats.cc/scrape`, `promtool check rules`, `metrics_path="/metrics/cadvisor"`, `/metrics/resource`, `container_memory_working_set_bytes`, `container_cpu_usage_seconds_total`, `kube_pod_container_*`, `count by (metrics_path) (<metric>{...})`, `quantile_over_time(0.90, ...[30d])`, `kubelet_volume_stats_*`, `openebs-hostpath`, `nfs-client`, `/volume2`, `NodeFilesystemAlmostFull`, `NodeFilesystemCriticallyFull`, `SynologyShareAlmostFull`, `vm-stack/vmrules.yml`, `imageGCHighThresholdPercent: 70`, `talconfig.yaml`, `kubernetes/apps/media/minecraft/app/world-size-configmap.yml`, `grafana_dashboard: "1"`, `fairwinds-stable/vpa`, `https://charts.fairwinds.com/stable`, `controller.flags.on-by-default: true`, `goldilocks.blackcats.cc`, `goldilocks-dashboard:80`, `du`, PSA `baseline`, `hostPath`.

Carry forward the rule that Goldilocks/VPA recommendations are fabricated (no metrics-server) so nothing is ever sized from them.

- [ ] **Step 2: Create `design/decisions/security-tooling.md`**

Absorbs: `security namespace PSA`, `Trivy Operator config`, `Falco`, `K8s-Cleaner`, `kubent`, `manifest-scan kube-linter gate`, `Descheduler`.

Read before editing: `kubernetes/apps/security/`, `kubernetes/apps/kube-system/k8s-cleaner*/`, `kubernetes/apps/kube-system/descheduler/`, `.github/kube-linter-config.yaml`, `.github/kube-linter-run.sh` (read-only).

Facts that must survive: `pod-security.kubernetes.io/enforce: privileged`, `aquasecurity/trivy-operator`, `standalone`, `scanJobsConcurrentLimit: 2`, `trivy.dbRepository`, `aquasec/trivy-db`, `mirror.gcr.io/ghcr.io/...`, `operator.infraAssessmentScannerEnabled: false`, `mkdir /etc/systemd`, `max_over_time(container_memory_working_set_bytes{namespace="security",container="trivy-operator",metrics_path="/metrics/cadvisor"}[7d])`, `2Gi`, `falcosecurity/falco`, `https://falcosecurity.github.io/charts`, `driver.kind: modern_ebpf`, `insmod`, `/sys/kernel/btf/vmlinux`, `allowSchedulingOnControlPlanes: true`, `security/falco-gotify-secret`, `oci://ghcr.io/gianlucam76/charts`, `apps.projectsveltos.io/v1alpha1`, `aggregatedSelection`, `{resources = {{resource = obj}, …}}`, `[]ResourceResult`, `executor/worker.go:721`, `GetKind()`, `ghcr.io/doitintl/kube-no-trouble`, `-o text -O /work/kubent.out`, `curlimages/curl`, `--exit-error`, `activeDeadlineSeconds`, `900`, `kubectl get cronjob kubent -n security -o jsonpath='{.status}'`, `lastSuccessfulTime`, `lastScheduleTime`, `kubectl create job --from=cronjob/kubent`, `checks.exclude`, `addAllBuiltIn: true`, `minimum-three-replicas`, `ignore-check.kube-linter.io/<check>`, `kubeconform`, `exit_code`, `kubernetes-sigs/descheduler`, `https://kubernetes-sigs.github.io/descheduler/`.

- [ ] **Step 3: Create `design/decisions/jobs-and-scripts.md`**

Absorbs: `apk add in a manifest must be bounded`, `YAML block scalar + Python`.

Read before editing: any `Job`, `CronJob` or `initContainer` under `kubernetes/apps/`.

Facts that must survive: `timeout 300`, `>/dev/null 2>&1`, `set -e`, `backoffLimit`, `kubectl exec <pod> -- ps -o pid,etime,args`, `kubectl logs`, `backup-tools`, `configMapGenerator`, and the rule that Python at 0-indent inside a YAML `|` block breaks the kustomize parser so scripts go in their own ConfigMap key.

Cross-cutting note to include verbatim in this file (it is a rule, not a reference): a bootstrap Job whose manifest changes while the old Job still exists needs `force: true` on its Flux Kustomization, or `kubectl delete job <name> -n <ns>`.

- [ ] **Step 4: Fold `Minecraft monitoring` into `design/decisions/minecraft.md`**

Append a `## Monitoring` section to the existing file rather than creating a new one; the routing table already sends Minecraft work there and a second file would break the one-hop rule.

Facts that must survive: `itzg/mc-monitor`, `export-for-prometheus`, `EXPORT_SERVERS`, `minecraft_status_healthy`, `minecraft_status_players_online_count`, `minecraft_status_players_max_count`, `minecraft_status_response_time_seconds`, `minecraft-proxy`, `MinecraftServerDown`, `MinecraftProxyDown`, `MinecraftWorldGrowingFast`, `MinecraftWorldLarge`, `MinecraftWorldSizeExporterStale`, `world-size-configmap.yml`, `:9109`, `matcha-metrics`, `vanilla-metrics`, `minecraft_world_size_bytes`, `minecraft_server_data_size_bytes`, `st_blocks*512`, `300s`, `publishNotReadyAddresses: true`, `media/minecraft-events`, `kubectl logs --follow`, `[connected player]`, `[server connection]`, `minecraft-events-gotify-secret`, `openebs-hostpath`, `20Gi`, `tcp://`.

Also carry the negative fact: there is no TPS metric, and no maintained exporter provides one — tick health is inferred from response time plus container CPU.

- [ ] **Step 5: Fold `static NFS PV nfsvers` into `design/docs/storage.md`**

Append to the existing NFS section. Facts: `nfsvers=4`, `nfsvers=4.1`, `Protocol not supported`, `mountOptions`, and that democratic-csi dynamic PVCs are unaffected because they mount inside privileged containers.

- [ ] **Step 5b: Prune the seven pre-existing decision files in place**

These predate this work and have never been pruned. Apply the Global Constraints
prune rules and the Task 3 Step 1 template to each. Names and paths do not change.

| File | Now | Note |
|---|---|---|
| `design/decisions/protonmail-bridge.md` | 14,501 B | Over the 8 KB window budget — prune hard; the load-bearing facts are the single `IP:127.0.0.1` SAN, the socat sidecar, `openebs-hostpath`, interactive login cannot be a Job, and `ghcr.io/videocurio/…` not `shenxn/…` |
| `design/decisions/obsidian-livesync.md` | 10,807 B | Over budget — the five traps are the content: the ConfigMap under `/opt/couchdb` killing the entrypoint via recursive `chown -f` under `set -e` (exit 1, empty logs), `NODENAME`, `single_node`, authenticated `exec` probes, per-platform CORS origins |
| `design/decisions/minecraft.md` | 14,015 B | Already grew in Step 4; if it exceeds 12 KB after the monitoring fold, split monitoring into `design/decisions/minecraft-monitoring.md` and add a routing row for it in Task 8 |
| `design/decisions/immich.md` | 4,311 B | Keep the `DB_VECTOR_EXTENSION` must-stay-unset rule, VectorChord, the `OPTFLAGS` build requirement, and that image tag v1.1.1 is permanently broken |
| `design/decisions/joplin.md` | 2,732 B | Keep: SSO is SAML not OIDC, SP metadata byte-identical to Terraform, probes need an explicit `Host` header, blobs on a dedicated PVC not `Type=Database` |
| `design/decisions/romm.md` | 1,989 B | Keep: external CNPG + embedded Valkey on `emptyDir`, runs as root and ignores PUID/PGID, OIDC via optional `envFrom` |
| `design/decisions/plex.md` | 1,304 B | Keep: `replicas: 1`, pool-b LB pinned to `172.16.20.51` and must not drift to `.52`, CPU-only transcoding |
| `design/decisions/proxmox-oidc.md` | 871 B | Keep: Proxmox is bare metal not a k8s workload, and never front it behind the cluster Gateway (circular dependency) |

Verify the two oversized files came down:

```bash
for f in design/decisions/{protonmail-bridge,obsidian-livesync,minecraft}.md; do
  printf '%6d B  %s\n' "$(wc -c <"$f")" "$f"; done
```

Expected: `protonmail-bridge.md` and `obsidian-livesync.md` both under 8,192 B.

- [ ] **Step 6: Delete the absorbed rows from AGENTS.md**

After this step the `### Kubernetes stack` table must be empty of rows. Verify:

```bash
python3 - <<'EOF'
t = open('AGENTS.md').read()
if '### Kubernetes stack' in t:
    sec = t.split('### Kubernetes stack')[1].split('\n## ')[0]
    rows = [l for l in sec.split('\n')
            if l.startswith('| ') and not l.startswith('| Topic') and not l.startswith('|---')]
    print("remaining k8s-stack rows:", len(rows))
    for r in rows: print("  -", r.split('|')[1].strip()[:70])
else:
    print("### Kubernetes stack section removed entirely")
EOF
```

Expected: `0` remaining rows, or the section gone.

- [ ] **Step 7: Verify no fact was lost, check sizes, commit**

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts-t5.txt
comm -23 <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) /tmp/facts-t5.txt
for f in design/decisions/{monitoring,security-tooling,jobs-and-scripts,minecraft}.md; do
  printf '%6d B  %s\n' "$(wc -c <"$f")" "$f"; done
git add design/decisions/ design/docs/storage.md AGENTS.md
git commit -m "docs(design): split ops gotchas out of the always-loaded file

monitoring, security-tooling, jobs-and-scripts; minecraft monitoring
folded into the existing minecraft decision file; nfsvers rule folded
into docs/storage.md. Kubernetes stack table is now empty.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Merge and prune the core reference docs

**Files:**
- Create: `design/architecture.md` (from `design/AI_CONTEXT.md` + `design/ARCHITECTURE.md`)
- Rename: `design/RUNBOOK.md` → `design/runbook.md`, pruned
- Delete: `design/AI_CONTEXT.md`, `design/ARCHITECTURE.md`
- Modify: `design/docs/{networking,storage,secrets,gitops,services}.md`

**Interfaces:**
- Consumes: the decision files from Tasks 3–5 (their content is now authoritative; duplication here must be removed in favour of them).
- Produces: `design/architecture.md` and `design/runbook.md` at these exact paths, both linked by Task 8's routing table.

- [ ] **Step 0: Absorb the leftover policy rows from AGENTS.md**

Seven rows in `### Platform & networking` and `### Storage, secrets, backups` are
repo-wide topology and policy, not a subsystem gotcha. They have no decision file
and belong in `design/architecture.md`. Move them there, then delete them from
`AGENTS.md`:

| Row | Facts that must survive |
|---|---|
| `Compute platform` | 3 control planes `cp-1/2/3`, `.11`–`.13`, schedulable, API VIP `.10`, Gateway VIP `.50`, no dedicated workers |
| `Internet exposure` | Cloudflare DNS for DNS-01 certs only, all A records to internal IPs, no port forwarding on the UDM SE, no Cloudflare proxy, remote access requires Netbird |
| `Netbird / ZeroTier` | Netbird is a Talos extension on every node (`wt0`), isolated from k8s networking; ZeroTier is gaming-only on a separate VM outside the cluster |
| `Cloudflare API tokens` | one token per consumer (external-dns, cert-manager, Proxmox), Zone→DNS→Edit on `blackcats.cc` only, isolated for independent revocation |
| `NFS / Postgres traffic` | cleartext on the internal VLAN — accepted risk, private network, VPN-gated |
| `Tofu state` | PostgreSQL on the Synology, `tofu_state` database; if lost, run `tofu apply` fresh |
| `UniFi backup` | not backed up; VLAN and firewall rules are reconfigured by hand after a reset |

The `Secrets` and `DNS` rows from the same tables are already covered —
`design/docs/secrets.md` and `design/docs/networking.md` respectively. Delete them
from `AGENTS.md` too rather than duplicating; the hard-rules block in Task 8 keeps
the one-line "never write a raw Secret" invariant.

- [ ] **Step 1: Merge AI_CONTEXT.md + ARCHITECTURE.md into design/architecture.md**

`AI_CONTEXT.md` (25 KB) and `ARCHITECTURE.md` (11 KB) both describe topology, networking, storage, databases, auth, GitOps, secrets and backups. Merge section by section, keeping one statement of each fact.

Must survive: the full IP map, node roles, the Netbird `wt0` / `100.80.x.x/16` isolation guards in `talconfig.yaml`, VLAN and NFS cleartext accepted-risk note, Cloudflare token-per-consumer rule, the Synology share table (`/volume2/Media/`, `/volume2/backups/`), and the `tofu_state` database location.

Cut: any paragraph whose content now lives in a `design/decisions/*.md` file from Tasks 3–5. Replace with nothing — the routing table, not this file, is what sends a reader there.

- [ ] **Step 2: Prune design/RUNBOOK.md into design/runbook.md**

```bash
git mv design/RUNBOOK.md design/runbook.md
```

**Every fenced code block and every command line is kept verbatim.** Only the prose around them is cut. This is the one file where the prune rules are subordinate to preserving operational detail — a half-remembered recovery command is worse than a long file.

Procedures that must remain complete and runnable: bootstrap from zero, Talos upgrade (including the `installer` vs `metal-installer` distinction), Flux upgrade (including the CRD `storedVersions` patch), CNPG replica rebuild, etcd restore via `talosctl bootstrap --recover-from`, paperless `document_index reindex`.

- [ ] **Step 3: Prune the five design/docs files**

Names unchanged. Remove content that now duplicates a decision file, and remove narrative. `services.md` (27 KB) is the biggest win: it must keep the inventory table (namespace, hostname, auth, storage per service) and the OIDC callback URI list, and drop per-service prose that Tasks 3–5 relocated.

- [ ] **Step 4: Verify no fact was lost**

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts-t6.txt
comm -23 <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) /tmp/facts-t6.txt
```

Adjudicate every line. Commands dropped from `runbook.md` are always load-bearing — restore them.

- [ ] **Step 5: Confirm every runbook command block survived**

```bash
git show HEAD:design/RUNBOOK.md | grep -cE '^\s*(mise exec|kubectl|talosctl|talhelper|flux|helm|kubeseal|tofu|packer|sops|restic|rclone) '
grep -cE '^\s*(mise exec|kubectl|talosctl|talhelper|flux|helm|kubeseal|tofu|packer|sops|restic|rclone) ' design/runbook.md
```

Expected: the second number is greater than or equal to the first. If lower, commands were lost — restore them.

- [ ] **Step 6: Commit**

```bash
git add design/
git commit -m "docs(design): merge AI_CONTEXT+ARCHITECTURE, prune runbook and docs

Every runbook command kept verbatim; only prose cut. Content that moved
to design/decisions/ is removed here rather than duplicated.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Delete proposals, rewrite TODO, retire superseded files

**Files:**
- Delete: `design/llm-inference.md`, `design/ups-power.md`, `design/monitoring-stabilization.md`, `design/CLAUDE.md`, `.claude/TODO.md`
- Create: `design/decisions/llm.md` (from `design/llm-deployment.md`)
- Delete: `design/llm-deployment.md`
- Rewrite: `design/TODO.md`

**Interfaces:**
- Consumes: nothing from earlier tasks beyond the extractor.
- Produces: `design/decisions/llm.md`, linked by Task 8.

- [ ] **Step 1: Convert llm-deployment.md into design/decisions/llm.md**

This also absorbs the `LLM stack (ai ns)` row from `AGENTS.md`'s `### Service-specific`
table — delete that row from `AGENTS.md` as part of this step. Its content is a
condensed duplicate of `llm-deployment.md`; where the two disagree, the longer
file wins, but the row's four named traps must all appear in the result: the
unbounded `--ctx-size` pre-allocation, the llama-swap group `swap/exclusive`
default evicting the chat model, `logToStdout: proxy` swallowing llama-server's
log, and `--cache-reuse` being discarded for this model.


```bash
git mv design/llm-deployment.md design/decisions/llm.md
```

Then prune to current state using the Task 3 template.

Facts that must survive: `Qwen3.6-35B-A3B Q8_0`, `34.4 GiB`, `llama.cpp`, `llama-swap`, `llm-1`, taint `workload=llm`, `LiteLLM`, `llm.blackcats.cc`, `Open WebUI`, `chat.blackcats.cc`, `local-smart`, `local-fast`, `enable_thinking: false`, `extra_body`, `--ctx-size`, `--mmproj`, `--cache-reuse`, `!llama_memory_can_shift`, `8192 MiB`, `swap/exclusive: true`, `logToStdout`, `proxy`, `GET /logs`, `/logs/stream/upstream`, `4Gi`, exit `137`, `/metrics`, `ENABLE_OAUTH_SIGNUP`, `ENABLE_PERSISTENT_CONFIG`, `--threads 6`, `openwebui-oidc-secret`, `kubernetes/apps/ai/`, `kubernetes/apps/monitoring/ai-monitoring/`.

Also keep the measured decode rate `8.4–8.9 tok/s` and the thinking-cost comparison (`572 chars / 152 tokens` vs `0 / 2`) — both are the evidence for routing agents to `local-fast`, which is a live decision, not history.

- [ ] **Step 2: Delete the three proposal documents**

```bash
git rm design/llm-inference.md design/ups-power.md design/monitoring-stabilization.md
```

Before deleting, confirm nothing in the new tree references them:

```bash
grep -rn 'llm-inference\|ups-power\|monitoring-stabilization' AGENTS.md design/ .claude/ .agents/ || echo "no dangling references"
```

Expected: `no dangling references`. Any hit must be removed first.

- [ ] **Step 3: Rewrite design/TODO.md**

From 57 KB to a short prioritized list. Keep only items that are genuinely open. Drop the `Stale / Needs Update`, `Needs Verification` and `Service Candidates` sections wholesale — they are narrative, and their durable content is either already fixed or belongs in a decision file.

Shape:

```markdown
# TODO

Open work only. A finished item is deleted, not struck through.

## Broken now
- <item> — <one line on impact>

## Planned
- <item>

## Accepted gaps
- <item> — <one line on why it is accepted>
```

Two items from the old file must survive because they are live constraints referenced elsewhere: Goldilocks/VPA recommendations are fabricated (no metrics-server), and `kube-proxy` is an orphaned bootstrap DaemonSet that should be deleted rather than sized.

- [ ] **Step 4: Retire the superseded plan and the old design/CLAUDE.md**

`design/CLAUDE.md` content is folded into `AGENTS.md` in Task 8; `.claude/TODO.md` is the prior plan for this work.

```bash
git rm design/CLAUDE.md .claude/TODO.md
```

Before removing `design/CLAUDE.md`, copy its `## What NOT to Do` list into the scratchpad — Task 8 Step 2 uses it as the basis for the hard-rules block:

```bash
sed -n '/## What NOT to Do/,/^## /p' design/CLAUDE.md > /tmp/hard-rules-seed.md
wc -l /tmp/hard-rules-seed.md
```

- [ ] **Step 5: Verify no dangling references anywhere**

```bash
grep -rn 'design/CLAUDE.md\|\.claude/TODO.md\|AI_CONTEXT\|ARCHITECTURE.md\|RUNBOOK.md\|llm-deployment' \
  AGENTS.md design/ .claude/ .agents/ docs/superpowers/plans/ || echo "clean"
```

Expected: `clean`, except for occurrences inside this plan file itself, which are historical and fine.

- [ ] **Step 6: Commit**

```bash
git add -A design/ .claude/
git commit -m "docs(design): delete shipped/unbuilt proposals, trim TODO

llm-inference (shipped, now decisions/llm.md), ups-power (never built),
monitoring-stabilization (outcome already in the monitoring decisions).
design/CLAUDE.md folds into AGENTS.md; .claude/TODO.md is superseded by
docs/superpowers/plans/2026-09-17-agent-docs-restructure.md.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Write the real AGENTS.md

The payoff task. Everything before it was preparation.

**Files:**
- Rewrite: `AGENTS.md`

**Interfaces:**
- Consumes: every file created in Tasks 3–7; `/tmp/hard-rules-seed.md` from Task 7 Step 4.
- Produces: the only always-loaded file. Its routing table is the loading mechanism the whole design rests on.

- [ ] **Step 1: Write the six sections, in this order**

```markdown
# Homelab — agent context

**Output shape:** follow `.agents/skills/i-have-adhd/SKILL.md` for every response in this repo.

Talos Linux Kubernetes cluster on `blackcats.cc`, managed by FluxCD. Everything
is declarative and driven from git: VM templates, cluster bootstrap, service
deployment. A tiny compose remnant survives only for ZeroTier.

## Hard rules

<the ~12 invariants — see Step 2>

## Tools

All k8s tooling is managed by mise and is NOT on `PATH`:
`mise exec -- kubectl|flux|kubeseal|talosctl|talhelper|helm|kubeconform`

After editing a manifest: `.agents/scripts/validate-manifests.sh <path>`

## Network

<the 8-line IP map — see Step 3>

## Which file to read

<the routing table — see Step 4>

## Keeping docs in sync

When you change IaC, update the one file this table routes to. New gotchas go
to `design/decisions/<topic>.md`, never into this file. Only add a row here if
the rule applies repo-wide.
```

- [ ] **Step 2: Write the hard-rules block**

Seed from `/tmp/hard-rules-seed.md`. Each rule is one line: imperative, then a dash, then the consequence. Required set:

```markdown
- **Never write a raw `Secret`** — seal it: `mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < /tmp/secret.yaml > <name>-sealed.yml`
- **Never use `Ingress`** — use `HTTPRoute`/`GRPCRoute` with `parentRefs: [{name: shared, namespace: gateway}]`
- **Never `kubectl apply` a config change** — it goes through git and Flux; `kubectl` is for diagnostics only
- **Never set `spec.targetNamespace`** on a Kustomization whose resources span namespaces — it overrides every explicit namespace field
- **Never use `nfsvers=4.1`** in a static PV — the Talos kernel supports NFSv4 only
- **Never use a rolling image tag** (`latest`, `3`) — and linuxserver.io images need the FULL tag, their short `X.Y.Z` is mutable
- **Never put Python at 0-indent** inside a YAML `|` block scalar — it breaks the kustomize parser
- **Never `apk add` without `timeout 300`** in a Job or initContainer — an unbounded stall wedges the pod Running with empty logs forever
- **Never install rclone from Alpine apk** where the filen backend is needed — it needs v1.69+, from `downloads.rclone.org`
- **Never trust `optional: true` on an app-template `envFrom`** — chart 3.7.3 strips it silently
- **Never assume a Helm values path took effect** — a wrong path is a silent no-op; render or check the live object
- **Never size a workload from Goldilocks/VPA** — there is no metrics-server, so its numbers are fabricated floors
```

- [ ] **Step 3: Write the network block**

Compress the current 15-line IP map to the lines an agent actually needs:

```
172.16.20.2    Synology     NFS only          .10   API VIP
172.16.20.3    Proxmox      hypervisor        .11-.13  cp-1/2/3 (schedulable)
172.16.20.4    DGX Spark    not a k8s node    .14   llm-1 (tainted workload=llm)
172.16.20.50   Gateway VIP  pool-a ingress    .51   Plex LB    .52  minecraft-proxy LB
172.16.20.254  UDM SE       gateway + DNS
```

Keep the note that Netbird `wt0` (`100.80.x.x/16`) runs as a Talos extension on every node. Full detail lives in `design/architecture.md`.

- [ ] **Step 4: Write the routing table**

One row per terminal file. Every target must exist. Rows, in this order:

```markdown
| Your task touches...                              | Read (only this)                       |
|---------------------------------------------------|----------------------------------------|
| adding or changing a service, Flux structure       | design/docs/gitops.md                  |
| Flux Kustomizations, versions, bootstrap Jobs      | design/decisions/flux.md               |
| HTTPRoute, DNS, certs, Gateway API                 | design/docs/networking.md              |
| Cilium config, MTU, ALPN, TCPRoute                 | design/decisions/cilium-gateway.md     |
| Postgres, CNPG, DB passwords, Reflector            | design/decisions/cnpg.md               |
| PVCs, storage classes, NFS                         | design/docs/storage.md                 |
| Sealed Secrets, OIDC bootstrap secrets             | design/docs/secrets.md                 |
| a HelmRelease, app-template, Reloader              | design/decisions/helm-charts.md        |
| backup CronJobs, restic, etcd snapshots            | design/decisions/backups.md            |
| VictoriaMetrics, Grafana, VMRule, alerts           | design/decisions/monitoring.md         |
| Gotify, notifications, the telegram bridge         | design/decisions/gotify.md             |
| Trivy, Falco, kubent, kube-linter, k8s-cleaner     | design/decisions/security-tooling.md   |
| any Job, CronJob, or initContainer script          | design/decisions/jobs-and-scripts.md   |
| image tags, Renovate                               | design/decisions/images.md             |
| Zitadel, SSO, the Terraform bootstrap              | design/decisions/zitadel.md            |
| Gitea                                              | design/decisions/gitea.md              |
| FreshRSS or Paperless OIDC                         | design/decisions/oidc-apps.md          |
| Immich                                             | design/decisions/immich.md             |
| Plex                                               | design/decisions/plex.md               |
| sonarr/radarr/prowlarr/sabnzbd/seerr/suwayomi/kavita | design/decisions/media-stack.md      |
| Minecraft                                          | design/decisions/minecraft.md          |
| RomM                                               | design/decisions/romm.md               |
| Joplin                                             | design/decisions/joplin.md             |
| Homebox                                            | design/decisions/homebox.md            |
| Obsidian LiveSync                                  | design/decisions/obsidian-livesync.md  |
| Proton Mail Bridge                                 | design/decisions/protonmail-bridge.md  |
| Proxmox OIDC                                       | design/decisions/proxmox-oidc.md       |
| the LLM stack                                      | design/decisions/llm.md                |
| service inventory, hostnames, auth model           | design/docs/services.md                |
| topology, nodes, IP plan, Netbird                  | design/architecture.md                 |
| bootstrap, upgrades, recovery                      | design/runbook.md                      |
| open work, known gaps                              | design/TODO.md                         |
```

- [ ] **Step 5: Verify every routing target exists**

```bash
python3 - <<'EOF'
import re, os, sys
rows = [l for l in open('AGENTS.md') if l.startswith('|') and ('design/' in l)]
missing = []
for l in rows:
    for p in re.findall(r'design/[A-Za-z0-9_./-]+\.md', l):
        if not os.path.exists(p):
            missing.append(p)
print(f"routing rows: {len(rows)}")
print("MISSING:", missing if missing else "none")
sys.exit(1 if missing else 0)
EOF
```

Expected: `MISSING: none`, exit 0.

- [ ] **Step 6: Verify the token budget**

```bash
B=$(wc -c < AGENTS.md); echo "AGENTS.md: $B B  ~$(( B * 10 / 37 )) tokens"
test "$(( B * 10 / 37 ))" -le 2500 && echo "UNDER BUDGET" || echo "OVER BUDGET — trim routing rows, never hard rules"
```

Expected: `UNDER BUDGET` (≤ 9,250 B). If over, merge routing rows that share a target; the hard-rules block is never the thing that gets cut.

- [ ] **Step 7: Verify the symlink still resolves after the rewrite**

```bash
cmp .claude/CLAUDE.md AGENTS.md && echo "symlink intact"
```

- [ ] **Step 8: Verify no fact was lost**

```bash
bash .agents/scripts/extract-facts.sh > /tmp/facts-t8.txt
comm -23 <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) /tmp/facts-t8.txt > /tmp/dropped-t8.txt
echo "dropped: $(wc -l < /tmp/dropped-t8.txt)"; cat /tmp/dropped-t8.txt
```

Adjudicate every line.

- [ ] **Step 9: Commit**

```bash
git add AGENTS.md
git commit -m "docs(agents): rewrite AGENTS.md as hard rules plus a routing table

21,540 -> under 2,500 tokens always-loaded. Everything else is one hop
away via the routing table.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Make commands and the validation hook vendor-neutral

**Files:**
- Create: `.agents/commands/review-k8s.md`, `.agents/commands/update-docs.md` (moved)
- Create symlinks: `.claude/commands/review-k8s.md`, `.claude/commands/update-docs.md`
- Create: `.agents/scripts/validate-manifests.sh`
- Rewrite: `.claude/hooks/validate-manifests.sh` (thin wrapper)
- Modify: `opencode.json` (add `command` block)

**Interfaces:**
- Consumes: `opencode.json` from Task 2.
- Produces: `.agents/scripts/validate-manifests.sh <path>` → exits 0 on valid or skipped, non-zero on a schema violation. Referenced by `AGENTS.md`'s Tools section (written in Task 8 Step 1).

- [ ] **Step 1: Move the command bodies**

```bash
mkdir -p .agents/commands
git mv .claude/commands/review-k8s.md .agents/commands/review-k8s.md
git mv .claude/commands/update-docs.md .agents/commands/update-docs.md
ln -s ../../.agents/commands/review-k8s.md .claude/commands/review-k8s.md
ln -s ../../.agents/commands/update-docs.md .claude/commands/update-docs.md
git add .claude/commands
git ls-files -s .claude/commands   # expect mode 120000 for both
```

- [ ] **Step 2: Extract the validation logic into a path-taking script**

Create `.agents/scripts/validate-manifests.sh`:

```bash
#!/usr/bin/env bash
# Validate one kubernetes manifest against upstream + CRD schemas.
# Usage: .agents/scripts/validate-manifests.sh <file>
# Exits 0 if valid or not a kubernetes manifest; non-zero on a schema violation.
set -euo pipefail

FILE="${1:-}"
[[ -z "$FILE" ]] && { echo "usage: $0 <file>" >&2; exit 2; }
[[ ! -f "$FILE" ]] && exit 0
case "$FILE" in
  */kubernetes/*.yml|*/kubernetes/*.yaml|kubernetes/*.yml|kubernetes/*.yaml) ;;
  *) exit 0 ;;
esac
command -v mise >/dev/null 2>&1 || exit 0

mise exec -- kubeconform \
  -strict \
  -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  "$FILE"
```

Then `chmod +x .agents/scripts/validate-manifests.sh`.

Two behaviour changes from the old hook, both deliberate: the path check happens **before** any interpreter is spawned, and the exit status is now real instead of swallowed by `|| true`.

- [ ] **Step 3: Test the script directly, both outcomes**

```bash
# a known-good manifest
GOOD=$(git ls-files 'kubernetes/apps/*/*/app/*.yml' | head -1); echo "good: $GOOD"
bash .agents/scripts/validate-manifests.sh "$GOOD"; echo "exit=$?"

# a known-bad manifest
mkdir -p /tmp/kubernetes && cat > /tmp/kubernetes/bad.yml <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: bad
data: "this must be a map, not a string"
EOF
bash .agents/scripts/validate-manifests.sh /tmp/kubernetes/bad.yml; echo "exit=$?"

# a non-kubernetes file is skipped
bash .agents/scripts/validate-manifests.sh README.md; echo "exit=$? (expect 0)"
```

Expected: good → `exit=0`; bad → non-zero exit with a kubeconform message; `README.md` → `exit=0`.

- [ ] **Step 4: Reduce the Claude hook to a wrapper**

Rewrite `.claude/hooks/validate-manifests.sh`:

```bash
#!/usr/bin/env bash
# Claude Code PostToolUse wrapper: parse file_path from stdin JSON, delegate.
# The real logic lives in .agents/scripts/validate-manifests.sh so any harness
# (or a human) can run it without a hook.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILE_PATH=$(python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || true)

[[ -z "$FILE_PATH" ]] && exit 0
case "$FILE_PATH" in
  */kubernetes/*.yml|*/kubernetes/*.yaml) ;;
  *) exit 0 ;;
esac

# Silent on success; only a failure is worth transcript space.
"$REPO/.agents/scripts/validate-manifests.sh" "$FILE_PATH" || {
  echo "kubeconform FAILED: $FILE_PATH"
  exit 0   # report, never block the edit
}
```

The old version echoed `kubeconform: <path>` and `OK` on every Write/Edit. Silence on success sharpens the signal when it fails.

- [ ] **Step 5: Test the hook wrapper with a simulated payload**

```bash
echo '{"tool_input":{"file_path":"/tmp/kubernetes/bad.yml"}}' \
  | bash .claude/hooks/validate-manifests.sh; echo "exit=$? (expect 0)"
echo '{"tool_input":{"file_path":"README.md"}}' \
  | bash .claude/hooks/validate-manifests.sh; echo "exit=$? (expect 0, no output)"
```

Expected: the first prints `kubeconform FAILED: /tmp/kubernetes/bad.yml` and exits 0; the second prints nothing and exits 0.

- [ ] **Step 6: Add the command block to opencode.json**

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["AGENTS.md"],
  "skills": {
    "paths": [".agents/skills"]
  },
  "command": {
    "review-k8s": {
      "description": "Review kubernetes manifests for repo-specific mistakes",
      "template": "{file:.agents/commands/review-k8s.md}"
    },
    "update-docs": {
      "description": "Sync design/ with the actual state of kubernetes/",
      "template": "{file:.agents/commands/update-docs.md}"
    }
  }
}
```

- [ ] **Step 7: Re-validate opencode.json against the schema**

```bash
python3 - <<'EOF'
import json
cfg = json.load(open('opencode.json'))
sch = json.load(open('/tmp/oc-schema.json'))
props = sch['$defs']['Config']['properties']
for k in cfg:
    if k == '$schema': continue
    assert k in props, f"unknown key: {k}"
cmd = props['command']['additionalProperties']
for name, body in cfg['command'].items():
    assert 'template' in body, f"{name}: template is required"
    for k in body:
        assert k in cmd['properties'], f"{name}: unknown command key {k}"
print("opencode.json OK —", sorted(cfg['command']))
EOF
```

Expected: `opencode.json OK — ['review-k8s', 'update-docs']`.

- [ ] **Step 8: Commit**

```bash
git add .agents/ .claude/ opencode.json
git commit -m "chore(agents): make commands and manifest validation vendor-neutral

Command bodies live in .agents/commands/ with .claude/commands/ symlinks
and an opencode command block pointing at the same files. Validation logic
moves to .agents/scripts/validate-manifests.sh taking a path, so any
harness can run it; the Claude hook is now a thin JSON-parsing wrapper
that is silent on success.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: PreToolUse hook that injects the right decision doc

Deterministic loading for Claude Code, layered on top of the routing table rather than replacing it. The table remains the mechanism everywhere else.

**Files:**
- Create: `.agents/scripts/route-context.sh`
- Create: `.claude/hooks/inject-context.sh`
- Modify: `.claude/settings.local.json`

**Interfaces:**
- Consumes: the routing table written in Task 8 Step 4; the decision files from Tasks 3–7.
- Produces: `.agents/scripts/route-context.sh <path>` → prints the matching `design/` file paths, one per line, exit 0 (prints nothing when no rule matches).

- [ ] **Step 1: Write the router**

Create `.agents/scripts/route-context.sh`:

```bash
#!/usr/bin/env bash
# Map a repo path to the design doc(s) that must be read before editing it.
# Usage: .agents/scripts/route-context.sh <path>
# Prints matching design/ paths one per line. Prints nothing if no rule matches.
set -euo pipefail

P="${1:-}"
[[ -z "$P" ]] && exit 0

emit() { [[ -f "$1" ]] && echo "$1"; }

case "$P" in
  */kubernetes/apps/media/minecraft*|kubernetes/apps/media/minecraft*) emit design/decisions/minecraft.md ;;
  */kubernetes/apps/media/plex*|kubernetes/apps/media/plex*)           emit design/decisions/plex.md ;;
  */kubernetes/apps/media/romm*|kubernetes/apps/media/romm*)           emit design/decisions/romm.md ;;
  */kubernetes/apps/media/*|kubernetes/apps/media/*)                   emit design/decisions/media-stack.md ;;
  */kubernetes/apps/gitea/*|kubernetes/apps/gitea/*)                   emit design/decisions/gitea.md ;;
  */kubernetes/apps/auth/*|kubernetes/apps/auth/*)                     emit design/decisions/zitadel.md ;;
  */kubernetes/apps/immich/*|kubernetes/apps/immich/*)                 emit design/decisions/immich.md ;;
  */kubernetes/apps/joplin/*|kubernetes/apps/joplin/*)                 emit design/decisions/joplin.md ;;
  */kubernetes/apps/homebox/*|kubernetes/apps/homebox/*)               emit design/decisions/homebox.md ;;
  */kubernetes/apps/obsidian/*|kubernetes/apps/obsidian/*)             emit design/decisions/obsidian-livesync.md ;;
  */kubernetes/apps/paperless/*|kubernetes/apps/paperless/*)           emit design/decisions/oidc-apps.md
                                                                       emit design/decisions/protonmail-bridge.md ;;
  */kubernetes/apps/freshrss/*|kubernetes/apps/freshrss/*)             emit design/decisions/oidc-apps.md ;;
  */kubernetes/apps/postgres/*|kubernetes/apps/postgres/*)             emit design/decisions/cnpg.md ;;
  */kubernetes/apps/ai/*|kubernetes/apps/ai/*)                         emit design/decisions/llm.md ;;
  */kubernetes/apps/security/*|kubernetes/apps/security/*)             emit design/decisions/security-tooling.md ;;
  */kubernetes/apps/monitoring/gotify*|kubernetes/apps/monitoring/gotify*) emit design/decisions/gotify.md ;;
  */kubernetes/apps/monitoring/*|kubernetes/apps/monitoring/*)         emit design/decisions/monitoring.md ;;
  */kubernetes/apps/kube-system/cilium/*|kubernetes/apps/kube-system/cilium/*) emit design/decisions/cilium-gateway.md ;;
  */kubernetes/apps/gateway/*|kubernetes/apps/gateway/*)               emit design/decisions/cilium-gateway.md ;;
  */kubernetes/flux/*|kubernetes/flux/*)                               emit design/decisions/flux.md ;;
  */ks.yml|ks.yml)                                                     emit design/decisions/flux.md ;;
  */kubernetes/talos/*|kubernetes/talos/*)                             emit design/architecture.md ;;
esac

# Any backup job, wherever it lives.
case "$P" in */backup/*|*backup*.yml) emit design/decisions/backups.md ;; esac
```

Then `chmod +x .agents/scripts/route-context.sh`.

- [ ] **Step 2: Test the router against real repo paths**

```bash
for p in kubernetes/apps/media/minecraft/app/pvc.yml \
         kubernetes/apps/media/sonarr/app/helmrelease.yml \
         kubernetes/apps/gitea/gitea/app/tcproute.yml \
         kubernetes/apps/postgres/cluster/app/cluster.yml \
         kubernetes/apps/immich/immich/backup/cronjob.yml \
         kubernetes/flux/config/flux.yml \
         README.md ; do
  printf '%-58s -> %s\n' "$p" "$(bash .agents/scripts/route-context.sh "$p" | tr '\n' ' ')"
done
```

Expected: minecraft → `design/decisions/minecraft.md`; sonarr → `media-stack.md`; gitea → `gitea.md`; postgres → `cnpg.md`; the immich backup path → both `immich.md`-adjacent routing and `backups.md`; flux → `flux.md`; `README.md` → empty.

If a case arm prints nothing because the target file does not exist, `emit` is doing its job — but confirm the file genuinely should not exist rather than a typo in the path.

- [ ] **Step 3: Write the PreToolUse hook**

Create `.claude/hooks/inject-context.sh`:

```bash
#!/usr/bin/env bash
# Claude Code PreToolUse: before an Edit/Write under kubernetes/, print the
# decision doc that governs that path so it enters context automatically.
# Vendor-specific convenience; the AGENTS.md routing table is the portable path.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILE_PATH=$(python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || true)

[[ -z "$FILE_PATH" ]] && exit 0

mapfile -t DOCS < <("$REPO/.agents/scripts/route-context.sh" "$FILE_PATH" || true)
[[ ${#DOCS[@]} -eq 0 ]] && exit 0

for d in "${DOCS[@]}"; do
  echo "=== required reading for $FILE_PATH: $d ==="
  cat "$REPO/$d"
done
```

Then `chmod +x .claude/hooks/inject-context.sh`.

- [ ] **Step 4: Test the hook with a simulated payload**

```bash
echo '{"tool_input":{"file_path":"kubernetes/apps/media/minecraft/app/pvc.yml"}}' \
  | bash .claude/hooks/inject-context.sh | head -5
echo "---"
echo '{"tool_input":{"file_path":"README.md"}}' \
  | bash .claude/hooks/inject-context.sh; echo "exit=$? (expect 0, no output)"
```

Expected: the first prints the `required reading` header followed by the minecraft decision file; the second prints nothing.

- [ ] **Step 5: Register the hook in `.claude/settings.local.json`**

Add a `PreToolUse` entry alongside the existing `PostToolUse` one. Do not touch the `env` or `permissions` blocks.

```json
"PreToolUse": [
  {
    "matcher": "Write|Edit",
    "hooks": [
      {
        "type": "command",
        "command": "bash /home/void/repos/Homelab/.claude/hooks/inject-context.sh",
        "timeout": 10,
        "statusMessage": "Loading design context..."
      }
    ]
  }
]
```

- [ ] **Step 6: Verify the settings file still parses**

```bash
python3 -c "import json; d=json.load(open('.claude/settings.local.json')); print('hooks:', sorted(d['hooks']))"
```

Expected: `hooks: ['PostToolUse', 'PreToolUse']`.

- [ ] **Step 7: Commit**

`.claude/settings.local.json` is gitignored (it holds a token) — it is configured but never committed. Commit only the scripts.

```bash
git add .agents/scripts/route-context.sh .claude/hooks/inject-context.sh
git status --porcelain .claude/settings.local.json   # expect empty: gitignored
git commit -m "feat(agents): inject the governing decision doc on manifest edits

route-context.sh maps a repo path to the design docs that govern it; the
Claude PreToolUse hook prints them before an Edit/Write under kubernetes/.
The AGENTS.md routing table remains the portable mechanism.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Growth ratchet

Without this, `AGENTS.md` regrows to 80 KB. The old file grew ~1.2 KB/day with no pruning path.

**Files:**
- Modify: `.agents/commands/update-docs.md`

**Interfaces:**
- Consumes: the routing table from Task 8; the command location from Task 9.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Rewrite the update-docs source-of-truth table**

Its current table points at `design/AI_CONTEXT.md`, `design/ARCHITECTURE.md` and `.claude/CLAUDE.md`, none of which exist any more. Repoint every row at the files that now hold that content, matching the Task 8 routing table exactly.

- [ ] **Step 2: Add the ratchet section verbatim**

```markdown
## Where a new fact goes

1. A gotcha about ONE service or subsystem → `design/decisions/<topic>.md`. Never `AGENTS.md`.
2. A rule that applies repo-wide, in every namespace → a one-line entry in the
   `AGENTS.md` hard-rules block. Nowhere else.
3. A new terminal doc → one row in the `AGENTS.md` routing table. Never a second index.

`AGENTS.md` has a hard budget of 2,500 tokens. Check before committing:

```bash
B=$(wc -c < AGENTS.md); echo "$B B ~$(( B * 10 / 37 )) tok"
test "$(( B * 10 / 37 ))" -le 2500 || echo "OVER BUDGET — move content to design/decisions/"
```

Cut a routing row before cutting a hard rule. Never record a deployed version
number: the manifest is the source of truth. Versions belong here only as a
constraint, a known-bad minimum, or a recorded incident.
```

- [ ] **Step 3: Verify the command's targets all exist**

```bash
python3 - <<'EOF'
import re, os
t = open('.agents/commands/update-docs.md').read()
missing = sorted({p for p in re.findall(r'design/[A-Za-z0-9_./-]+\.md', t) if not os.path.exists(p)})
print("MISSING:", missing if missing else "none")
EOF
grep -n 'AI_CONTEXT\|ARCHITECTURE.md\|\.claude/CLAUDE.md' .agents/commands/update-docs.md || echo "no stale references"
```

Expected: `MISSING: none` and `no stale references`.

- [ ] **Step 4: Commit**

```bash
git add .agents/commands/update-docs.md
git commit -m "docs(commands): repoint update-docs and add the AGENTS.md size ratchet

New gotchas go to design/decisions/; only repo-wide rules reach AGENTS.md,
which now has a checkable 2,500-token budget.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 12: Deduplicate the memory store

`MEMORY.md` is loaded into context every session. Nine of its entries restate a rule that is now in a committed file, so the same fact has two update paths and will drift.

**Files:**
- Delete: nine files under `/home/void/.claude/projects/-home-void-repos-Homelab/memory/`
- Modify: `/home/void/.claude/projects/-home-void-repos-Homelab/memory/MEMORY.md`

**Interfaces:**
- Consumes: the committed rules from Tasks 3–8.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: List what is there and confirm each duplicate**

```bash
M=/home/void/.claude/projects/-home-void-repos-Homelab/memory
ls "$M"; echo "--- index ---"; wc -l "$M/MEMORY.md"
```

- [ ] **Step 2: Delete the entries now covered by a committed file**

Each of these restates a rule that lives in `AGENTS.md`'s hard-rules block or a `design/decisions/` file. Repo-specific technical rules belong in the committed file only; memory keeps cross-project preferences.

```bash
M=/home/void/.claude/projects/-home-void-repos-Homelab/memory
rm -f "$M"/feedback_flux_target_namespace.md \
      "$M"/feedback_talos_static_nfs_nfsvers.md \
      "$M"/feedback_rclone_filen_binary.md \
      "$M"/feedback_yaml_block_scalar_python.md \
      "$M"/feedback_apptemplate_single_controller_naming.md \
      "$M"/feedback_allauth65_callback_path.md \
      "$M"/feedback_paperless_apps_env.md \
      "$M"/feedback_backup_tools_no_jq.md \
      "$M"/feedback_claude_md_no_versions.md
ls "$M"
```

Kept deliberately: `feedback_flux_only.md`, `feedback_mise_exec_k8s_tools.md`, `feedback_alpine_apk_needs_root.md`, `feedback_zitadel_bootstrap_apply_k.md`, `feedback_talos_kubelet_mountns.md`, and every `project_*` / `ops_*` entry — those carry context that is not in the repo.

- [ ] **Step 3: Remove the matching index lines from MEMORY.md**

Delete exactly the nine `- [...](...)` lines pointing at the removed files. Leave every other line untouched.

- [ ] **Step 4: Verify the index has no dangling links**

```bash
M=/home/void/.claude/projects/-home-void-repos-Homelab/memory
python3 - <<EOF
import re, os
M = "$M"
t = open(os.path.join(M, "MEMORY.md")).read()
links = re.findall(r'\]\(([^)]+\.md)\)', t)
missing = [l for l in links if not os.path.exists(os.path.join(M, l))]
print(f"links: {len(links)}  missing: {missing if missing else 'none'}")
EOF
```

Expected: `missing: none`.

- [ ] **Step 5: No commit**

The memory store is outside the repo and is not version-controlled. Report the before/after entry count instead.

---

### Task 13: Final verification against the spec's acceptance criteria

**Files:**
- Create: `docs/superpowers/specs/2026-09-17-facts-after.txt`
- Create: `docs/superpowers/specs/2026-09-17-facts-dropped.txt`

**Interfaces:**
- Consumes: everything.
- Produces: the evidence that the acceptance criteria hold.

- [ ] **Step 1: Capture the final fact set and the full drop list**

```bash
bash .agents/scripts/extract-facts.sh > docs/superpowers/specs/2026-09-17-facts-after.txt
comm -23 <(grep -v '^#' docs/superpowers/specs/2026-09-17-facts-before.txt) \
         docs/superpowers/specs/2026-09-17-facts-after.txt \
         > docs/superpowers/specs/2026-09-17-facts-dropped.txt
printf 'before: %s\nafter:  %s\ndropped:%s\n' \
  "$(grep -vc '^#' docs/superpowers/specs/2026-09-17-facts-before.txt)" \
  "$(wc -l < docs/superpowers/specs/2026-09-17-facts-after.txt)" \
  "$(wc -l < docs/superpowers/specs/2026-09-17-facts-dropped.txt)"
```

- [ ] **Step 2: Adjudicate every dropped fact**

Read `2026-09-17-facts-dropped.txt` line by line. Annotate the file in place, one tag per line:

```
--force                     INTENTIONAL: narrative, never a rule in this repo
2026-08-28                  INTENTIONAL: date, prune rule
GOTIFY_DEFAULTUSER_PASS     LOAD-BEARING: restore to design/decisions/gotify.md
```

Restore every `LOAD-BEARING` line into the correct file, then re-run Step 1. Repeat until zero `LOAD-BEARING` lines remain. Report the final count of intentional drops to the user — do not silently accept any.

- [ ] **Step 3: Run the full acceptance checklist**

```bash
echo "== 1. token budget =="
B=$(wc -c < AGENTS.md); echo "AGENTS.md $B B ~$(( B * 10 / 37 )) tok (limit 2500)"
test "$(( B * 10 / 37 ))" -le 2500 && echo PASS || echo FAIL

echo "== 2. routing targets exist =="
python3 - <<'EOF'
import re, os
rows = [l for l in open('AGENTS.md') if l.startswith('|') and 'design/' in l]
miss = sorted({p for l in rows for p in re.findall(r'design/[A-Za-z0-9_./-]+\.md', l) if not os.path.exists(p)})
print(f"rows={len(rows)} missing={miss or 'none'}", "PASS" if not miss else "FAIL")
EOF

echo "== 3. symlinks resolve =="
cmp .claude/CLAUDE.md AGENTS.md && test -f .claude/skills/i-have-adhd/SKILL.md \
  && test -f .claude/commands/review-k8s.md && echo PASS || echo FAIL

echo "== 4. opencode.json valid =="
python3 - <<'EOF'
import json
cfg=json.load(open('opencode.json')); sch=json.load(open('/tmp/oc-schema.json'))
props=sch['$defs']['Config']['properties']
bad=[k for k in cfg if k!='$schema' and k not in props]
print("unknown keys:", bad or "none", "PASS" if not bad else "FAIL")
EOF

echo "== 5. validation script works standalone =="
bash .agents/scripts/validate-manifests.sh README.md && echo "PASS (skips non-k8s)" || echo FAIL

echo "== 6. no infra files touched =="
git diff --name-only 4c36fb3..HEAD | grep -E '^(kubernetes|infra|\.github)/' \
  && echo "FAIL — infra modified" || echo "PASS (no infra changes)"

echo "== 7. every design file fits a 32k window =="
find design -name '*.md' -size +8k -printf '%s\t%p\n' | sort -rn \
  && echo "(files above 8k listed; runbook.md and architecture.md may legitimately exceed)"

echo "== 8. no dangling doc references =="
grep -rn 'AI_CONTEXT\|design/ARCHITECTURE.md\|design/RUNBOOK.md\|design/CLAUDE.md\|llm-deployment\|llm-inference\|ups-power\|monitoring-stabilization' \
  AGENTS.md design/ .agents/ 2>/dev/null && echo "FAIL — stale reference" || echo PASS
```

Every numbered check must print PASS. Check 7 is informational: `runbook.md` and `architecture.md` are allowed to exceed 8 KB because they are read deliberately rather than auto-injected; any `design/decisions/*.md` over 8 KB must be split.

- [ ] **Step 4: Report the headline numbers**

```bash
printf 'always-loaded: 79701 B (~21540 tok) -> %s B (~%s tok)\n' \
  "$(wc -c < AGENTS.md)" "$(( $(wc -c < AGENTS.md) * 10 / 37 ))"
printf 'design/ total: %s B -> %s B\n' \
  "$(git show 4c36fb3:design/TODO.md | wc -c)" "$(cat design/*.md design/*/*.md | wc -c)"
printf 'local 32k window now %s%% consumed by always-on context\n' \
  "$(( $(wc -c < AGENTS.md) * 10 / 37 * 100 / 32768 ))"
```

- [ ] **Step 5: Commit the evidence**

```bash
git add docs/superpowers/specs/2026-09-17-facts-after.txt \
        docs/superpowers/specs/2026-09-17-facts-dropped.txt
git commit -m "docs: record post-prune fact diff

Every dropped string adjudicated as intentional; load-bearing strings
restored. Evidence for the spec's acceptance criteria.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Manual smoke test — ask the user to run one session in each harness**

This is the only check a script cannot do. Ask the user to:

1. Open Claude Code in this repo and confirm the first response follows the i-have-adhd shape (no preamble, action first).
2. Open opencode with `local-fast` and ask "what do I need to know before editing the minecraft PVC?" — it should read `design/decisions/minecraft.md` and nothing else.
3. Confirm the opencode session's context usage leaves room to work.

Report the outcome. A failure here means the routing table wording needs to be more directive, not that the architecture is wrong.
