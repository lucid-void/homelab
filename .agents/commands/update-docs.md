# update-docs

Keep `design/` and `AGENTS.md` in sync with the actual state of `kubernetes/`.

Run this after making changes to kubernetes manifests, or to audit documentation drift.

## What to update

| Source of truth | Design files to keep in sync |
|---|---|
| `kubernetes/apps/` manifests | `design/docs/services.md`, `design/docs/gitops.md` |
| `kubernetes/apps/kube-system/cilium/` | `design/docs/networking.md`, `design/decisions/cilium-gateway.md` |
| `kubernetes/apps/gateway/` | `design/docs/networking.md` |
| `kubernetes/apps/cert-manager/`, `kubernetes/apps/network/` | `design/docs/networking.md` |
| `kubernetes/apps/*/backup/` | `design/decisions/backups.md` |
| `kubernetes/apps/postgres/`, `kubernetes/apps/*/database/` | `design/docs/storage.md`, `design/decisions/cnpg.md` |
| `kubernetes/apps/democratic-csi/`, `kubernetes/apps/openebs/` | `design/docs/storage.md` |
| `kubernetes/talos/talconfig.yaml` | `design/architecture.md` |
| `kubernetes/flux/` | `design/docs/gitops.md`, `design/decisions/flux.md` |
| A service listed in the `AGENTS.md` routing table | its `design/decisions/<topic>.md` row |
| Any new service namespace | `design/docs/services.md` **and** a new routing row in `AGENTS.md` (see below) |
| Any new key decision or gotcha | see "Where a new fact goes" below |

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
- New non-obvious decisions or gotchas discovered during work → route per "Where a new fact goes" below

### 3. Update design files

Edit only the files that have actual changes. For each changed file:
- Make targeted edits — don't rewrite sections that are still accurate
- Keep the same structure and format
- Update version numbers, IPs, service rows, and decision entries in-place

### 4. Update AGENTS.md if needed

Update `AGENTS.md` only if:
- A new terminal `design/` file needs a routing-table row
- A new hard rule applies repo-wide, in every namespace
- A rule that used to be repo-wide got scoped to one service (demote it to a routing row)

Never add a gotcha's body to `AGENTS.md` — see "Where a new fact goes" below.

### 5. Update design/TODO.md

- Mark items as done if they've been implemented
- Add new known gaps discovered during the pass

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

A `design/decisions/*.md` file over 8,192 bytes should be split, and the
`AGENTS.md` routing table updated to reach both halves — a row points at a
file, it never carries the body.

Adding a new service means adding its routing row in the same change, not later.

## Output

Report what was changed: which files were updated and what specifically changed in each. If nothing was out of sync, say so explicitly.
