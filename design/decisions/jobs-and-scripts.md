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
