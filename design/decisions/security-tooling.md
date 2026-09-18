# Security tooling

**Read before editing:** `kubernetes/apps/security/`, `kubernetes/apps/kube-system/k8s-cleaner*/`, `kubernetes/apps/kube-system/descheduler/`, `.github/kube-linter-config.yaml`, `.github/kube-linter-run.sh` (read-only)

## Current state

The `security` namespace carries `pod-security.kubernetes.io/enforce: privileged`,
required for Falco's privileged container plus `hostPath` volumes; trivy-operator,
kubent and security-report all schedule fine under it too.

**Trivy Operator** — chart `aquasecurity/trivy-operator`, mode `standalone` (each scan
job downloads its own ~300 MB vuln DB), `scanJobsConcurrentLimit: 2`, memory limit
`2Gi`, `operator.infraAssessmentScannerEnabled: false`.

**Falco** — chart `falcosecurity/falco` (`https://falcosecurity.github.io/charts`),
`driver.kind: modern_ebpf`, `allowSchedulingOnControlPlanes: true` (removes the
NoSchedule taint, so the DaemonSet runs on all 3 CP nodes with no extra tolerations).
Falcosidekick routes to Gotify via `GOTIFY_TOKEN` from `security/falco-gotify-secret`
(provisioned by `gotify-bootstrap`).

**K8s-Cleaner** — OCI chart `oci://ghcr.io/gianlucam76/charts` (chart `k8s-cleaner`) in
`kube-system`. Cleaner CRs (cluster-scoped, `apps.projectsveltos.io/v1alpha1`) live in
a separate `k8s-cleaner-rules` Flux Kustomization that depends on `k8s-cleaner`, so the
CRDs exist before any Cleaner resource is applied. Two rules: `succeeded-pods` (every
6h on the hour) and `failed-pods` (every 6h at :30) delete pods by phase cluster-wide.

**kubent** — weekly CronJob (Monday 08:00, alongside `security-report`),
`activeDeadlineSeconds` `900`. Two containers: initContainer `scan` runs the scratch
image `ghcr.io/doitintl/kube-no-trouble` directly (`-o text -O /work/kubent.out`, no
runtime apk/GitHub download) into a shared `emptyDir`; main container `notify`
(`curlimages/curl`) reads the file and posts pass/fail to Gotify via `gotify-secret`
(`optional: true`), echoing findings to its own stdout. No `--exit-error` — a
"deprecated APIs found" result is a Gotify message, not a job failure; a genuine
kubent error still exits non-zero. `ttlSecondsAfterFinished: 86400`. ClusterRole grants
read-all, shared by the initContainer.

**manifest-scan gate** — `.github/kube-linter-run.sh` discovers leaf dirs holding raw
workloads and runs kube-linter's default check set (not `addAllBuiltIn: true`, which
adds ~460 style/convention findings including `minimum-three-replicas` — wrong for a
3-node cluster with no dedicated workers). Baseline findings are suppressed via
`ignore-check.kube-linter.io/<check>` annotations on top-level metadata. kube-linter
cannot render `HelmRelease` CRs, so app-template workloads are never linted. kubeconform
gates on `exit_code` and needs no equivalent fix.

**Descheduler** — `kube-system`, chart `kubernetes-sigs/descheduler`
(`https://kubernetes-sigs.github.io/descheduler/`), runs as a CronJob every 5 minutes
with default policies. Depends on the `cilium` Flux Kustomization.

## Rules

- **Never override `trivy.dbRepository` with a full `ghcr.io/...` path** — the chart
  prepends the registry itself, producing a double-prefixed `mirror.gcr.io/ghcr.io/...`
  that fails to pull. Leave it at the chart default (`aquasec/trivy-db`).
- **Trivy Operator needs the full `2Gi` memory limit, not `1Gi`** — it holds the report
  set in memory while reconciling and pins to the ceiling during a full scan sweep even
  though steady state is far lower. Re-check with
  `max_over_time(container_memory_working_set_bytes{namespace="security",container="trivy-operator",metrics_path="/metrics/cadvisor"}[7d])`.
- **Leave `operator.infraAssessmentScannerEnabled` at `false`** — node-collector tries
  to `mkdir /etc/systemd`, which fails on Talos's read-only root filesystem.
- **Falco must use `driver.kind: modern_ebpf` on Talos, never the kernel module or
  legacy eBPF driver** — the kernel module needs `insmod` (unavailable on an immutable
  OS) and legacy eBPF needs kernel headers Talos does not expose. Modern eBPF uses
  CO-RE + BTF (`/sys/kernel/btf/vmlinux`) and needs no host OS access.
- **A Cleaner rule's `aggregatedSelection` must return `{resources = {{resource = obj}, …}}`,
  never a bare list of objects** — the controller unmarshals `resources` into
  `[]ResourceResult`, so a bare object leaves `Resource` nil and the controller
  segfaults on `GetKind()` (`executor/worker.go:721`). The crash only happens when
  there is something to delete, so a wrong-shaped rule can ship and silently delete
  nothing while merely restarting on a schedule boundary. Check the Lua return shape
  first if K8s-Cleaner is restarting on a 6h boundary.
- **Check `kubectl get cronjob kubent -n security -o jsonpath='{.status}'` and compare
  `lastSuccessfulTime` against `lastScheduleTime`** rather than trusting the absence of
  a firing alert — `ttlSecondsAfterFinished: 86400` deletes a failed Job (and its
  evidence) before three failures would otherwise retain it, so a self-deleting weekly
  failure is invisible unless the schedule is checked directly. Run
  `kubectl create job --from=cronjob/kubent` before any Talos/k8s upgrade.
- **`.github/kube-linter-config.yaml`'s `checks.exclude` must be a flat list of check
  name strings, not an object with `objects:` keys** — the object shape fails config
  load, and if the workflow captures that failure into its own JSON output instead of
  erring, both base and PR violation counts silently read `0` and the gate passes
  without linting anything.
- **Never let a simplified invocation replace `.github/kube-linter-run.sh`'s leaf-dir
  discovery** (e.g. reverting to plain `kube-linter lint kubernetes/`) — kube-linter
  treats any directory containing a `kustomization.yml` as a kustomize root, renders
  it, and does not descend further. Every `apps/<ns>/kustomization.yml` renders only
  to Flux `Kustomization` CRs plus a `Namespace` — no pod specs — so pointing it at
  the repo root saw 6 files and **zero workloads**, and fixing the `checks.exclude`
  shape alone would only have moved the gate from 0-vs-0 to 19-vs-19, not to a real
  one. This is the same "passes loudly while doing nothing" failure class as the
  K8s-Cleaner `aggregatedSelection` bug above.

## Verify

```bash
mise exec -- kubectl get cronjob kubent -n security -o jsonpath='{.status}'
mise exec -- kubectl get pods -n security -l app.kubernetes.io/name=trivy-operator
mise exec -- kubectl get pods -n kube-system -l app.kubernetes.io/name=k8s-cleaner
```
