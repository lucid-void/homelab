# Monitoring stabilization plan

**Date:** 2026-06-02
**Scope:** `monitoring` namespace (victoria-metrics-k8s-stack) + control-plane stability
**Trigger:** Steady stream of Gotify/Telegram alerts; control plane flapping.

---

## Symptoms observed

- A vmagent pod crash-looping (`vmagent-...-cd79fb77d`, 38+ restarts), while an older
  vmagent ReplicaSet stayed up — i.e. a **stuck rollout**.
- `kube-controller-manager` restarted 150–176× across the 3 control planes;
  `kube-scheduler-cp-1` 174×. Crash reason: `leaderelection lost/stopped` (exit 1).
- `kube-state-metrics`, the VM operator, and Grafana's config sidecars restarted with
  `context deadline exceeded` / `Failed to contact API server`; operator logged
  `leader election lost`.
- Firing alerts: `KubeAPIErrorBudgetBurn` (×2), `KubeClientErrors` (~1.1% errors vs
  `172.16.20.20:6443`), `KubeDeploymentRolloutStuck`, `KubePodCrashLooping`,
  `ServiceDown`, `TargetDown`, plus operator `ReconcileErrors` / `LogErrors`.

## Root-cause analysis

1. **Invalid vmagent flag (the trigger).** `helmrelease.yml` set
   `vmagent.spec.extraArgs.promscrape.maxConcurrency: "4"` — **this flag does not exist
   in the running vmagent**, so the container exits immediately
   (`flag provided but not defined: -promscrape.maxConcurrency`, exit 2). The new pod
   crash-loops; the operator can never finish the rollout (`progress deadline exceeded`).
   Every restart (~every 2 min) makes vmagent re-LIST the whole cluster for service
   discovery → a relist storm against the apiserver. This generates most of the alert
   noise (RolloutStuck / CrashLooping / ServiceDown / TargetDown / operator errors).

2. **Control-plane instability (deeper, partly independent).** controller-manager and
   scheduler lose their ~2s leader-election Lease and self-terminate. Mechanism is most
   likely **apiserver request saturation** (large cluster-wide LISTs + relist storms
   consume apiserver CPU/memory, delaying Lease writes) and/or **slow etcd commit
   latency**. The churn spans ~9 days, so it predates the vmagent breakage — the bad
   flag amplifies a pre-existing condition rather than being its sole cause.

3. **Visibility gap.** `kubeControllerManager`, `kubeScheduler`, and `kubeEtcd` scrapes
   are disabled (Talos binds those metrics to localhost). We therefore **cannot measure
   etcd commit/fsync latency or apiserver APF saturation** — the exact data needed to
   confirm whether the cause is monitoring request volume or etcd disk latency.

### Findings corrected during investigation

- **Scrape footprint is small**: 12 VMServiceScrapes, 4 VMNodeScrapes, 0 pod scrapes;
  `kubernetesSDCheckInterval` already 5m; KSM collectors already restricted. So
  *steady-state* monitoring API load is modest — the dynamic relist storm (finding 1)
  was the dominant monitoring-caused load.
- **Grafana sidecars are NOT a cluster-wide watch load**: although the chart grants them
  a cluster-wide ClusterRole (`configmaps,secrets: get/watch/list`), their `NAMESPACE`
  env is unset, so k8s-sidecar watches only the `monitoring` namespace. Scoping them
  would not reduce load (the ClusterRole is only a minor RBAC over-grant). **Phase 1 #2
  was dropped as a no-op.**

---

## Plan

### Phase 0 — stop the self-reinforcing loop  ✅ (done 2026-06-02)
Remove the invalid `promscrape.maxConcurrency` arg from the VMAgent spec. Unsticks the
rollout, collapses 2 vmagent pods → 1, ends the relist storm, clears most alerts.

### Phase 1 — trim monitoring's apiserver footprint  ✅ (done 2026-06-02)
Reduce the kube-apiserver self-scrape: raise its interval to 3m and drop the highest-
cardinality apiserver/workqueue histogram buckets via `metricRelabelConfigs`. Lowers
apiserver render CPU and TSDB cardinality. (Modest stability impact; mainly storage.)
~~Phase 1 #2 — scope Grafana sidecars~~ — dropped: already namespace-scoped (see above).

