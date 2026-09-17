# Zitadel

**Read before editing:** `kubernetes/apps/auth/`, `infra/terraform/`

## Current state

Zitadel at `zitadel.blackcats.cc` is the single user store — manages all credentials and
2FA; a Go binary backed by Postgres. Apps with native OIDC connect directly to it;
client/secret are provisioned per-app by the Terraform bootstrap job.

**RBAC.** The `zitadel-bootstrap` kustomization sets `targetNamespace: auth`, which
overrides ALL namespace fields, even explicit ones. Cross-namespace Roles/RoleBindings
for other app namespaces live in `kubernetes/apps/auth/bootstrap-rbac/` — a separate
kustomization with no `targetNamespace`, which `zitadel-bootstrap` depends on.

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
drift". `main.tf` owns every `client_id`/`client_secret` in the cluster, so a Zitadel DB
reset silently reissues credentials for every app and rewrites every OIDC secret — apps
without `reloader.stakater.com/auto` keep using the old ones. The JSON plan contains the
real client secrets under `.resource_changes[].change.after`; `report.sh` extracts only
`.address` and `.change.actions`, never values. Token is `auth/gotify-secret` via
`envFrom … optional: true`.

`.terraform.lock.hcl` is committed and shipped in the same `configMapGenerator` as
`main.tf`/`run.sh` — it is a dotfile, so `run.sh` copies it by name
(`cp /tf-config/*.tf` does not match it). A Renovate bump of the `required_providers`
constraint must regenerate the lock in the same PR:
`mise exec -- tofu -chdir=<tmpdir> providers lock -platform=linux_amd64`, run against a
file holding only the `required_providers` block.

**Secret formats.** Env-var style (FreshRSS, Paperless, Immich): Terraform writes flat
key=value data in the Secret, consumed via `envFrom: secretRef` or mounted directly.
Helm-valuesFrom style (Gitea): Terraform writes `data["values.yaml"]` containing a YAML
fragment, consumed via `valuesFrom: [{kind: Secret, name: ..., valuesKey: values.yaml}]`
in the HelmRelease. Use the Helm-valuesFrom style when the credentials need to populate
a chart values list (e.g. `gitea.oauth`).

## Rules

- **Regenerate `.terraform.lock.hcl` in the same PR as any `required_providers` bump** —
  otherwise `tofu init -lockfile=readonly` fails loudly. That is the right trade against
  silently installing a provider major into the identity provider.
- **Never let the jq filter in `report.sh` extract anything beyond `.address` and
  `.change.actions`** — the plan JSON carries real client secrets under
  `.resource_changes[].change.after`, and pulling more would land them in pod logs and
  the Gotify body.
- **After a Zitadel DB reset, check every app's OIDC secret was actually rotated in its
  namespace, not just re-issued in Zitadel** — apps missing
  `reloader.stakater.com/auto` keep using the stale client secret until manually
  restarted.
- **Use the Helm-valuesFrom Secret style only when the chart needs the credentials
  inside a values list** — for everything else, the flat env-var style is simpler and is
  what every other app uses.

## Verify

```bash
mise exec -- kubectl get kustomization zitadel-bootstrap -n flux-system -o jsonpath='{.spec.force}'
mise exec -- kubectl get configmap -n auth -l kustomize.toolkit.fluxcd.io/name=zitadel-bootstrap
grep -n 'required_providers' kubernetes/apps/auth/bootstrap/app/tofu/main.tf
```
