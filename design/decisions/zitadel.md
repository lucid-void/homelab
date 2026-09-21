# Zitadel

**Read before editing:** `kubernetes/apps/auth/`, `infra/terraform/`

## Current state

**Zitadel serves nothing.** Every OIDC app moved to Keycloak
(`design/decisions/keycloak.md`), and Joplin — its last consumer, over SAML — was
deleted on 2026-09-21. What remains is a running instance with no clients: the
HelmRelease, database, managed role, bootstrap Job, Terraform, DNS record and the
`auth` namespace are all pending removal (`design/TODO.md`). `auth/proxmox-oidc-secret`
must move out of the namespace before that happens — Proxmox reads it and its client
lives in Keycloak.

It remains a separate user store with its own credentials and 2FA — a Go binary backed
by Postgres at `zitadel.blackcats.cc`. Nothing federates between it and Keycloak.

**RBAC is gone.** `kubernetes/apps/auth/bootstrap-rbac/` was deleted once Terraform
forgot the eighteen OIDC resources: the Job no longer writes Secrets into any other
namespace, so it needs no cross-namespace Roles. `design/docs/gitops.md` cites that
directory as the worked example of why a cross-namespace Kustomization must not set
`targetNamespace` — the principle stands, the directory does not.

**Bootstrap job.** Follows the `gotify-bootstrap` shape: hourly self-heal
(`ttlSecondsAfterFinished: 3600` + 30m Kustomization interval — the TTL deletes the
finished Job, Flux recreates it), drift reporting, no unpinned runtime download. Two
containers: a `tofu` initContainer runs `tofu init -lockfile=readonly` →
`tofu plan -out=tfplan` → `tofu show -json tfplan` into a shared `emptyDir` →
`tofu apply tfplan` (the plan records what was wrong *before* the repair, which is
unrecoverable once apply has run); a `report` container (image `backup-tools` — the
OpenTofu image has no jq/kubectl/curl) hashes `main.tf`+`run.sh`+`.terraform.lock.hcl`,
compares against `configHash` in `auth/zitadel-bootstrap-state`, and posts to Gotify
priority 8 when an **unchanged** config still had resources to change. Being an
initContainer means a failed apply stops the pod instead of reporting a false "no
drift". `main.tf` still owns Joplin's SAML application, its attribute-rename action
and trigger, and the `homelab` project, all of which now back a deleted app — they are
deliberately left in place so the whole Terraform goes in one step with Zitadel rather
than needing a hand-run `tofu apply` first. It issues no client secrets and writes no
Secrets. The eighteen OIDC resources were dropped from state with
`removed { lifecycle { destroy = false } }` rather than deleted, so the Zitadel-side
objects still exist and are simply unmanaged. The JSON plan can still contain secrets
under `.resource_changes[].change.after`; `report.sh` extracts only
`.address` and `.change.actions`, never values. Token is `auth/gotify-secret` via
`envFrom … optional: true`.

`.terraform.lock.hcl` is committed and shipped in the same `configMapGenerator` as
`main.tf`/`run.sh` — it is a dotfile, so `run.sh` copies it by name
(`cp /tf-config/*.tf` does not match it). A Renovate bump of the `required_providers`
constraint must regenerate the lock in the same PR:
`mise exec -- tofu -chdir=<tmpdir> providers lock -platform=linux_amd64`, run against a
file holding only the `required_providers` block.

**Secret formats.** The two shapes Zitadel established outlived it — the Keycloak
migration kept both, sealed instead of Terraform-written. Env-var style (FreshRSS,
Paperless, Immich): flat key=value data in the Secret, consumed via
`envFrom: secretRef` or mounted directly. Helm-valuesFrom style (Gitea):
`data["values.yaml"]` holding a YAML fragment, consumed via
`valuesFrom: [{kind: Secret, name: ..., valuesKey: values.yaml}]` in the HelmRelease.
Use the Helm-valuesFrom style only when the credentials must populate a chart values
list (e.g. `gitea.oauth`).

## Rules

- **Regenerate `.terraform.lock.hcl` in the same PR as any `required_providers` bump** —
  otherwise `tofu init -lockfile=readonly` fails loudly. That is the right trade against
  silently installing a provider major into the identity provider.
- **Never let the jq filter in `report.sh` extract anything beyond `.address` and
  `.change.actions`** — the plan JSON carries real client secrets under
  `.resource_changes[].change.after`, and pulling more would land them in pod logs and
  the Gotify body.
- **Do not re-add OIDC resources to `main.tf`** — new clients belong in Keycloak, as
  `KeycloakOIDCClient` CRs. The eighteen `removed` blocks are load-bearing history: they
  are what stopped the cutover from destroying the live Zitadel clients, and deleting
  them is only safe once the Zitadel-side objects are genuinely unwanted.
- **Use the Helm-valuesFrom Secret style only when the chart needs the credentials
  inside a values list** — for everything else, the flat env-var style is simpler and is
  what every other app uses.

## Verify

```bash
mise exec -- kubectl get kustomization zitadel-bootstrap -n flux-system -o jsonpath='{.spec.force}'
mise exec -- kubectl get configmap -n auth -l kustomize.toolkit.fluxcd.io/name=zitadel-bootstrap
grep -n 'required_providers' kubernetes/apps/auth/bootstrap/app/tofu/main.tf
```
