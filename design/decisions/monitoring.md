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

### kromgo — README badges

`kromgo` (official OCI chart `oci://ghcr.io/home-operations/charts/kromgo`, repo
`home-operations`) turns PromQL into shields.io badge JSON. It queries VMSingle at
`vmsingle-vm-stack-victoria-metrics-k8s-stack.monitoring.svc.cluster.local:8428` —
the Prometheus *query* API, port 8428, not Prometheus' 9090 — and serves
`/badges/{id}?format=shields` plus a gallery at `/` on `kromgo.blackcats.cc`.

Three badges: `nodes` (`count(kube_node_info)`), `pods`
(`sum(kube_pod_status_phase{phase="Running"})`) and `uptime`
(`time() - min(node_boot_time_seconds)`, rendered by `humanizeDurationDays`).

The README badges are **pushed, not pulled**. GitHub renders README images through its
camo proxy, which fetches from the public internet; `kromgo.blackcats.cc` resolves to a
LAN address, so a direct shields.io endpoint would render broken for everyone. Instead
the `kromgo-badge-push` CronJob runs every 15m, curls the three shields payloads and
`PATCH`es them as `nodes.json` / `pods.json` / `uptime.json` into a public gist, which
the README points shields.io at. The connection is outbound-only, so the
no-WAN-exposure property the README advertises stays true. Credentials live in the
`kromgo-gist` sealed Secret (`GITHUB_TOKEN`, `GIST_ID`); the PAT carries the `gist`
scope and nothing else.

## Rules

- **Verify a kromgo query returns series before committing the badge** — kromgo answers
  `200` with an error payload when a query matches nothing, so a typo'd metric renders
  as a broken badge on a public README and nothing alerts. Query VMSingle directly
  first (see Verify below). The push CronJob guards the same failure at runtime by
  testing for `.schemaVersion` rather than the HTTP status.
- **Collect every badge before pushing to the gist, never one at a time** — a
  mid-loop failure would leave the gist holding badges taken at three different times,
  which reads as real cluster state rather than as a broken job. `set -euo pipefail`
  plus `curl -fsS` aborts before the single `PATCH`, leaving the last good set intact.
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
- **The four `kube-apiserver-*` SLO rule groups are off, and must stay off while the
  apiserver scrape drops its histogram buckets** — `availability`, `burnrate`,
  `histogram` and `slos` are all built on `apiserver_request_sli_duration_seconds_bucket`,
  which the `kubeApiServer` scrape drops as the highest-cardinality series on a control
  plane already under leader-election pressure. Enabled, every rule evaluated to zero
  samples and fired a permanent (info) `RecordingRulesNoData`; `KubeAPIErrorBudgetBurn`
  read the empty burnrate recordings and could never fire. Re-enable the groups only in
  the same change that un-drops the bucket metric, and re-add an alertmanager route for
  `KubeAPIErrorBudgetBurn` only then — a route for an alert that cannot fire reads as
  coverage that does not exist. Real apiserver alerting is unaffected: the
  `kubernetes-system-apiserver` group (`KubeAPIDown`, `KubeAPITerminatedRequests`,
  `KubeAggregatedAPIErrors`, `KubeClientCertificateExpiration`) uses metrics outside the
  drop regex.
- **Validate rules with `promtool check rules` before pushing** — it also checks
  annotation templates, not just PromQL.
- **Never combine `[BODY]` and `[CERTIFICATE_EXPIRATION]` on one gatus endpoint** — a
  `[BODY]` condition makes gatus read the response to the end, so Go returns the
  connection to the idle pool and a sub-90s `interval` keeps it alive indefinitely.
  Every later check reports the certificate from the *first* handshake, so the moment
  cert-manager renews `shared-tls` the endpoint goes red against a certificate no
  server is serving any more, while the service itself is fine. Endpoints without a
  `[BODY]` condition leave the body unread, get their connection closed, and
  re-handshake each interval — which is why only the one body-checking endpoint
  drifts. Confirm with
  `kubectl debug -n monitoring pod/<gatus-pod> --image=alpine/openssl -- sh -c 'echo Q | openssl s_client -connect <host>:443 -servername <host> 2>/dev/null | openssl x509 -noout -dates'`:
  a fresh handshake from gatus's own netns showing a different expiry than the
  dashboard proves it is connection reuse, not the certificate.
- **Keep `[CERTIFICATE_EXPIRATION]` thresholds well under the renewal window** —
  `shared-tls` renews 720h before expiry, so a `> 720h` condition trips exactly when
  cert-manager is *supposed* to act and leaves no margin. It survives today only
  because non-`[BODY]` endpoints re-handshake instantly and pick the new certificate
  up the same minute.
- **A gatus endpoint asserts what "up" means for that service, and three here do not
  assert `200` on `/`** — the probe path is a judgement, so record it rather than
  rederive it:
  - **Keycloak** is checked at `/realms/homelab/.well-known/openid-configuration`
    because its real `/health` is on management port 9000 and the Gateway does not
    route it. The discovery document is also what every OIDC app fetches, so a `200`
    means SSO works, not merely that the pod answers.
  - **LiteLLM** is checked at `/health/liveliness` (`/health` needs the master key).
    That proves only the proxy process — it never reaches llama-swap, so a dead model
    backend still shows green. The `ai-monitoring` VMRules cover that side.
  - **Obsidian LiveSync and FreshRSS assert `401`, not `200`.** For LiveSync a `200`
    would mean CouchDB's `require_valid_user` had come off, i.e. the probe going green
    *is* the incident.
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

# kromgo badge queries, against VMSingle directly
mise exec -- kubectl -n monitoring port-forward svc/vmsingle-vm-stack-victoria-metrics-k8s-stack 18428:8428 &
curl -s --get --data-urlencode 'query=count(kube_node_info)' "http://127.0.0.1:18428/api/v1/query" | jq .data.result

# the rendered badges, and a manual push
mise exec -- kubectl -n monitoring port-forward svc/kromgo 18080:8080 &
curl -s "http://127.0.0.1:18080/badges/uptime?format=shields"
mise exec -- kubectl -n monitoring create job --from=cronjob/kromgo-badge-push kromgo-badge-push-manual
promtool check rules kubernetes/apps/monitoring/vm-stack/app/vmrules.yml
```
