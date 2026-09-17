# Monitoring

**Read before editing:** `kubernetes/apps/monitoring/`, any `VMRule` or `VMServiceScrape`, `kubernetes/apps/goldilocks/`

## Current state

VictoriaMetrics (`vm-stack`) plus Grafana in the `monitoring` namespace (PSA
`privileged`), alerting routed to Gotify. Per-subject monitoring lives in its own Flux
Kustomization under `monitoring/` (e.g. `proxmox-monitoring`, `minecraft-monitoring`)
holding a scrape CR, a `VMRule` and a Grafana dashboard ConfigMap
(`grafana_dashboard: "1"`). vmagent runs `selectAllByDefault`, so adding one needs no
`vm-stack` HelmRelease edit.

Node-disk alerting lives in `vm-stack/vmrules.yml`: `NodeFilesystemAlmostFull` /
`NodeFilesystemCriticallyFull` at 80/90%, plus `SynologyShareAlmostFull` at 85%. The
80% warning sits above `imageGCHighThresholdPercent: 70` from `talconfig.yaml`, so
routine image GC churn stays quiet and an alert means GC ran and did not reclaim
enough.

Resource requests are sized from a 30-day VictoriaMetrics window — request ≈ p90,
limit ≈ 2x peak — queried as
`quantile_over_time(0.90, container_memory_working_set_bytes{metrics_path="/metrics/cadvisor",container!=""}[30d])`.
Deliberately no memory limit on cilium-agent, cilium-envoy and the two democratic-csi
`driver` containers — OOMKilling the CNI or a CSI driver mid-mount is worse than the
overrun it would prevent.

Goldilocks lives in the `goldilocks` namespace via Fairwinds charts
(`https://charts.fairwinds.com/stable`) with VPA (`fairwinds-stable/vpa`) in
recommender-only mode — admission controller and updater both disabled.
`controller.flags.on-by-default: true` monitors all namespaces except
`kube-system,flux-system,kube-public,kube-node-lease,default,goldilocks`. Dashboard at
`goldilocks.blackcats.cc` → `goldilocks-dashboard:80`.

## Rules

- **Pin `metrics_path="/metrics/cadvisor"` on every `container_*` selector** — the
  kubelet exports `container_memory_working_set_bytes`, `container_cpu_usage_seconds_total`
  and the rest from two scrape endpoints (`/metrics/cadvisor` and `/metrics/resource`),
  so an unfiltered selector matches each container twice: an alert fires in duplicate,
  and a `sum by (...)` panel silently doubles. `kube_pod_container_*`
  (kube-state-metrics) has a single source and is unaffected. Verify with
  `count by (metrics_path) (<metric>{...})`.
- **Key a `VMServiceScrape` selector off a dedicated label
  (`monitoring.blackcats.cc/scrape: <target>`), never `app.kubernetes.io/name`** —
  Flux `commonMetadata` rewrites that label on every resource in a Kustomization, so
  selecting on it silently matches the wrong Services or none.
- **Validate rules with `promtool check rules` before pushing** — it also checks
  annotation templates, not just PromQL.
- **Never size a request or limit from Goldilocks/VPA output** — there is no
  metrics-server in this cluster, so the recommender ingests nothing and emits only
  its configured floors; treat every recommendation as fabricated.
- **Treat `kubelet_volume_stats_*` as reporting the backing filesystem, never the
  PVC, for both storage classes** — `openebs-hostpath` claims all report the node's
  `/var` (a plain directory, no quota; PSA `baseline` forbids `hostPath` on most
  namespaces, closing the node-exporter textfile workaround), and `nfs-client` claims
  all report the whole Synology `/volume2`, so every `nfs-client` PVC returns
  byte-identical capacity and usage. A per-PVC "almost full" alert on either class is
  really a node-disk or NAS alert wearing the wrong name, and two PVCs on one node
  produce duplicate alerts for one condition. Real per-directory size needs `du` run
  inside the pod — see
  `kubernetes/apps/media/minecraft/app/world-size-configmap.yml` for the pattern.

## Verify

```bash
mise exec -- kubectl get vmservicescrape -A
promtool check rules kubernetes/apps/monitoring/vm-stack/app/vmrules.yml
```
