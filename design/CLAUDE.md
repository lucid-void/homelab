# Claude Context — homelab-k8s Kubernetes Cluster

Kubernetes cluster running on Talos Linux, managed by FluxCD, on the `example.com` domain.
Full context: `design/AI_CONTEXT.md`. Detailed docs: `design/docs/`.

---

## Repo Layout (kubernetes-relevant)

```
kubernetes/
├── bootstrap/helmfile.yml     # Pre-Flux bootstrap (run once)
├── bootstrap/flux/            # Flux install entrypoint
├── flux/
│   ├── config/                # GitRepository + root Kustomizations
│   ├── apps.yml               # Points to kubernetes/apps/
│   ├── repositories/          # HelmRepository + GitRepository sources
│   ├── vars/                  # cluster-settings ConfigMap + cluster-secrets SealedSecret
│   └── pub-cert.pem           # Sealed Secrets public cert (commit this)
├── apps/
│   └── <namespace>/
│       ├── namespace.yml
│       ├── kustomization.yml  # lists ks.yml files
│       └── <app>/
│           ├── ks.yml         # Flux Kustomization (dependsOn, targetNamespace)
│           └── app/           # manifests: HelmRelease, SealedSecret, PVC, HTTPRoute…
└── talos/
    ├── talconfig.yaml         # Node definitions (edit this)
    ├── talsecret.sops.yaml    # Cluster PKI + tokens (SOPS-encrypted, generated once)
    └── clusterconfig/         # Generated per-node configs (gitignored)
```

---

## Key Conventions

**Ingress:** Always use `HTTPRoute` (or `GRPCRoute`) targeting `parentRefs: [{name: shared, namespace: gateway}]`. Never use `Ingress` objects.

**DNS:** Add `external-dns.alpha.kubernetes.io/enabled: "true"` to HTTPRoute/GRPCRoute to create a Cloudflare A record → `172.16.20.50`.

**Secrets:** Seal with `kubeseal --cert kubernetes/flux/pub-cert.pem`. Never commit raw `Secret` manifests. File naming: `*-sealed.yml` for committed files, `*-secret.yml` for plaintext templates (gitignored).

**Namespace strategy:** One namespace per application. Operators go in their own namespaces (`cnpg-system`, `democratic-csi`, `cert-manager`, etc.).

**app-template naming:**
- Single controller named `app` → Deployment and Service are `{release-name}` (no suffix)
- Two+ controllers → `{release-name}-{controller}`
- Always verify with `kubectl get svc -n <namespace>` before writing HTTPRoute backendRef

**Flux targetNamespace:** Overrides ALL namespace fields unconditionally. Cross-namespace RBAC needs its own Kustomization without targetNamespace.

**Image pinning:** Minor semver or exact tag. Never rolling major tags (`latest`, `3`). No digest pinning.

**Flux-only for config changes:** No `kubectl apply` for config changes. Everything goes through git → Flux reconciliation. Ad-hoc `kubectl` is for diagnostics only.

---

## Adding a Service (quick checklist)

1. `kubernetes/apps/<namespace>/namespace.yml`
2. `kubernetes/apps/<namespace>/kustomization.yml` — lists `ks.yml`
3. `kubernetes/apps/<namespace>/<app>/ks.yml` — Flux Kustomization with `dependsOn`
4. `kubernetes/apps/<namespace>/<app>/app/` — HelmRelease, PVC, HTTPRoute, SealedSecrets
5. Wire namespace into `kubernetes/apps/kustomization.yml` (if it doesn't exist)
6. Seal any secrets with `kubeseal --cert kubernetes/flux/pub-cert.pem`
7. Push → `flux get kustomizations --watch`

See `design/docs/gitops.md` for the full walkthrough.

---

## Sealed Secrets Workflow

```bash
# seal
kubeseal --cert kubernetes/flux/pub-cert.pem \
  --format yaml < /tmp/secret.yaml > kubernetes/apps/<ns>/<app>/app/<name>-sealed.yml

shred -u /tmp/secret.yaml
git add ... && git push
```

Controller: `sealed-secrets-controller` in `kube-system`. Key rotation disabled. Public cert: `kubernetes/flux/pub-cert.pem`.
Key backup: `sops -e` → Synology `/volume2/backups/keys/sealed-secrets-key.sops.yaml`.

---

## CNPG Database Passwords

CNPG manages `{app}-role-secret` in `postgres` namespace. Reflector mirrors it to the app namespace automatically via annotations on the SealedSecret template. Apps reference: `secretKeyRef: {name: myapp-role-secret, key: password}`.

Gitea exception: uses `extraEnvFrom` only — a `gitea-db-bootstrap` Job remaps the password into `gitea-db-env` Secret.

---

## Running Kubernetes Tools

All k8s tooling (`kubectl`, `flux`, `kubeseal`, `talosctl`, `talhelper`, `helm`, `kubeconform`) is managed by mise and may not be on `PATH` in all shell contexts. Always invoke via `mise exec`:

```bash
mise exec -- kubectl get pods -n <namespace>
mise exec -- flux get kustomizations
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < /tmp/secret.yaml > sealed.yml
mise exec -- talosctl -n 172.16.20.11 dmesg
```

Or activate the mise shims for the session:
```bash
eval "$(mise activate bash)"
```

## Flux Reconciliation

Force reconciliation:
```bash
mise exec -- flux reconcile kustomization cluster --with-source
mise exec -- flux reconcile kustomization <name>
```

Diff without applying:
```bash
mise exec -- flux diff kustomization <name>
```

---

## What NOT to Do

- **Never** write raw `Secret` manifests — always use SealedSecrets
- **Never** use `Ingress` objects — use `HTTPRoute`
- **Never** use `kubectl apply` for persistent config changes — go through git
- **Never** set `spec.targetNamespace` in a Kustomization if resources span multiple namespaces
- **Never** use `nfsvers=4.1` in static PV `mountOptions` — Talos kernel only supports NFSv4
- **Never** use Alpine's `apk add rclone` for Filen-backend scripts — install from `downloads.rclone.org`
- **Never** put Python code at 0-indent inside a YAML `|` block scalar — breaks kustomize parser

---

## Deeper References

| Topic | File |
|---|---|
| Full cluster context | `design/AI_CONTEXT.md` |
| Design decisions | `design/ARCHITECTURE.md` |
| Bootstrap + operations | `design/RUNBOOK.md` |
| Service inventory | `design/docs/services.md` |
| Networking + Gateway API | `design/docs/networking.md` |
| Flux structure + adding services | `design/docs/gitops.md` |
| Sealed Secrets + CNPG passwords | `design/docs/secrets.md` |
| Storage classes + PVC patterns | `design/docs/storage.md` |
| Known gaps | `design/TODO.md` |
