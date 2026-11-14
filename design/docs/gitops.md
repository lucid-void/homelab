# GitOps — FluxCD

**Contents** — read this list, then only the section you need:

- Overview
- Repository Layout
- Flux Reconciliation Model
  - Entry Points
  - Kustomization Tree
  - Kustomization Naming Convention
  - targetNamespace
  - Variable Substitution
- Helm Repositories
- HelmRelease Conventions
  - app-template Service/Deployment Naming
- Adding a New Application
  - 1. Create the namespace directory
  - 2. namespace.yml
  - 3. ks.yml
  - 4. Wire into namespace kustomization
  - 5. No root wiring needed
  - 6. Create Sealed Secrets (if needed)
  - 7. Create HTTPRoute
  - 8. Register the OIDC client — and gate it
  - 9. Register a Gatus check
  - 10. Add a Homepage tile
  - 11. Push and watch
- Drift Detection and Validation

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
│   │   ├── kustomization.yml
│   │   ├── cluster.yml           # GitRepository + root cluster Kustomization
│   │   └── flux.yml              # flux-system Kustomization
│   ├── apps.yml                  # Root apps Kustomization → kubernetes/apps/
│   ├── repositories/
│   │   ├── helm/                 # HelmRepository sources (one file per chart registry)
│   │   ├── git/                  # GitRepository sources (gateway-api, keycloak-operator)
│   │   └── oci/                  # OCI repository sources — empty (`resources: []`)
│   ├── vars/
│   │   ├── cluster-settings.yml  # ConfigMap: CLUSTER_NAME, CLUSTER_DOMAIN
│   │   └── cluster-secrets.sealed.yml  # SealedSecret: SECRET_CLOUDFLARE_API_TOKEN, SECRET_CLOUDFLARE_EMAIL
│   └── pub-cert.pem              # Sealed Secrets public cert (safe to commit)
├── apps/
│   ├── <namespace>/
│   │   ├── namespace.yml         # Namespace resource
│   │   ├── kustomization.yml     # Kustomize root listing all ks.yml files
│   │   └── <app>/
│   │       ├── ks.yml            # Flux Kustomization (dependsOn, path, targetNamespace)
│   │       └── app/              # Actual manifests: HelmRelease, SealedSecret, PVC, HTTPRoute, …
│   └── …
├── images/                       # Dockerfiles for custom images, built by GitHub Actions
│   ├── backup-tools/
│   └── postgres-cnpg-immich/
└── talos/
    ├── talconfig.yaml            # Node definitions, cluster settings, extensions
    ├── talsecret.sops.yaml       # Cluster PKI + join tokens (SOPS-encrypted)
    ├── talenv.sops.yaml          # talhelper env substitutions, e.g. the Netbird setup key (SOPS-encrypted)
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
                          └── keycloak-database → keycloak ─→ keycloak-realm
                                                                └── keycloak-clients
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
| `<app>` | `cilium`, `immich`, `keycloak`, `gotify` |
| `<app>-config` | `cilium-config`, `cert-manager-config` |
| `<app>-database` | `immich-database`, `keycloak-database`, `gitea-database` |
| `<app>-backup` | `immich-backup`, `postgres-backup` |
| `<app>-bootstrap` | `gotify-bootstrap`, `gitea-db-bootstrap` |

### targetNamespace

Most Kustomizations set `spec.targetNamespace`. **Critical:** `targetNamespace` overrides the namespace on ALL resources in the path, including those with explicit `metadata.namespace`. Only use `targetNamespace` when every resource in the path belongs to that one namespace. Cross-namespace RBAC needs its own Kustomization without `targetNamespace`. The worked example was `kubernetes/apps/auth/bootstrap-rbac/`: `zitadel-bootstrap` set `targetNamespace: auth`, so the Roles it needed in `gitea`, `immich`, `media` and the rest could not live in the same path. That directory went with the Keycloak migration and the rest of `auth/` with Zitadel's retirement (2026-09-21), so nothing in the repo demonstrates the trap today — the rule stands regardless.

### Variable Substitution

Variables are substituted in all manifests via `postBuild.substituteFrom`:

| Variable | Source | Value |
|---|---|---|
| `${CLUSTER_NAME}` | `cluster-settings` ConfigMap | `homelab-k8s` |
| `${CLUSTER_DOMAIN}` | `cluster-settings` ConfigMap | `blackcats.cc` |
| `${SECRET_CLOUDFLARE_EMAIL}` | `cluster-secrets` SealedSecret | Cloudflare account email |
| `${SECRET_CLOUDFLARE_API_TOKEN}` | `cluster-secrets` SealedSecret | Cloudflare API token — fills the two raw `cloudflare-secret.yml` manifests (`design/docs/secrets.md`) |

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

- Chart version: pin an exact version and let Renovate raise the bump. Every HelmRelease
  does this except `reflector`, which floats on a major wildcard
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
      version: "<copy from an existing app-template HelmRelease>"
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

See `design/docs/secrets.md`.

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

### 8. Register the OIDC client — and gate it

Only for a service behind SSO. Two halves, both in git. First the client:

```yaml
# kubernetes/apps/keycloak/clients/app/myapp.yml
apiVersion: k8s.keycloak.org/v2alpha1
kind: KeycloakOIDCClient
metadata:
  name: myapp          # this IS the client id — the CRD has no clientId field
  namespace: keycloak
spec:
  keycloakCRName: keycloak
  realm: homelab
  client:
    displayName: MyApp
    enabled: true
    loginFlows: [STANDARD]
    redirectUris: [https://myapp.blackcats.cc/oidc/callback]
    webOrigins: [https://myapp.blackcats.cc]
    roles: [admin, user]
    auth:
      method: client-secret
      secretRef: {name: myapp-client-secret, key: secret}
```

