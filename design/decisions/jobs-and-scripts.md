# Jobs and scripts

**Read before editing:** any `Job`, `CronJob` or `initContainer` under `kubernetes/apps/`

## Current state

Bootstrap and maintenance logic runs as `Job`/`CronJob`/`initContainer` workloads
across the cluster, usually on the `backup-tools` image (bash, curl, jq, kubectl,
restic, rclone, postgresql17-client) with the script delivered via
`configMapGenerator` rather than baked into the image.

## Rules

- **Bound every `apk add` in a manifest with `timeout 300`, and never silence it with
  `>/dev/null 2>&1`** — `apk` has no default timeout, so a network stall hangs it
  forever, and with `set -e` blocking the rest of the script the pod sits Running and
  Ready with zero log output: the Job never completes and never fails, so
  `backoffLimit` never fires and nothing retries. Diagnose a wedged one with
  `kubectl exec <pod> -- ps -o pid,etime,args` (an `apk` process with a multi-day
  ELAPSED) — `kubectl logs` is empty and tells you nothing. Prefer not needing `apk` at
  runtime at all: use the `backup-tools` image with a ConfigMap-mounted script instead.
- **Never run an `apk add` Job as non-root — it needs BOTH `runAsUser: 0` and
  `runAsNonRoot: false`** — the two settings are one fix, not alternatives: with
  `runAsNonRoot: true` still in effect the kubelet rejects the pod outright before the
  container starts, so setting only `runAsUser: 0` swaps one failure for another. As
  non-root, `apk add` cannot write the package database and the script dies at exit 99
  with nothing in the log. The symptom that separates this from the `apk` stall above:
  here the pod reaches `Status: Error`, having started and finished within the same
  second, where a stall sits Running and Ready indefinitely. `kubectl logs` is empty in
  both cases, so the timing is the only thing that tells them apart — check
  `kubectl get pod <name> -o wide` and the start/finish timestamps before reaching for
  the timeout rule. This bit the `freshrss-notify` CronJob in 2026-06, which carried a
  pinned `uid 1000`; with the rule unknown it reads as a config or image problem. Note
  that `nfs-client` `/state` PVCs are exported 0777, so root writes there fine
  regardless — keep `fsGroup` only where a PVC genuinely needs group ownership.
- **Never put Python code at 0-indent inside a YAML `|` block scalar** — the kustomize
  YAML scanner reads the unindented line as a new mapping key and the parse breaks.
  Put Python scripts as their own ConfigMap keys (mounted alongside the shell script as
  separate files), or use a tool like `jq` that can be invoked inline without a
  multi-line code block.
- **A bootstrap Job whose manifest changes while the old Job still exists needs
  `force: true` on its Flux Kustomization, or `kubectl delete job <name> -n <ns>`** —
  Job spec is immutable after creation, so editing it while the previous run is still
  around (inside its TTL window, or still Running) fails the Flux dry-run with
  "field is immutable" and leaves the Kustomization NotReady.
