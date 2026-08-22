# GitOps — FluxCD

## Overview

FluxCD continuously reconciles cluster state from the `kubernetes/` directory in the mono-repo.
Every change to the cluster goes through git. No ad-hoc `kubectl apply` for config changes.

## Repository Layout

```
kubernetes/
├── bootstrap/
│   ├── helmfile.yml              # Pre-Flux bootstrap: Cilium, Sealed Secrets, Spegel, prometheus-operator-crds
│   └── flux/
│       ├── kustomization.yml     # kubectl apply -k entrypoint
│       └── github-deploy-key.sealed.yml
├── flux/
│   ├── config/
│   │   ├── cluster.yml           # GitRepository + root cluster Kustomization
│   │   └── flux.yml              # flux-system Kustomization
│   ├── apps.yml                  # Root apps Kustomization → kubernetes/apps/
│   ├── repositories/
│   │   ├── helm/                 # HelmRepository sources (one file per chart registry)
│   │   ├── git/                  # GitRepository sources (gateway-api)
│   │   └── oci/                  # OCI repository sources (k8s-cleaner)
│   ├── vars/
│   │   ├── cluster-settings.yml  # ConfigMap: CLUSTER_NAME, CLUSTER_DOMAIN
│   │   └── cluster-secrets.sealed.yml  # SealedSecret: SECRET_CLOUDFLARE_EMAIL, etc.
│   └── pub-cert.pem              # Sealed Secrets public cert (safe to commit)
├── apps/
│   ├── <namespace>/
│   │   ├── namespace.yml         # Namespace resource
│   │   ├── kustomization.yml     # Kustomize root listing all ks.yml files
│   │   └── <app>/
│   │       ├── ks.yml            # Flux Kustomization (dependsOn, path, targetNamespace)
│   │       └── app/              # Actual manifests: HelmRelease, SealedSecret, PVC, HTTPRoute, …
│   └── …
└── talos/
    ├── talconfig.yaml            # Node definitions, cluster settings, extensions
    ├── talsecret.sops.yaml       # Cluster PKI + join tokens (SOPS-encrypted)
    └── clusterconfig/            # Generated per-node configs (gitignored)
```

## Flux Reconciliation Model

### Entry Points

Flux starts from two root Kustomizations (applied via `kubectl apply -k kubernetes/bootstrap/flux` during bootstrap):

1. **`cluster`** → `kubernetes/flux/` — loads repositories, vars, and `apps.yml`
2. **`cluster-apps`** → `kubernetes/apps/` — root of all application Kustomizations

Both use `postBuild.substituteFrom` to inject `cluster-settings` ConfigMap and `cluster-secrets` Secret variables into all manifests (e.g. `${CLUSTER_DOMAIN}` → `blackcats.cc`).

### Kustomization Tree

Within `kubernetes/apps/`, each namespace has a top-level `kustomization.yml` that lists its `ks.yml` files. Each `ks.yml` defines an individual Flux `Kustomization` resource with `dependsOn` ordering.

**Dependency chain (simplified):**

```
gateway-api
  └── cilium
        └── cilium-config (L2 pools)
        └── sealed-secrets
              └── cert-manager
                    └── cert-manager-config (ClusterIssuer)
              └── cnpg
                    └── postgres-cluster
                          └── immich-database ──→ immich
                          └── auth-database ───→ zitadel ──→ zitadel-bootstrap
                                                                └── immich
                                                                └── paperless
                                                                └── gitea
                                                                └── freshrss
              └── democratic-csi
              └── openebs
              └── reflector
              └── shared-gateway
                    └── immich
                    └── paperless
                    └── gitea
                    └── …all app routes
```

Full dependency details are in each `ks.yml`. A Kustomization waits for all its `dependsOn` entries to be `Ready` before applying.

### Kustomization Naming Convention

Flux Kustomization names are globally unique within `flux-system`. Naming scheme:

| Pattern | Examples |
|---|---|
| `<app>` | `cilium`, `immich`, `zitadel`, `gotify` |
| `<app>-config` | `cilium-config`, `cert-manager-config` |
| `<app>-database` | `immich-database`, `auth-database`, `gitea-database` |
| `<app>-backup` | `immich-backup`, `postgres-backup` |
| `<app>-bootstrap` | `zitadel-bootstrap`, `gotify-bootstrap`, `gitea-db-bootstrap` |

### targetNamespace

