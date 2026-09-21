# Flux

**Read before editing:** `kubernetes/flux/`, `kubernetes/apps/*/*/ks.yml`, `kubernetes/apps/monitoring/flux-notifications/`, `kubernetes/bootstrap/flux/`

## Current state

FluxCD reconciles everything under `kubernetes/apps/`. Every `Kustomization` object
lives in `flux-system` and runs with `wait: true`.

Flux's own version is pinned in **two** files that must be bumped together:

- `kubernetes/flux/config/flux.yml` — the `OCIRepository` tag. Drives the running
  cluster; Flux self-upgrades through the `flux` Kustomization.
- `kubernetes/bootstrap/flux/kustomization.yml` — the `?ref=`. Bootstrap-from-zero only.

Renovate raises these as two separate PRs.

Failure alerting is `kubernetes/apps/monitoring/flux-notifications/`
(`dependsOn: gotify-bootstrap`): one `v1beta3` `Provider` (`type: generic` →
`http://gotify.monitoring.svc.cluster.local/message`) plus one `Alert` at
`eventSeverity: error`, both in `monitoring`. Auth is the `flux` Gotify token in
`monitoring/flux-gotify` under the key **`headers`**, holding `X-Gotify-Key: <token>`
(written by `gotify-bootstrap`). This closes the meta-hole: Flux deploys the
metrics-alerting stack, so without it Flux itself could fail silently.

Notification-controller has no namespace wildcard
([nc#784](https://github.com/fluxcd/notification-controller/issues/784)), so coverage is
two layers:

- `Kustomization` / * / `flux-system` — gapless backstop. Every Kustomization is in
  `flux-system` and waits, so any failure (including a broken HelmRelease) trips its
  parent; new namespaces are covered automatically.
- `HelmRelease` / * / `<ns>` — enumerated per app namespace, for fast and specific
  alerts.

The generic payload carries no `priority`, so these land at Gotify priority 0 (still
forwarded to Telegram).

## Rules

- **Only put resources in a `targetNamespace` Kustomization when every one of them
  belongs in that namespace** — `spec.targetNamespace` overrides the namespace on ALL
  namespaced resources unconditionally, including resources carrying an explicit
  `metadata.namespace`.
- **Bump both Flux pin files in the same change** — `kubernetes/flux/config/flux.yml`
  drives the cluster and `kubernetes/bootstrap/flux/kustomization.yml` drives a rebuild
  from zero, so leaving one behind means a from-scratch bootstrap installs a different
  Flux than the one running.
- **Before a Flux minor upgrade that removes an API version, clear that version from
  every CRD's `status.storedVersions`** — the dry-run fails on a stored version even
  with zero objects of that kind. Symptom is *only* the `flux` Kustomization going
  NotReady with
  `... missing from spec.versions; v1beta2 was previously a storage version`;
  controllers stay on the old version and nothing else breaks. Confirm no objects exist,
  then:
  `kubectl patch crd <name> --subresource=status --type=merge -p '{"status":{"storedVersions":["v1"]}}'`.
  The three `image.toolkit.fluxcd.io` CRDs are the ones that bite — unused here, but
  installed by the Flux manifests.
- **Set `force: true` on the Flux Kustomization of every bootstrap Job** — Job spec is
  immutable, so editing the manifest while the old Job still exists (inside its
  `ttlSecondsAfterFinished` window, or still Running) fails the dry-run with
  `spec.template: Invalid value` / "field is immutable" and takes the Kustomization
  NotReady. `force: true` makes Flux delete+recreate it; the manual escape is
  `kubectl delete job <name> -n <ns>`. `gotify-bootstrap` sets it. This applies to
  every bootstrap Job, not just that one.
- **Expect `helm uninstall` to report an error when an app is removed with its
  namespace** — Flux prunes the Namespace and the HelmRelease in the same apply, the
  namespace goes `Terminating`, and any chart `post-delete` hook then fails to create
  its Job: `forbidden: unable to create new content in namespace <ns> because it is
  being terminated`. Helm logs `uninstallation completed with 1 error(s)` and the next
  reconcile logs `uninstalled Helm release for deleted resource` — the release is gone
  either way. Deleting Zitadel on 2026-09-21 hit this with the chart's
  `zitadel-cleanup` hook, whose only job was deleting Secrets in the namespace that was
  already being deleted. Before dismissing it, check what the hook actually does: one
  that reaches *outside* the namespace (an external database, an object store) really
  would be skipped, and that work then falls to you.
- **Deliver a bootstrap Job's script through `configMapGenerator`** — the hash-suffixed
  ConfigMap name changes when the script changes, which changes the Job's pod spec,
  which makes `force: true` re-run the Job. That is what turns "edit the script" into
  "re-run now".
- **Do not use Reloader to re-run a Job** — it does support Jobs (`ReCreateJobFromjob` =
  delete+recreate; CronJobs get `CreateJobFromCronjob`, an immediate Job off the
  template), but it fires on in-place ConfigMap *content* updates while a hash-suffixed
  generator produces new **names**, so there is nothing for it to see
  (`--reload-on-create` is off on this install). `force: true` is needed anyway for
  edits to `job.yml` itself.
- **After adding a new app token to `gotify-bootstrap`, update the echo log at the end
  of the job script** — otherwise the run under-reports what it provisioned.
- **A HelmRelease in a new namespace needs its own eventSource on the `Alert`** —
  non-fatal if forgotten (the `flux-system` backstop still catches it), but the alert
  is then slow and unspecific.

## Verify

```bash
# the two pins
grep -n 'tag:' kubernetes/flux/config/flux.yml
grep -n '?ref=' kubernetes/bootstrap/flux/kustomization.yml

# stored versions on a CRD, before an upgrade that drops an API version
mise exec -- kubectl get crd <name> -o jsonpath='{.status.storedVersions}'

# a bootstrap Job's Kustomization really forces
mise exec -- kubectl get kustomization <name> -n flux-system -o jsonpath='{.spec.force}'
```
