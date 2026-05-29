# Secrets

## Overview

Two secret systems coexist:

| System | Scope | Stored in git? | Use case |
|---|---|---|---|
| **Sealed Secrets** | Kubernetes app secrets | Yes (encrypted) | All application secrets in `kubernetes/apps/` |
| **SOPS + age** | Talos machine secrets | Yes (encrypted) | `kubernetes/talos/talsecret.sops.yaml` only |

SOPS is **not** used for Kubernetes application secrets — only for Talos. All k8s app secrets are SealedSecrets.

---

## Sealed Secrets

### Controller

- **Namespace:** `kube-system`
- **Name:** `sealed-secrets-controller` (set via `fullnameOverride` in HelmRelease)
- **Key rotation:** Disabled (`keyrenewperiod: "0"`) — single stable key, backed up to Synology
- **Public cert:** `kubernetes/flux/pub-cert.pem` (committed; safe to share)

The controller holds the private key. Only it can decrypt SealedSecret resources. FluxCD applies SealedSecrets like any other manifest — it has no role in decryption.

### Sealing a Secret

```bash
# Write plaintext secret (NEVER commit this file)
cat > /tmp/secret.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: myapp-credentials
  namespace: myapp
type: Opaque
stringData:
  API_KEY: "my-secret-value"
EOF

# Seal it using the committed public cert (no cluster access needed)
kubeseal --cert kubernetes/flux/pub-cert.pem \
  --format yaml < /tmp/secret.yaml \
  > kubernetes/apps/myapp/myapp/app/credentials-sealed.yml

# Destroy plaintext
shred -u /tmp/secret.yaml

# Commit only the SealedSecret
git add kubernetes/apps/myapp/myapp/app/credentials-sealed.yml
git commit -m "feat(myapp): add credentials secret"
git push
```

FluxCD picks up the commit → Sealed Secrets controller decrypts → `Secret` is created in the cluster.

### File Naming Convention

- Sealed files: `*-sealed.yml` (e.g. `app-sealed.yml`, `rclone-sealed.yml`, `masterkey-sealed.yml`)
- Plaintext templates: `*-secret.yml` — these are gitignored templates, never committed

### Namespace Scoping

SealedSecrets are scoped to the namespace specified in the template. A SealedSecret sealed for namespace `immich` cannot be decrypted in namespace `paperless`. If you need to move a secret to a different namespace, re-seal it with the correct namespace.

### Refreshing the Public Cert

After cluster recreation (new Sealed Secrets key):

```bash
kubeseal --fetch-cert \
  --controller-name=sealed-secrets-controller \
  --controller-namespace=kube-system \
  > kubernetes/flux/pub-cert.pem

git add kubernetes/flux/pub-cert.pem
git commit -m "feat(k8s): refresh sealed-secrets public cert"
git push
```

All existing SealedSecrets in git will be re-sealed using the new key during recovery (see RUNBOOK.md).

### Backing Up the Controller Key

**Do this immediately after first install and after any manual key rotation.**
Without this backup, all SealedSecrets are permanently unreadable if the cluster is rebuilt.

```bash
kubectl get secret -n kube-system \
  -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o yaml > /tmp/ss-key.yaml

# Encrypt with existing SOPS age key, store on Synology
sops -e /tmp/ss-key.yaml > /mnt/backups/keys/sealed-secrets-key.sops.yaml

shred -u /tmp/ss-key.yaml
```

### Restoring the Key on Cluster Recreate

Restore before FluxCD reconciles any SealedSecrets — the controller will use the key whether it was restored before or after controller startup.

```bash
sops -d /mnt/backups/keys/sealed-secrets-key.sops.yaml | kubectl apply -f -
kubectl rollout restart deployment sealed-secrets-controller -n kube-system
```

---

## CNPG Database Passwords

CloudNativePG automatically creates a Secret for each managed role:

```
postgres/<app>-role-secret  →  key: password
```

These are the **single source of truth** for database passwords. They are not SealedSecrets — CNPG manages them directly.

### Cross-Namespace Access via Reflector

Apps need the DB password in their own namespace. **Reflector** (in `kube-system`) mirrors the secret automatically.

On the SealedSecret template in `postgres/<app>/database/app/role-sealed.yml`, add annotations:

```yaml
metadata:
  name: myapp-role-secret
  namespace: postgres
  annotations:
    reflector.v1.k8s.emberstack.com/reflection-auto-enabled: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-namespaces: "myapp"
    reflector.v1.k8s.emberstack.com/reflection-auto-allowed: "true"
    reflector.v1.k8s.emberstack.com/reflection-auto-allowed-namespaces: "myapp"
```

Reflector watches for changes to `postgres/myapp-role-secret` and syncs it to `myapp/myapp-role-secret`. The app then references:

```yaml
env:
  - name: DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: myapp-role-secret
        key: password
```

### Gitea Exception

The Gitea chart only supports `extraEnvFrom` (no per-key `valueFrom`). A `gitea-db-bootstrap` Job reads `gitea-role-secret.password` and writes a new Secret `gitea-db-env` with key `GITEA__database__PASSWD`. HelmRelease mounts it via `extraEnvFrom`.

---

## Zitadel Bootstrap Secrets

When Zitadel provisions OIDC clients for apps, it writes Secrets into each app namespace. Two formats are used:

**Env-var style** (FreshRSS, Paperless, Immich) — flat key=value consumed as `envFrom: secretRef`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: myapp-oidc-secret
  namespace: myapp
data:
  OIDC_CLIENT_ID: <base64>
  OIDC_CLIENT_SECRET: <base64>
```

**Helm-valuesFrom style** (Gitea) — YAML fragment consumed via `valuesFrom.valuesKey` in HelmRelease:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: gitea-oidc-secret
  namespace: gitea
data:
  values.yaml: <base64-encoded YAML fragment with gitea.oauth list>
```

Use the Helm-valuesFrom style when credentials need to populate a chart values list.

---

## Backup CronJob Secrets

Backup CronJobs (`immich-backup`, `paperless-backup`, etc.) need three secrets per namespace:

| Secret name | Contents | Source |
|---|---|---|
| `rclone-sealed` | rclone config with Filen credentials | SealedSecret |
| `restic-sealed` | `RESTIC_PASSWORD` | SealedSecret |
| `gotify-secret` | `GOTIFY_TOKEN` | Written by `gotify-bootstrap` Job (not a SealedSecret) |

`gotify-secret` is **not** a SealedSecret — `gotify-bootstrap` creates the Gotify app token via REST API and writes the plain Secret. This is intentional (idempotent re-provisioning after a Gotify DB reset). All backup jobs reference it with `optional: true` so they start even if the token hasn't been provisioned yet.

---

## Talos Secrets (SOPS)

Talos cluster secrets (`talsecret.sops.yaml`) are encrypted with the homelab SOPS age key via talhelper:

```bash
cd kubernetes/talos/
talhelper gensecret > talsecret.sops.yaml
sops -e -i talsecret.sops.yaml
```

The Netbird setup key lives in `talos/talenv.sops.yaml` and is injected into `talconfig.yaml` via talhelper environment substitution. It is not embedded in plaintext in `talconfig.yaml`.

The SOPS age key is the root of the trust chain: age key → decrypt sealed-secrets backup → restore cluster.