Most Kustomizations set `spec.targetNamespace`. **Critical:** `targetNamespace` overrides the namespace on ALL resources in the path, including those with explicit `metadata.namespace`. Only use `targetNamespace` when every resource in the path belongs to that one namespace. Cross-namespace RBAC needs its own Kustomization without `targetNamespace` (see `kubernetes/apps/auth/bootstrap-rbac/`).

### Variable Substitution

Variables are substituted in all manifests via `postBuild.substituteFrom`:

| Variable | Source | Value |
|---|---|---|
| `${CLUSTER_NAME}` | `cluster-settings` ConfigMap | `homelab-k8s` |
| `${CLUSTER_DOMAIN}` | `cluster-settings` ConfigMap | `blackcats.cc` |
| `${SECRET_CLOUDFLARE_EMAIL}` | `cluster-secrets` SealedSecret | Cloudflare account email |

`cluster-secrets` is `optional: true` — reconciliation proceeds even if the secret is absent.

---

## Helm Repositories

All HelmRepositories live in `kubernetes/flux/repositories/helm/`. One file per chart registry. To add a new one:

```yaml
# kubernetes/flux/repositories/helm/myregistry.yml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: myregistry
  namespace: flux-system
spec:
  interval: 12h
  url: https://charts.myregistry.io
```

Then add it to `kubernetes/flux/repositories/helm/kustomization.yml`.

---

## HelmRelease Conventions

- Chart version: pin to minor semver (e.g. `version: "3.7.*"`) or exact
- `valuesFrom`: use for secrets that must come from a Secret rather than inline values
- `postRenderers`: avoid unless absolutely necessary
- `crds: CreateReplace` — use for charts that ship CRDs

Standard HelmRelease with app-template:

```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: myapp
spec:
  interval: 30m
  chart:
    spec:
      chart: app-template
      version: 3.7.*
      sourceRef:
        kind: HelmRepository
        name: bjw-s
        namespace: flux-system
  values:
    controllers:
      app:                        # single controller named "app" → Deployment named "myapp"
        containers:
          app:
            image:
              repository: example/myapp
              tag: 1.0.0
    service:
      app:                        # single service named "app" → Service named "myapp" (no suffix)
        controller: app
        ports:
          http:
            port: 8080
```

### app-template Service/Deployment Naming

**Single controller named `app`** → Deployment is `{release-name}` (e.g. `homebox`, `gitea`).
**Two+ controllers** → all get `{release-name}-{controller}` (e.g. `paperless-app`, `immich-server`).
Same rule applies to Services.

HTTPRoute `backendRef.name` must match the Service name exactly. Verify with:
```bash
kubectl get deployments,services -n <namespace>
```

---

## Adding a New Application

### 1. Create the namespace directory

```
kubernetes/apps/myapp/
├── namespace.yml
├── kustomization.yml        # lists ks.yml files
└── myapp/
    ├── ks.yml
    └── app/
        ├── kustomization.yml
        ├── helmrelease.yml
        ├── pvc.yml          # if needed
        ├── route.yml        # HTTPRoute
        └── app-sealed.yml   # SealedSecret (if needed)
```

### 2. namespace.yml

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: myapp
```

### 3. ks.yml

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app myapp
  namespace: flux-system
spec:
  targetNamespace: myapp
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  dependsOn:
    - name: sealed-secrets
    - name: shared-gateway
    # add: - name: postgres-cluster  if DB needed
  path: ./kubernetes/apps/myapp/myapp/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  interval: 30m
  timeout: 5m
  postBuild:
    substituteFrom:
      - kind: ConfigMap
        name: cluster-settings
      - kind: Secret
        name: cluster-secrets
        optional: true
```

### 4. Wire into namespace kustomization

```yaml
# kubernetes/apps/myapp/kustomization.yml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./namespace.yml
  - ./myapp/ks.yml
```

### 5. No root wiring needed

`kubernetes/apps/` has no root `kustomization.yml`. Flux's `cluster-apps` Kustomization points to `./kubernetes/apps` and auto-discovers all namespace-level `kustomization.yml` files. Creating the namespace directory is sufficient — no additional wiring required.

### 6. Create Sealed Secrets (if needed)

See `docs/secrets.md`.

### 7. Create HTTPRoute