### Phase 2 — gain visibility (Talos machine-config change; needs explicit OK)
Expose etcd / controller-manager / scheduler metrics (Talos: etcd `listen-metrics-urls`,
the others `bind-address=0.0.0.0`) and enable the chart scrapes. Read-only, reversible.
Then measure etcd commit/fsync latency and apiserver APF rejections to **confirm or
refute** "monitoring request volume destabilizes the control plane."

### Phase 3 — durable fix (based on Phase 2 data)
- Add an **APF FlowSchema + PriorityLevelConfiguration** putting the monitoring service
  accounts (kube-state-metrics, vmagent, grafana sidecar) in a bounded low-priority lane,
  so system leader-election traffic always wins regardless of monitoring load. This
  directly enforces isolation at the mechanism level.
- If Phase 2 shows high etcd commit latency → disk/CPU fix (dedicated/faster etcd disk or
  more vCPU on the control-plane VMs), not more alert suppression.

### Unrelated gap (track separately)
- `metrics-server` is not installed → `kubectl top`, HPA, and Goldilocks/VPA are blind.

---

## Control-plane flap investigation (ongoing)

After Phase 0/1 landed, the control plane **still flaps**: kube-controller-manager and
kube-scheduler intermittently lose their leader-election lease (5s GET to the local
apiserver via KubePrism `127.0.0.1:7445` times out) and restart.

**Ruled out:**
- etcd disk latency — `etcd status` clean (DB ~210 MB, ~40% used, no errors, no alarms),
  no `apply request took too long` / slow-fsync logs, stable single leader.
- Monitoring request volume — the big relist storm was removed in Phase 0 and it still
  flaps; steady-state scrape footprint is small (34 targets, apiserver at 3m).

**Leading hypothesis:** CPU contention on the 4-vCPU control-plane nodes (they run the
control plane + all workloads). Calm-moment snapshot: loadavg 3.2–3.4/4, PSI cpu "some"
avg60 ~10% on .21. `CPUThrottlingHigh` firing. Periodic spikes (Trivy scans, Falco,
backups, image GC) likely push apiserver latency past the 5s lease deadline.

### Overnight flap-rate measurement — baseline reset 2026-06-02 ~21:09 UTC
`leaseTransitions` zeroed on both leases to measure the overnight flap rate cleanly.
Just before reset they read 514 / 521 (and flapped cp-2→cp-3 during the session, so
still active). Static-pod restart counts can't be zeroed without recreating the pods, so
they're recorded as a baseline for delta.

| Metric | Baseline @ 2026-06-02 21:09 UTC |
|---|---|
| kube-controller-manager leaseTransitions | 0 |
| kube-scheduler leaseTransitions | 0 |
| controller-manager restarts (cp-1/2/3) | 178 / 152 / 176 |
| kube-scheduler restarts (cp-1/2/3) | 174 / 166 / 178 |

**Check tomorrow morning (2026-06-03):**
```bash
kubectl get lease -n kube-system kube-controller-manager kube-scheduler \
  -o custom-columns='NAME:.metadata.name,TRANSITIONS:.spec.leaseTransitions'
kubectl get pods -n kube-system | grep -E 'kube-controller-manager|kube-scheduler'
```
`leaseTransitions` tomorrow = number of flaps overnight (was ~one per 25 min ≈ ~35/night
at the historical rate). Restart-count delta corroborates.

**Overnight result (2026-06-03 07:25 UTC):** 35 / 40 transitions over ~10.5h — still
flapping at the historical rate (~one per 17 min). Phase 0/1 did not affect it.

### Root cause found (2026-06-03): TSDB I/O starving etcd on the same disk

Morning snapshot shifted the diagnosis from CPU to **disk I/O**:
- PSI `some`: **io 15–22%** (sustained) ≫ cpu 5–10% ≫ **memory 0%**.
- Flaps are **correlated** — all 6 control-plane components (cm + scheduler ×3 nodes)
  restarted in a 4-min window (07:14–07:18), implying a shared dependency (etcd/disk),
  not independent per-node CPU contention.
