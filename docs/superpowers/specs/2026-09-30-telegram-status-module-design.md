# Telegram bot: status module (step 3 of 3)

Date: 2026-09-30. Status: awaiting review. Builds on the step 1 core spec and step 2 alerts
spec (both live).

## Goal

`/status` replies with one short message summarising cluster health, read from the
VictoriaMetrics query API. Read-only, no RBAC, no new secret, no manifest change beyond the
ConfigMap file list.

## Decisions (from the brainstorm)

- **No Flux line for now.** The Flux controllers do not expose per-object Ready status
  (`gotk_reconcile_condition` / `gotk_resource_info` are absent; only reconcile-duration and
  controller-runtime error counters exist). A real line needs kube-state-metrics custom
  resource state, a separate change to the `vm-stack` HelmRelease. Flux events still reach
  Telegram through the alerts module. Adding the line later is one new check in `status.py`.
- Built on firing alerts, pod health and backup freshness, all verified queryable on the
  live cluster on 2026-09-30.

## Constraints

- Stdlib only, read-only rootfs, no state file (nothing to persist).
- VictoriaMetrics URL from env `VM_URL`, default
  `http://vmsingle-vm-stack-victoria-metrics-k8s-stack.monitoring.svc.cluster.local:8428`.
- `/status` runs on the Telegram receiver thread, so it must stay fast: queries run
  concurrently, each with a 5 s timeout, so the worst case is about 6 s, not the sum.
- Exception type only in logs.

## Reply

```
Cluster: 🔴 needs attention        (or: Cluster: 🟢 all good)
🔴 Alerts: KubeJobFailed ×2
🟢 Pods: 125 running · 0 failed · 0 pending · 0 deployments down
🟢 Backups: oldest 14.5h (etcd-snapshot), limit 26h
```

Each line is 🟢 or 🔴. The header is 🟢 only when every line is 🟢. An unavailable check is 🔴,
never silently green.

## Checks

Each check is one or two PromQL queries against `/api/v1/query`.

| Check | Query | Red when |
|-------|-------|----------|
| Alerts | `sum by (alertname) (ALERTS{alertstate="firing",alertname!~"Watchdog\|InfoInhibitor"})` | any series. Lists `name ×count`, at most 5, then `+N more` |
| Pods | `sum by (phase) (kube_pod_status_phase)` and `count(kube_deployment_status_replicas_unavailable > 0)` (absent = 0) | Failed, Pending or Unknown > 0, or any deployment with unavailable replicas |
| Backups | `time() - kube_cronjob_status_last_successful_time{cronjob=~".*-backup\|etcd-snapshot"}`, plus `kube_cronjob_info{cronjob=~".*-backup\|etcd-snapshot"} unless on(namespace,cronjob) kube_cronjob_status_last_successful_time` | any age over 26 h, or any matching CronJob that has never succeeded |

The 26 h limit is a nightly schedule plus slack. Red backup line names the offenders, at most 5:
`STALE gitea-backup 31h, obsidian-backup never`. Green line names the single oldest.

Edge cases, each with a test:

- **A query fails** (timeout, HTTP error, bad JSON, missing `data.result`): that line becomes
  `🔴 <Check>: unavailable`, the other lines still render.
- **kube-state-metrics is down or stale:** the pod-phase query returns no series, so the pods
  and backups lines would otherwise read as zero and go green. If the total pod count is 0
  both lines show `unavailable`.
- **VictoriaMetrics unreachable:** all three lines unavailable, header red.
- **No matching backup CronJobs at all:** `🔴 Backups: none found`, since silence there is the
  failure this line exists to catch.
- **Extra arguments** to `/status` are ignored.
- **Malformed series** (missing labels, non-numeric value): skipped, not a crash.

## Module

`status.py`, class `Status(ctx, query)`, `query(promql) -> list of series` injected so tests
need no network. `vm_query(base, promql, timeout=5)` is the real one and raises `OSError`,
`ValueError` or `KeyError`. Commands: `{"status": ...}`, no callbacks, help line
`/status - cluster health summary`. Registered in `main.py` with one line; `status.py` added to
`files:`. Checks run in a `ThreadPoolExecutor` from the stdlib.

## Testing

`tests/test_status.py`, stdlib only, with a fake `query` keyed by PromQL substring:
all green, each red condition on its own, the truncation to 5 with `+N more`, every edge case
above, header logic, and that a raising check does not stop the others. `MainWiringTests`
gains `status` and asserts no command clashes.

Smoke test after deploy: `/status` in the chat. Expected right now: red, because
`KubeJobFailed` is currently firing twice on the live cluster.

## Docs

`design/decisions/flight-tracker.md`: add `status.py` to the layout and the `/status` command,
plus rules: the metric names it relies on (`ALERTS`, `kube_pod_status_phase`,
`kube_deployment_status_replicas_unavailable`, `kube_cronjob_*`), that an unavailable check is
red never green, and that the backup match is by CronJob name (`*-backup`, `etcd-snapshot`), so
a new backup job with another name is not covered. `design/decisions/monitoring.md`: one line
that `/status` reads these series, so renaming them breaks it.

## Out of scope

The Flux Ready line (needs kube-state-metrics custom resource state), node or disk checks,
per-alert detail or silencing from Telegram, any write action.
