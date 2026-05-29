# review-k8s

Review kubernetes manifests in `$ARGUMENTS` (defaults to all recently changed files under `kubernetes/`) for common mistakes specific to this repo.

Run this before pushing any new service or after refactoring manifests.

## Discovery

If `$ARGUMENTS` is a path (e.g. `kubernetes/apps/myapp`), scan that directory.
Otherwise, scan all files changed relative to `main`:
```
git diff --name-only main | grep '^kubernetes/'
```

Read each relevant YAML file. Focus on: `ks.yml`, HelmRelease, HTTPRoute, PVC/PV, SealedSecret, Kustomization manifests.

## Checklist

For each file found, check the following rules. Report violations as `FAIL: <file>:<line> — <reason>`. Report clean files as `OK: <file>`.

### Secrets
- [ ] No `kind: Secret` in any committed file — only `kind: SealedSecret` or bootstrap Jobs (`kind: Job`) are allowed
- [ ] No plaintext passwords, tokens, or keys in any manifest value

### Ingress / Routing
- [ ] No `kind: Ingress` — must use `kind: HTTPRoute` or `kind: GRPCRoute`
- [ ] Every `HTTPRoute` and `GRPCRoute` has `parentRefs: [{name: shared, namespace: gateway}]`
- [ ] Every `HTTPRoute`/`GRPCRoute` that needs a DNS record has annotation `external-dns.alpha.kubernetes.io/enabled: "true"`
- [ ] HTTPRoute `backendRef.name` matches the actual Service name:
  - Single controller named `app` → Service is `{release-name}` (NOT `{release-name}-app`)
  - Two+ controllers → Service is `{release-name}-{controller-name}`
  - Gitea exception: HTTP service is `{release-name}-http` port 3000

### Flux Kustomizations (`ks.yml`)
- [ ] `spec.targetNamespace` is NOT set if any resource in the path spans multiple namespaces (e.g. cross-namespace RBAC Roles/RoleBindings)
- [ ] `dependsOn` includes `sealed-secrets` for any app that uses SealedSecrets
- [ ] `dependsOn` includes `shared-gateway` for any app that has an HTTPRoute
- [ ] `dependsOn` includes `postgres-cluster` for any app that has a CNPG database Kustomization
- [ ] `dependsOn` includes `zitadel-bootstrap` for any app that needs OIDC credentials written by Terraform

### Storage
- [ ] Static NFS `PersistentVolume` objects use `nfsvers=4` in `mountOptions`, never `nfsvers=4.1`
- [ ] `StorageClassName` is one of: `nfs-client` (NFS subdirectory, default), `openebs-hostpath` (local node), or blank (static PV binding)

### Image tags
- [ ] No rolling major-only tags (e.g. `nginx:3`, `redis:latest`) — must be minor semver or exact (e.g. `3.7`, `3.7.4`, `sha256:...`)
- [ ] No `latest` tag

### YAML structure
- [ ] No Python (or other multi-line code) at 0-indent inside a YAML `|` block scalar — this breaks the kustomize parser

### HelmRelease
- [ ] Chart versions are pinned to minor semver (e.g. `3.7.*`) or exact — no `>=` ranges or `*` alone

## Output

List all violations first (grouped by file), then list all clean files. End with a one-line summary:
- `All clear — N files checked, 0 violations` 
- or `N violations found across M files — fix before pushing`
