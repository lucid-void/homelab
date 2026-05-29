# update-docs

Keep `design/` and `.claude/CLAUDE.md` in sync with the actual state of `kubernetes/`.

Run this after making changes to kubernetes manifests, or to audit documentation drift.

## What to update

| Source of truth | Design files to keep in sync |
|---|---|
| `kubernetes/apps/` manifests | `design/docs/services.md`, `design/docs/gitops.md` |
| `kubernetes/apps/kube-system/cilium/` | `design/docs/networking.md`, `design/AI_CONTEXT.md` |
| `kubernetes/apps/gateway/` | `design/docs/networking.md`, `design/AI_CONTEXT.md` |
| `kubernetes/apps/cert-manager/`, `kubernetes/apps/network/` | `design/docs/networking.md` |
| `kubernetes/apps/*/backup/` | `design/ARCHITECTURE.md`, `design/AI_CONTEXT.md` |
| `kubernetes/apps/postgres/`, `kubernetes/apps/*/database/` | `design/docs/storage.md`, `design/AI_CONTEXT.md` |
| `kubernetes/apps/democratic-csi/`, `kubernetes/apps/openebs/` | `design/docs/storage.md` |
| `kubernetes/talos/talconfig.yaml` | `design/ARCHITECTURE.md`, `design/AI_CONTEXT.md` |
| `kubernetes/flux/` | `design/docs/gitops.md` |
| Any new service namespace | `design/docs/services.md` |
| Any new key decision or gotcha | `design/AI_CONTEXT.md` (Non-obvious Decisions), `.claude/CLAUDE.md` (key decisions table) |

## Process

### 1. Identify what changed (if $ARGUMENTS is set, focus there; otherwise do a full pass)

If `$ARGUMENTS` is provided, treat it as a hint about what changed (e.g. "added sonarr postgres", "upgraded cilium", "new backup job"). Scope the discovery pass accordingly.

Otherwise, do a full pass:

- Read `kubernetes/talos/talconfig.yaml` — check Talos/k8s versions, extensions, node IPs, patches
- Read `kubernetes/apps/kube-system/cilium/app/helm-values.yml` and `kubernetes/apps/kube-system/cilium/config/cilium-l2.yml` — check Cilium config and L2 pools
- Read `kubernetes/apps/gateway/shared-gateway/config/gateway.yml` — check Gateway IP and listeners
- Read all `kubernetes/apps/*/ks.yml` files — inventory all Kustomization names and their `dependsOn` chains
- Read `kubernetes/apps/*/namespace.yml` — inventory all namespaces
- For each app namespace, read the HelmRelease and HTTPRoute to get the hostname

### 2. Compare against design files

Read the relevant `design/` files and identify:
- Services listed in `design/docs/services.md` that no longer exist in manifests → **remove or flag**
- Services in manifests not listed in `design/docs/services.md` → **add**
- Hostnames, auth methods, or storage that differ from what's documented → **update**
- IP addresses, versions, or config values that have changed → **update**
- New non-obvious decisions or gotchas discovered during work → **add to AI_CONTEXT.md**

### 3. Update design files

Edit only the files that have actual changes. For each changed file:
- Make targeted edits — don't rewrite sections that are still accurate
- Keep the same structure and format
- Update version numbers, IPs, service rows, and decision entries in-place

### 4. Update .claude/CLAUDE.md if needed

Update `.claude/CLAUDE.md` only if:
- The design specs table needs a new entry
- A new "what not to do" rule was identified
- A key decision changed that Claude would get wrong without knowing

### 5. Update design/TODO.md

- Mark items as done if they've been implemented
- Add new known gaps discovered during the pass

## Output

Report what was changed: which files were updated and what specifically changed in each. If nothing was out of sync, say so explicitly.