Then wire it into `kubernetes/apps/keycloak/clients/app/kustomization.yml`.

The sealed `myapp-client-secret` must carry the label
`operator.keycloak.org/kind: KeycloakOIDCClient` in `spec.template.metadata.labels`, or the
operator refuses to read it and the client never reconciles.

**A new client is open to every realm user until you gate it.** Declaring `roles:` above
grants nothing and denies nothing — it only makes the roles assignable. Add the client to
`kubernetes/apps/keycloak/entitlements/app/entitlements.conf`, and the
`keycloak-entitlements` Job creates its groups, its `browser-myapp` gate and, if you name a
`claim`, its role mapper:

```json
"myapp": {
  "gate": "user",
  "claim": "myapp_roles",
  "groups": { "user": ["user"], "admin": ["user", "admin"] }
}
```

Every group must carry the `gate` role. If the app can read a role, point it at the claim
so it re-applies the role at each login — see `design/decisions/keycloak.md`. Who belongs in
each group is set in the Keycloak console, not in git; `initialMembers` only seeds a group
the Job has just created. Skipping this step ships a service any realm account can log into,
which `sso-audit` then reports.

### 9. Register a Gatus check

Every externally-reachable service gets one. `coverage-audit` reports a route without
one the next morning, and the same for a missing tile (step 10) or a PVC with no backup
job; a deliberate skip goes in
`kubernetes/apps/monitoring/coverage-audit/app/exemptions.json` with a reason. Nothing
else notices a service that is merely *down* — Flux only reports what it cannot *reconcile*, so a healthy HelmRelease
serving 503s is silent without this.

Edit `kubernetes/apps/monitoring/gatus/app/configmap.yml` and add an endpoint to the
matching `group` (`infrastructure`, `apps`, or `media`):

```yaml
      - name: MyApp
        group: apps
        url: https://myapp.blackcats.cc/health
        interval: 1m
        conditions:
          - "[STATUS] == 200"
          - "[CERTIFICATE_EXPIRATION] > 720h"
```

Rules, each of which has already caused a false red or a silent blind spot:

- **Probe a real health path, not `/`.** Many apps serve a 200 login page while the
  backend is dead. Curl the candidate first and write the condition from what it
  actually returns — `/` is right only when nothing better exists.
- **Never combine a `[BODY]` condition with `[CERTIFICATE_EXPIRATION]`** on the same
  endpoint. See `design/decisions/monitoring.md` — it pins the certificate from the
  first handshake and goes red at every renewal.
- **Assert the status the service actually returns**, not the one you expect. FreshRSS
  and Obsidian LiveSync both assert `401`: for them a `200` would mean auth had come
  off, so the "wrong-looking" code is the healthy one. Use `[STATUS] < 400` where a
  200/302 split depends on configuration.
- **Non-HTTP services use `tcp://`** and drop the certificate condition — see the
  Minecraft and `Gitea SSH` endpoints.
- **A ClusterIP-only service gets no check here** (there is no route to probe);
  cover it with a VMRule instead.

Gatus is its own Flux Kustomization, so it reconciles independently of the app.
`configmap.yml` is a plain resource, not a generated one, so its name never changes —
the pod picks up an edit because `reloader.stakater.com/auto: "true"` is set on the
controller, which restarts it. No hash suffix is involved.

### 10. Add a Homepage tile

Only if the service has a web UI a human would open. Edit
`kubernetes/apps/homepage/homepage/app/helm-values.yml` under `config.services`:

```yaml
    - Documents:
        - MyApp:
            href: https://myapp.blackcats.cc
            description: What it is for
            icon: myapp.png
```

- Groups are `Media`, `AI`, `Games`, `Documents`, `Development`, `Infrastructure`.
  Adding a **new** group also needs a `config.settings.layout` entry, or it renders
  with default columns.
- `icon:` resolves against the homepage dashboard-icons set. When there is no icon for
  the app, fall back to a `mdi-` Material Design name (`mdi-magnify`) or an `sh-`
  selfh.st name.
- Point `href` at the page that is actually useful — LiteLLM's tile links `/ui`, not
  `/`, because `/` serves the Swagger API reference, not the admin UI.

Unlike Gatus, this file is consumed by a `configMapGenerator` and referenced through
`valuesFrom`, with `kustomizeconfig.yml` rewriting the reference to the hash-suffixed
name. An edit therefore produces a **new** ConfigMap and a Helm upgrade — Reloader is
not in the path here.

### 11. Push and watch

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

#### kube-linter scope and config

The scan targets are **discovered leaf directories**, not `kubernetes/` — see
`.github/kube-linter-run.sh`. kube-linter treats any directory containing a
`kustomization.yml` as a kustomize root and does not descend, so pointing it at the
repo root would only ever render `kubernetes/apps/<ns>/kustomization.yml` down to
Flux `Kustomization` CRs plus a `Namespace` — no pod specs. Targeting leaf dirs also
lets kustomize resolve each app's ServiceAccount/RBAC beside its workload, which
removes `non-existent-service-account` false positives. kube-linter cannot render
`HelmRelease` CRs, so app-template workloads are never covered by this gate; it sees
only the raw Deployments/Jobs/CronJobs.

Image pinning, the lscr tag trap and the `image-scan` CVE gate are in
`design/decisions/images.md`.

Flux reconciles every 10–30 min. To force immediate reconciliation:

```bash
flux reconcile kustomization cluster --with-source
flux reconcile kustomization myapp
```

To see what Flux would change without applying:

```bash
flux diff kustomization myapp
```
