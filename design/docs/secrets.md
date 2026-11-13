# Secrets

## Overview

Two secret systems coexist, plus one substitution mechanism built on the first:

| System | Scope | Stored in git? | Use case |
|---|---|---|---|
| **Sealed Secrets** | Kubernetes app secrets | Yes (encrypted) | All application secrets in `kubernetes/apps/` |
| **SOPS + age** | Talos, OpenTofu and Packer secrets | Yes (encrypted) | `kubernetes/talos/talsecret.sops.yaml`, `kubernetes/talos/talenv.sops.yaml`, `infra/terraform/secrets.sops.tfvars`, `infra/packer/credentials.sops.pkr.hcl` |
| **Flux `substituteFrom`** | The Cloudflare API token | Placeholder only | Two raw `Secret` manifests filled from the sealed `cluster-secrets` — see below |

SOPS is **not** used for Kubernetes application secrets — only for Talos, OpenTofu and
Packer (rules in `.sops.yaml`, wired up in the `justfile`). All k8s app secrets are
SealedSecrets.

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
- Two bootstrap files use `.sealed.yml` instead:
  `kubernetes/bootstrap/flux/github-deploy-key.sealed.yml` and
  `kubernetes/flux/vars/cluster-secrets.sealed.yml`
- Plaintext templates: `*-secret.yml` — these are gitignored templates, never committed
  (the two `cloudflare-secret.yml` files below are the deliberate exception)

**The plaintext files are not a backup.** The gitignored `*-secret.yml` files exist only
on the workstation, nothing in this repo backs them up, and they are the only source for
re-sealing if the controller key is lost. Some sealed files have no plaintext sibling at
all: 16 of the 80 tracked `*-sealed.yml` files had no `<same-prefix>-secret.yml` beside
them when counted on 2026-11-13. Those values exist only as ciphertext in git and as
live Secrets in the cluster.

### Flux Variable Substitution (`cluster-secrets`)

The third mechanism. `kubernetes/flux/vars/cluster-secrets.sealed.yml` is a SealedSecret
that unseals to the `cluster-secrets` Secret in `flux-system`. Flux Kustomizations list
it under `postBuild.substituteFrom`, so `${SECRET_...}` placeholders in their manifests
are filled at apply time.

Two tracked raw `kind: Secret` manifests rely on it, and they are the only sanctioned
raw Secrets in the repo:

- `kubernetes/apps/cert-manager/cert-manager/config/cloudflare-secret.yml`
- `kubernetes/apps/network/external-dns/app/cloudflare-secret.yml`

Their only value is the `${SECRET_CLOUDFLARE_API_TOKEN}` placeholder — no secret
material is in the file. Both names match the `*-secret.yml` gitignore pattern but are
tracked deliberately, so a new file of that shape needs `git add -f` and a reason.

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

Normal recovery never reaches this point: it restores the **old** controller key from
backup (below), so every SealedSecret in git still decrypts and nothing is re-sealed. If
that key is lost, every SealedSecret must be re-sealed by hand from plaintext against
the new cert. No procedure for that is written — tracked in `design/TODO.md`.

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

`/mnt/backups` is the workstation's mount of the Synology `backups` share, so the file
lands in `/volume2/backups/keys/` on the NAS. How that mount is set up is not documented
yet.

A single age key covers all SOPS secrets (Talos, OpenTofu, Packer and this backup). It
lives in three places: the local `recovery.key` named by `SOPS_AGE_KEY_FILE` in
`mise.toml`, a copy on the Synology at `/volume2/backups/keys/`, and an offline paper
copy.

**Risk: the Synology and the workstation are the whole key chain.** The controller key
backup and the age key copy both sit on the Synology. The restic and rclone credentials
for the Filen offsite backups exist in git only as SealedSecrets. Lose the Synology and
the workstation together and the offsite backups cannot be decrypted — only the paper
copy of the age key survives, and it decrypts nothing without the key backup file.
Tracked in `design/TODO.md`.

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