- **`vmsingle` (the VictoriaMetrics TSDB) runs on `k8s-cp-3` / `172.16.20.22`, the etcd
  leader**, and etcd + the TSDB share the same physical disk: `/var` is `/dev/sda4` on a
  single `sda` per node; both `/var/lib/etcd` and `openebs-hostpath` live under `/var`.
  There is no dedicated etcd disk.

Mechanism: leader-election renews are etcd writes (need fsync) and lease GETs are
linearizable reads (need etcd). The TSDB's continuous writes + bursty background merges
saturate `sda`, etcd fsync/commit latency spikes past the ~5s lease deadline → lease
ops time out → flap. Because the busy disk is on the etcd **leader**, one burst stalls
the leader's commits and knocks out every lease client on all nodes at once.

This **supersedes the CPU-contention hypothesis** above. It also means monitoring *is*
destabilizing the control plane — via disk I/O (TSDB on etcd's disk), NOT API request
volume (Phase 0 already removed the API/relist load and flapping continued).
Still-unconfirmed direct metric: etcd `wal_fsync_duration` / `backend_commit_duration`
(localhost-bound; needs Phase 2).

### Confirmation experiment #3 — DONE, hypothesis REFUTED (2026-06-03 08:45→09:45 UTC)

Stopped the TSDB write path (suspended Flux on `vm-stack`, scaled the operator + `vmsingle`
+ `vmagent` to 0), re-zeroed the lease counters, and watched for ~59 min. **Restored after**
(operator→1, `flux resume`; operator + Flux brought `vmsingle`/`vmagent` back to 1).

**Result — TSDB disk I/O is NOT the cause:**
- I/O pressure collapsed: PSI `some` io 15–22% → **~1%** (avg60). TSDB was indeed the main
  disk-I/O source.
- But flapping continued **unchanged**: 3 transitions in 59 min ≈ 1 per 19.7 min, vs the
  overnight 1 per 18 min. Restart deltas agreed (cm +0/+2/+1, scheduler +0/+2/+1).

So the disk-I/O hypothesis is refuted, alongside the earlier ruled-out causes
(API request volume — Phase 0; memory — PSI 0%).

### What the flap actually is (evidence narrowed)
A fresh crash log during the experiment (09:19): the controller-manager got
`etcdserver: request timed out` on a write, then 3s later its lease renew timed out and it
stopped leading. So **etcd itself intermittently fails to service requests**, yet:
- etcd logs are **silent** (no slow-apply/fsync/heartbeat/raft warnings) all window,
- disk I/O ~1%, memory 0%, CPU moderate (PSI some 5–10%),
- TCP retransmits cumulative 0.02–0.05% (network looks healthy at a coarse level).

`etcdserver: request timed out` with no local etcd warnings and low disk/mem load is the
signature of **raft not committing in time** (quorum/peer/CPU-scheduling), not local disk
or apply latency. We've exhausted black-box (PSI/logs) inference.

### Next step — Phase 2 is now REQUIRED (not optional)
Expose etcd / controller-manager / scheduler metrics (Talos: etcd `listen-metrics-urls`,
the others `bind-address=0.0.0.0`) and scrape them. Decisive metrics to look at:
- `etcd_server_proposals_pending`, `etcd_server_proposals_failed_total`
- `etcd_network_peer_round_trip_time_seconds` (peer RTT → network/raft)
- `etcd_disk_backend_commit_duration_seconds`, `etcd_disk_wal_fsync_duration_seconds`
- `etcd_server_leader_changes_seen_total`, `etcd_server_slow_apply_total`
These tell us whether it's peer RTT (network), commit/fsync (disk — already doubtful), or
proposal stalls under brief CPU starvation. Fix follows from which one spikes at flap time.

### Earlier candidate permanent fixes (revisit after Phase 2 data)
Dedicated etcd disk and/or more vCPU were the leading fixes under the disk/CPU hypotheses.
Disk is now doubtful; keep CPU-headroom (`kubeReserved`/`systemReserved`, more vCPU) and
etcd-isolation options open until the metrics say which bottleneck is real.