```yaml
# kubernetes/apps/myapp/myapp/app/route.yml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: myapp
  namespace: myapp
  annotations:
    external-dns.alpha.kubernetes.io/enabled: "true"
spec:
  parentRefs:
    - name: shared
      namespace: gateway
  hostnames:
    - myapp.blackcats.cc
  rules:
    - backendRefs:
        - name: myapp   # single-service app-template naming
          port: 8080
```

### 8. Push and watch

```bash
git add kubernetes/apps/myapp/ && git commit -m "feat(myapp): initial deploy"
git push
flux get kustomizations --watch
kubectl get pods -n myapp --watch
```

---

## Drift Detection and Validation

CI runs on PRs touching `kubernetes/**`:

- **kubeconform** — validates manifests against Kubernetes JSON schemas
- **kube-linter** — static analysis; delta gate (new violations only fail the PR)

Workflow: `.github/workflows/manifest-scan.yml`
Required status check: `Manifest Scan Passed`

#### kube-linter scope and config (two traps)

The scan targets are **discovered leaf directories**, not `kubernetes/` — see
`.github/kube-linter-run.sh`. kube-linter treats any directory containing a
`kustomization.yml` as a kustomize root: it renders that directory and **does not
descend**. Every `kubernetes/apps/<ns>/kustomization.yml` renders to Flux
`Kustomization` CRs plus a `Namespace` (no pod specs), so `kube-linter lint
kubernetes/` walked past the entire app tree and only ever saw 6 files. Targeting
leaf dirs also lets kustomize resolve each app's ServiceAccount/RBAC beside its
workload, which removes `non-existent-service-account` false positives.

`checks.exclude` in `.github/kube-linter-config.yaml` takes a **flat list of
check-name strings** — there is no per-object form. Per-object suppression uses
an `ignore-check.kube-linter.io/<check>: "reason"` annotation on the object's
**top-level** metadata (never the pod template — that is a Job *spec* change, and
Job specs are immutable).

kube-linter cannot render `HelmRelease` CRs, so app-template workloads (sonarr,
plex, immich, …) are **never** covered by this gate; it sees only the raw
Deployments/Jobs/CronJobs.

### Image vulnerability scanning

`.github/workflows/image-scan.yml` scans every container image a PR changes, so
Renovate bumps carry a CVE diff. Required status check: `Image Scans Passed`.

**Gate:** with a baseline (a tag bump), a PR fails only if the *net* Critical or
High count **increases**. An upgrade that introduces some CVEs while fixing more
passes — the introduced ones are still listed in the PR comment. With no baseline
(new image, or the old tag no longer pulls) any Critical fails.

Both scanners run on each image: **grype** (Anchore) and **osv-scanner** (Google).
Results are posted as a single PR comment with before/after/delta tables and
collapsible introduced/fixed CVE lists. Raw scanner JSON is uploaded as a
30-day artifact.

Logic lives in `.github/scripts/`, not inline in the workflow:

| Script | Role |
|---|---|
| `extract-images.py` | Pairs old/new image refs across the diff |
| `scan-report.py` | Normalises both scanners, computes the delta, renders the comment |

Two things make this repo-specific and are easy to get wrong if the workflow is
ever rewritten:

- **Renovate usually does not change a whole image reference.** The bjw-s
  app-template and Helm-values form splits the image across two lines, so a bump
  shows up as a lone `tag:` line with `repository:` sitting on an unchanged
  context line. Grepping the diff's `+`/`-` hunks for `image:` misses these
  entirely. `extract-images.py` instead extracts the full image set from the base
  and head versions of each changed file and diffs those sets.
- **osv-scanner v2 removed severity labels.** v1 exposed
  `database_specific.severity` as `CRITICAL`/`HIGH`; in v2 that field is always
  null and severity is only available as a CVSS base score on
  `groups[].max_severity`. Any filter keyed off the v1 field silently counts zero
  for every image — a dead gate that still reports green. `scan-report.py` bands
  the CVSS score instead. grype's `matches[].vulnerability.severity` is unaffected.

Helm chart coordinates (`chart:` + `version:`, OCIRepository `url:`) are
deliberately not scanned — they are chart artifacts, not runnable images.

Test the extractor against a real PR without pushing:

```bash
git fetch origin <renovate-branch>
python3 .github/scripts/extract-images.py --base origin/main --head origin/<renovate-branch>
```

Flux reconciles every 10–30 min. To force immediate reconciliation:

```bash
flux reconcile kustomization cluster --with-source
flux reconcile kustomization myapp
```

To see what Flux would change without applying:

```bash
flux diff kustomization myapp
```
