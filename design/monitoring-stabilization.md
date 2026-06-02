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