### Onboarding a new app database (full recipe)

A CNPG `Database` CR alone is **not enough** — its `owner` role must exist first, and
the role only exists if it's declared in the cluster's `managed.roles`. Miss this and the
app crashloops with `password authentication failed` / the Database reports
`role "<app>" does not exist`. All four steps are required:

1. **Managed role** — add an entry to `managed.roles` in
   `kubernetes/apps/postgres/cluster/app/cluster.yml` (`name`, `login: true`,
   `passwordSecret.name: <app>-role-secret`). *This is what actually creates the role.*
2. **Role password** — `<app>-role-secret` SealedSecret in the `postgres` namespace,
   with the Reflector annotations below to mirror it into the app namespace.
3. **Database CR** — a `Database` resource (`owner: <app>`) in the app's `database/app/`.
4. **Backup** — add `<app>` to `kubernetes/apps/postgres/backup/app/databases.yml`
   (unless the app has its own quiesced backup CronJob).

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

---

## OIDC Client Secrets

Every app's OIDC credentials are **sealed in git**, not issued by the IdP. Each
`KeycloakOIDCClient` CR points at a `keycloak/<app>-client-secret` via
`client.auth.secretRef`, and the matching app-side Secret carries the same value in
whatever shape the app wants. Two formats are used:

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

Because git owns the value on both sides, rebuilding the realm does **not** rotate
anything — the opposite of the old Zitadel arrangement, where a database reset silently
reissued every app's credentials and apps without `reloader.stakater.com/auto` kept
authenticating with secrets that no longer existed.

**Generate these with `openssl rand -hex 32`, never `openssl rand -base64`.** Base64
emits `+`, which decodes as a SPACE in a form-encoded client-credentials body: Keycloak
answers `401 invalid_client_credentials` while the stored secret matches byte-for-byte.

**A SealedSecret will not overwrite a Secret it does not own.** Any Secret that already
exists — created by hand, or by a previous Terraform run — must first be annotated
`sealedsecrets.bitnami.com/managed: "true"`, and the SealedSecret's
`spec.template.metadata.annotations` must carry the same annotation so it survives
re-encryption. Without it the controller skips the write in silence and the app keeps
its old credentials.

---

## Backup CronJob Secrets

Backup CronJobs (`immich-backup`, `paperless-backup`, etc.) need three secrets per namespace:

| Secret name | Contents | Source |
|---|---|---|
| `rclone-secret` | rclone config with Filen credentials | SealedSecret (`rclone-sealed.yml`) |
| `restic-secret` | `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` | SealedSecret (`restic-sealed.yml`) |
| `gotify-secret` | `GOTIFY_TOKEN` | Written by `gotify-bootstrap` Job (not a SealedSecret) |

`gotify-secret` is **not** a SealedSecret — `gotify-bootstrap` creates the Gotify app
token via REST API and writes the plain Secret, and it is the source of truth for the
token value. All backup jobs reference it with `optional: true` so they start even if
the token hasn't been provisioned yet.

---

## Talos Secrets (SOPS)

Talos cluster secrets (`talsecret.sops.yaml`) are encrypted with the homelab SOPS age key via talhelper:

```bash
cd kubernetes/talos/
talhelper gensecret > talsecret.sops.yaml
sops -e -i talsecret.sops.yaml
```

The Netbird setup key lives in `talos/talenv.sops.yaml` and is injected into `talconfig.yaml` via talhelper environment substitution. It is not embedded in plaintext in `talconfig.yaml`.

The same age key encrypts the OpenTofu variables (`infra/terraform/secrets.sops.tfvars`)
and the Packer credentials (`infra/packer/credentials.sops.pkr.hcl`); the `justfile`
recipes decrypt them on the fly.

The SOPS age key is the root of the trust chain: age key → decrypt sealed-secrets backup → restore cluster.
