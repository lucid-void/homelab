# Helm charts, app-template and Reloader

**Read before editing:** any `HelmRelease` under `kubernetes/apps/`, `kubernetes/apps/kube-system/reloader/`

## Current state

Most apps are `bjw-s/app-template` HelmReleases; the rest are upstream charts. Reloader
(Stakater) restarts workloads when a mounted Secret or ConfigMap changes.

**app-template naming.** A single service named `app` produces a k8s Service called
`{release-name}` with no suffix; two or more services produce `{release-name}-{service-name}`
for all of them. The same rule applies to Deployments: a single controller named `app`
gives `{release-name}` (e.g. `homebox`, `gitea`), multiple controllers give
`{release-name}-{controller}` (e.g. `paperless-app`).

**Reloader image tag.** Chart `v1.0.112` ships `appVersion: vv1.0.112` (double-v), which
causes `ImagePullBackOff`. The HelmRelease overrides it:
`reloader.deployment.image.tag: "v1.0.112"`.

## Rules

- **Match HTTPRoute `backendRef` and RBAC `resourceNames` to the real object name, not
  the one you expect** — the single-controller/single-service suffix rule above makes it
  easy to write `foo-app` where the object is `foo`. Check with
  `kubectl get deployments -n <ns>` before writing.
- **Do not assume `optional: true` on an `envFrom` secretRef does anything** —
  `controllers.<n>.containers.<c>.envFrom[].secretRef.optional` is **stripped** by chart
  3.7.3. No error, no warning; the key never reaches the rendered manifest, so a Secret
  you marked optional becomes mandatory and the pod sits in
  `CreateContainerConfigError` until it exists. Check with
  `kubectl get deploy <n> -o jsonpath='{..envFrom}'`.
- **Do not work around that with `optional` on a map-form `secretKeyRef` under `env:`** —
  it fails the chart's *values schema* outright (`'anyOf' failed`), so it errors at
  render instead of merely being dropped. Only the raw **array form** of `env:` passes
  `optional` through, and that is all-or-nothing per container: map and array form
  cannot be mixed.
- **A workload consuming a bootstrap-written Secret must either `dependsOn` the Job that
  writes it or tolerate a `CreateContainerConfigError` window** — those are the only two
  options once `optional` is off the table. `open-webui` takes the first, `kavita` the
  second (it self-heals).
- **Never trust a Helm values path you have not rendered** — Helm merges whatever you
  give it, and a key the chart never reads is silently dropped, so the workload comes up
  with chart defaults while git claims otherwise. Nothing warns, and Flux reporting
  `Ready` proves the release installed, not that your values were understood. Live
  examples: `k8s-cleaner` declared a **top-level** `resources:` block while the chart
  reads `controller.resources`, so the container ran `resources: {}`; gitea's
  `valkey-cluster.resources` does nothing because the real path is
  `valkey-cluster.valkey.resources`.
- **Check the *subchart's* `values.yaml`, not the parent's `@param` list** — the parent
  only documents keys it chose to surface, and subcharts are where the wrong-path
  failure actually bites.
- **Put `reloader.stakater.com/auto: "true"` on the controller's own
  `metadata.annotations`** — it is read from the Deployment/StatefulSet/DaemonSet
  object. Under `spec.template.metadata.annotations` it is a **silent no-op**: no error,
  no event, nothing restarts, and a workload waiting on a Secret created later keeps
  running with the old or empty value forever. app-template maps
  `controllers.<name>.annotations` to the controller and `controllers.<name>.pod.annotations`
  to the template, so copying from a HelmRelease into a hand-written Deployment loses
  the distinction. Verify with
  `kubectl get deploy <n> -o jsonpath='{.metadata.annotations}'`.
- **For a long-running script that can re-read its own config, prefer an optional
  mounted secret volume read per use over Reloader** — the kubelet populates it when the
  Secret appears, so it needs no restart and no Reloader at all.

## Verify

```bash
# what the chart actually renders for a values path
mise exec -- helm template <name> <repo>/<chart> --version <v> -f <values> | yq '...resources'

# what the live pod really got
mise exec -- kubectl get pod -o jsonpath='{.spec.containers[*].resources}'

# did `optional` survive?
mise exec -- kubectl get deploy <n> -o jsonpath='{..envFrom}'

# is the reloader annotation on the controller, not the template?
mise exec -- kubectl get deploy <n> -o jsonpath='{.metadata.annotations}'

# real object names before writing an HTTPRoute or RBAC rule
mise exec -- kubectl get deployments -n <ns>
```
