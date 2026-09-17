# Homelab — Claude Context

## What this repo is

Infrastructure-as-Code repository for a personal homelab that doubles as a production
environment. The primary compute platform is a **Talos Linux Kubernetes cluster**
managed by **FluxCD**. Everything — VM templates, cluster bootstrap, and service
deployment — is declarative and driven from git.

A tiny Docker Swarm/compose remnant survives only for the handful of workloads that
need host networking outside the cluster (ZeroTier gaming VPN). Netbird, the primary
remote-access VPN, runs as a Talos extension on every node — not on a VM.

## Design specs

All design specs live in **[design/](design/)** — committed alongside the code, kept in
sync with the implemented state. Read only the file(s) relevant to your current task.

| File | Covers |
|---|---|
| [design/CLAUDE.md](design/CLAUDE.md) | **Start here for k8s work** — conventions, adding services, sealed secrets, what not to do |
| [design/AI_CONTEXT.md](design/AI_CONTEXT.md) | Canonical context: topology, network, ingress, auth, secrets, GitOps, service inventory, gotchas |
| [design/ARCHITECTURE.md](design/ARCHITECTURE.md) | Design decisions and rationale |
| [design/RUNBOOK.md](design/RUNBOOK.md) | Bootstrap, upgrades, recovery procedures |
| [design/docs/networking.md](design/docs/networking.md) | IP plan, Cilium, L2 pools, Gateway API hierarchy, cert-manager, external-dns |
| [design/docs/services.md](design/docs/services.md) | Full service inventory: namespace, hostname, auth, storage |
| [design/docs/gitops.md](design/docs/gitops.md) | Flux structure, Kustomization tree, adding a service end-to-end |
| [design/docs/secrets.md](design/docs/secrets.md) | Sealed Secrets, CNPG password + Reflector, Zitadel bootstrap secret formats |
| [design/docs/storage.md](design/docs/storage.md) | Storage classes, static NFS PV, OpenEBS hostpath, PVC patterns |
| [design/decisions/](design/decisions/) | **Per-service decision bodies** — the gotchas behind the pointer rows below. Read the one matching the service you're touching; never read the whole directory. |
| [design/TODO.md](design/TODO.md) | Known gaps and planned work |

### Which design file to read

| If your task involves... | Read |
|---|---|
| Anything Kubernetes (Talos, Cilium, CNPG, Flux, app deploys) | [design/CLAUDE.md](design/CLAUDE.md), then [design/AI_CONTEXT.md](design/AI_CONTEXT.md) and the relevant `design/docs/` file |
| Adding or modifying a service | [design/docs/gitops.md](design/docs/gitops.md) + [design/docs/services.md](design/docs/services.md) |
| Ingress, DNS records, Gateway API, certs | [design/docs/networking.md](design/docs/networking.md) |
| Secrets, Sealed Secrets, CNPG passwords, OIDC bootstrap | [design/docs/secrets.md](design/docs/secrets.md) |
| Storage classes, PVCs, NFS | [design/docs/storage.md](design/docs/storage.md) |
| SSO / OIDC | [design/AI_CONTEXT.md](design/AI_CONTEXT.md) (auth model) |
| Bootstrap, upgrades, recovery | [design/RUNBOOK.md](design/RUNBOOK.md) |
| VM templates / OpenTofu provisioning | files under [infra/](infra/) |

## Keeping design in sync with implementation

Design files (`design/`) describe the **intended and implemented** state — not just plans.
Once implementation begins, reality takes precedence over the design.

**When you make any change to IaC (Talos config, Flux manifests, Helm values, OpenTofu,
Packer, image Dockerfiles, scripts):**
- If the change differs from what the relevant design file describes, update the design
  file to match what was actually built.
- Update the matching key-decision entry below if a decision changed during
  implementation (a service swapped, a tool replaced, an approach simplified). Don't
  leave it describing the original plan.

**Do not record deployed version numbers in this file.** Image tags and chart versions
move on every Renovate PR, and a stale pin here is worse than no pin — it reads as
authoritative and gets copied into manifests. The manifest is the source of truth; check
it with `kubectl`/`grep` when the running version actually matters. Versions belong here
only when they are *durable facts* rather than current state: a constraint (`rclone
v1.69+` for the filen backend, Gateway API `<1.7.0`), a known-bad or minimum version
(homebox `0.25.0`), or a recorded incident (`allauth 65.16` changed the token-auth
inference). Those stay true after the next bump; "we run X.Y.Z" does not.

## Synology share naming convention

| Shared folder | Path | Purpose |
|---|---|---|
| Media | `/volume2/Media/` | Single NFS export, surfaced in-cluster as the `media-nfs` RWX PVC; contains `Series/`, `Movies/`, `Downloads/`, `Photos/`, `Manga/`, etc. |
| Backups | `/volume2/backups/` | restic repos (offsite staging), DB dumps, recovery keys (incl. the Sealed Secrets key backup) |

Application data shared by the media stack lives under the single `Media` share via the
`media-nfs` PVC. Per-app config uses `nfs-client` dynamic PVCs. CNPG database data lives
on cluster storage, never on the media share.

## Key decisions (do not re-litigate without reason)

### Platform & networking
| Topic | Decision |
|---|---|
| Compute platform | Talos Linux k8s cluster, FluxCD GitOps. 3 control planes `cp-1/2/3` (`.11`–`.13`, schedulable), API VIP `.10`, Gateway VIP `.50`. No dedicated workers. |
| DNS | UDM SE at `.254` — local overrides for *.blackcats.cc, ad blocking, upstream to 1.1.1.1; external-dns writes Cloudflare A records → internal IPs. |
| Internet exposure | Cloudflare DNS used only for valid TLS certs (DNS-01); all A records → internal IPs; no port forwarding on UDM SE; remote access requires Netbird VPN. No Cloudflare proxy. |
| Netbird / ZeroTier | Netbird = primary remote-access VPN, runs as a Talos extension on every node (`wt0`, isolated from k8s networking — see AI_CONTEXT). ZeroTier = gaming with friends only, on a separate VM outside the cluster (plain compose). |
| Cloudflare API tokens | One token per consumer (external-dns, cert-manager, Proxmox), Zone→DNS→Edit on blackcats.cc only; isolated for independent revocation. |
| NFS / Postgres traffic | Cleartext on internal VLAN — accepted risk; private network, VPN-gated. |

### Storage, secrets, backups
| Topic | Decision |
|---|---|
| Secrets | App secrets via Sealed Secrets (controller in `kube-system`); SOPS+age only for Talos machine secrets. Single age key for all SOPS secrets; recovery key in `tank/backups/keys/` + offline paper copy. |
| Tofu state | Stored in PostgreSQL on the Synology (`tofu_state` database); if lost, run `tofu apply` fresh. |
| UniFi backup | Not backed up — VLAN/firewall rules reconfigured manually after reset. |

### Auth & identity
| Topic | Decision |
|---|---|

### Service-specific

Pointers, not the decisions themselves. **Read the linked file before editing the named
paths** — each holds gotchas that cost real debugging time and are invisible in the
manifests. Do not re-litigate them without reason.

| Service | Orientation | Read before touching |
|---|---|---|
| Immich | OIDC via Zitadel; embeddings on **VectorChord** in shared CNPG (`DB_VECTOR_EXTENSION` must stay **unset**); custom Postgres image; user migration needs `asset`+`album`+`person`. **pgvector must be built with an explicit `OPTFLAGS`** (Makefile defaults to `-march=native`, which bakes the GitHub runner's ISA into `vector.so`) — image tag **v1.1.1 is permanently broken**, it SIGILLs on our AVX-512-less Arrow Lake CPUs; Renovate is disabled on `imagecatalog.yml`. | `design/decisions/immich.md` — `kubernetes/apps/immich/`, `kubernetes/images/postgres-cnpg-immich/` |
| Plex | `media` ns, `replicas: 1`. Web via HTTPRoute; direct/GDM via pool-b LB pinned to `172.16.20.51` (must not drift to `.52`). CPU-only transcoding. | `design/decisions/plex.md` — `kubernetes/apps/media/plex/` |
| Proxmox OIDC | Proxmox is **bare metal, not a k8s workload**. Zitadel app provisioned by Terraform, secret lands in `auth` ns with no consumer. **Never front Proxmox behind the cluster Gateway** (circular dependency). | `design/decisions/proxmox-oidc.md` — `kubernetes/apps/auth/`, `infra/terraform/` |
| RomM | Game/ROM manager in `media`. External CNPG + embedded Valkey on `emptyDir` (keep off NFS). Runs as root, ignores PUID/PGID. OIDC via optional `envFrom`. | `design/decisions/romm.md` — `kubernetes/apps/media/romm/` |
| Minecraft | Two Paper servers (matcha, vanilla) + **Velocity** proxy on its own pool-b IP `172.16.20.52`. World data on `openebs-hostpath`, **never NFS**. Many traps: proxy IP sharing, memory sizing, quiesced backups, `server.properties` drift, plugin ports, Modrinth loaders. | `design/decisions/minecraft.md` — `kubernetes/apps/media/minecraft*/` |
| Proton Mail Bridge | Makes E2E-encrypted Proton mail readable by Paperless as local IMAP. In the `paperless` ns, IMAP-only ClusterIP, `openebs-hostpath` (gluon = SQLite), not backed up. **Its self-signed cert has one SAN, `IP:127.0.0.1`** — hence the socat sidecar in the Paperless pod; login is interactive and cannot be a Job. Use `ghcr.io/videocurio/…`, **not** `shenxn/…` (publishes stale images despite live commits). | `design/decisions/protonmail-bridge.md` — `kubernetes/apps/paperless/protonmail-bridge/`, `kubernetes/apps/paperless/paperless/` |
| Obsidian LiveSync | CouchDB in its own `obsidian` ns, sync backend for the Obsidian plugin. **Central server, not Syncthing P2P** (deliberate — see file). `openebs-hostpath`, **never NFS**. The one user-facing service **not** behind Zitadel (HTTP Basic; the plugin has no OIDC path). Five traps: a ConfigMap mounted under `/opt/couchdb` kills the entrypoint silently (exit 1, **empty logs**) via its recursive `chown -f` under `set -e` — config must be copied by an initContainer onto an emptyDir; plus `NODENAME`, `single_node`, authenticated `exec` probes, per-platform CORS origins. | `design/decisions/obsidian-livesync.md` — `kubernetes/apps/obsidian/` |
| Joplin | Own `joplin` ns. External CNPG; blobs on a dedicated PVC (**not** `Type=Database`). **SSO is SAML, not OIDC** — SP metadata must stay byte-identical to Terraform, and probes need an explicit `Host` header. | `design/decisions/joplin.md` — `kubernetes/apps/joplin/` |
| LLM stack (`ai` ns) | Qwen3.6-35B-A3B Q8_0 (34.4 GiB) on llama.cpp behind llama-swap, on the dedicated tainted `llm-1`; routed by LiteLLM (`llm.blackcats.cc`), fronted by Open WebUI (`chat.blackcats.cc`). **Text/coding only — no vision** (the model is itself multimodal, so vision is one `--mmproj` flag if ever wanted, not another model). Served as **two LiteLLM names on one process**: `local-smart` and `local-fast` (`enable_thinking: false` via `extra_body`) — thinking cost **572 chars / 152 tokens vs 0 / 2** on the same trivial prompt, and it is *per turn*, so agents get `local-fast`. Decode measured **8.4–8.9 tok/s**. **Four traps, all silent:** `--ctx-size` pre-allocates the entire KV cache and there is **no memory limit** (mmap page-cache accounting), so a bad value takes the *node*; llama-swap group defaults are `swap/exclusive: true`, so without explicit groups an embedding call evicts the 34 GiB chat model; `logToStdout` defaults to `proxy` and **swallows llama-server's log entirely** (invisible in `kubectl logs`, `GET /logs` *and* `/logs/stream/upstream`), which hides model-load failures completely; and `--cache-reuse` is **discarded at startup** for this model (`!llama_memory_can_shift`), so the only prefix caching is llama-server's cross-request prompt cache — which defaults to **8192 MiB and is on unasked**, 8 GiB the memory budget must account for. LiteLLM needs **4Gi** (OOMKilled at 1Gi inside the Prisma migration, exit 137, **zero log output**) and its `/metrics` is **404 on the OSS tier** — the scrape was deleted rather than left red. Open WebUI's `ENABLE_OAUTH_SIGNUP` defaults **false** (login fails with no account to create) and `ENABLE_PERSISTENT_CONFIG` defaults **true** (copies config into the DB, then ignores env). | `design/llm-deployment.md` — `kubernetes/apps/ai/`, `kubernetes/apps/monitoring/ai-monitoring/` |

<!-- Row bodies live in design/decisions/. Adding a service here means adding a POINTER,
     not a body. If a row exceeds ~200 chars, move it out. See .claude/TODO.md. -->


### Kubernetes stack
| Topic | Decision |
|---|---|
| Platform resource requests are grounded in VictoriaMetrics, not guesses | 35 of 99 pods ran with **no memory requests at all** until 2026-08-29 — nearly all platform components (cilium, cert-manager, democratic-csi, falco, node-exporter, sealed-secrets, reflector, external-dns, openebs, hubble, grafana sidecars, zitadel-login, cnpg). They landed in BestEffort/Burstable and were first to be evicted under node pressure, and their absence also meant scheduler allocation numbers described only part of the cluster. Requests are sized from a 30d window: request ≈ p90, limit ≈ 2x peak, queried as `quantile_over_time(0.90, container_memory_working_set_bytes{metrics_path="/metrics/cadvisor",container!=""}[30d])` — **the `metrics_path` pin is mandatory**, without it the kubelet's two endpoints double every `container_*` series. Do NOT size from Goldilocks/VPA: there is no metrics-server, so the recommender ingests nothing and emits its configured floors (see TODO "Goldilocks/VPA recommendations are fabricated"). Deliberately **no memory limit** on cilium-agent, cilium-envoy and the two democratic-csi `driver` containers — OOMKilling the CNI or a CSI driver mid-mount is worse than the overrun it would prevent. `kube-proxy` was excluded on purpose: it is an orphaned bootstrap DaemonSet that should be deleted, not sized (see TODO). |
| `kubelet_volume_stats_*` reports the **backing filesystem**, never the PVC | True for both storage classes here, in opposite directions, and it silently invalidates any per-PVC alert. `openebs-hostpath` is a plain directory on the node EPHEMERAL partition with **no quota**, so all its claims report cp-N's `/var` (~97Gi, shared with containerd/etcd/logs); `nfs-client` claims all report the whole ~21TiB Synology `/volume2`. Concretely: all 34 nfs PVCs return byte-identical capacity and usage. Consequences to remember: (a) a per-PVC "almost full" alert is really a node-disk or NAS alert wearing the wrong name — `MinecraftWorldVolumeAlmostFull` fired on ~30GB of unused container images against ~1.4GB of actual world; (b) two PVCs on one node produce **duplicate** alerts for one condition, and an unpinned nfs selector produces ~34. Real per-directory size needs `du` — and inside the pod, because the `media` namespace runs under the default PSA `baseline` which forbids `hostPath`, closing the node-exporter textfile route. See `kubernetes/apps/media/minecraft/app/world-size-configmap.yml` for the pattern. |
| Node disk has its own alert (and had none before) | `NodeFilesystemAlmostFull` / `NodeFilesystemCriticallyFull` (80/90%, `vm-stack/vmrules.yml`) plus `SynologyShareAlmostFull` (85%). Added 2026-08-29 because **nothing watched Talos node disk at all** — the only accidental coverage was the mis-attributed Minecraft PVC alert above, which fired only for the two nodes holding a world. `/var` is the one partition on a Talos node that can fill (containerd images, etcd, kubelet state, logs, every hostpath PVC), and filling it causes DiskPressure evictions rather than a clean per-volume error. The 80% warning sits deliberately above `imageGCHighThresholdPercent: 70` from `talconfig.yaml`, so routine image GC churn stays quiet and an alert means GC ran and did not reclaim enough. |
| static NFS PV nfsvers | Talos kernel only supports NFSv4 for host-level static PV mounts — always use `nfsvers=4` in PV `mountOptions`. `nfsvers=4.1` fails with "Protocol not supported". Democratic-csi dynamic PVCs mount inside privileged containers and are unaffected by this restriction. |
| security namespace PSA | The `security` namespace has `pod-security.kubernetes.io/enforce: privileged` — required for Falco (privileged container + hostPath volumes). Non-privileged workloads (trivy-operator, kubent, security-report) schedule fine under privileged policy. Without this label the cluster-default `baseline` enforcement blocks Falco pods. |
| Trivy Operator config | Chart `aquasecurity/trivy-operator`. Mode `standalone`: each scan job downloads the vuln DB independently (~300 MB each). Set `scanJobsConcurrentLimit: 2` to prevent disk exhaustion on initial scan burst. Do NOT override `trivy.dbRepository` with a full `ghcr.io/...` path — the chart prepends the registry, causing `mirror.gcr.io/ghcr.io/...` double-prefix. Leave at chart default (`aquasec/trivy-db`). **Operator memory limit is 2Gi — 1Gi is not enough.** It holds the report set in memory while reconciling and peaked at **1022 MiB against a 1 GiB limit** (2026-08-28), i.e. pinned to the ceiling: OOMKilled (exit 137) **58 times in 2d9h**, firing `KubePodCrashLooping` / `KubePodNotReady` / `KubeDeploymentReplicasMismatch`. Steady state is ~450 MiB, so the headroom only matters during a full scan sweep and the earlier "≥1 Gi" note read as sufficient for months. Re-check with `max_over_time(container_memory_working_set_bytes{namespace="security",container="trivy-operator",metrics_path="/metrics/cadvisor"}[7d])` — note the `metrics_path` pin, without it the kubelet's two endpoints double every `container_*` series. Set `operator.infraAssessmentScannerEnabled: false` — node-collector tries to `mkdir /etc/systemd` which fails on Talos's read-only root filesystem. |
| Goldilocks | Deployed in `goldilocks` namespace via Fairwinds charts (`https://charts.fairwinds.com/stable`). Requires VPA (`fairwinds-stable/vpa`) in recommender-only mode — admission controller and updater both disabled (recommendations only, no mutation). Goldilocks chart: `controller.flags.on-by-default: true` monitors all namespaces; excludes `kube-system,flux-system,kube-public,kube-node-lease,default,goldilocks`. Dashboard at `goldilocks.blackcats.cc` → `goldilocks-dashboard:80`. |
| Falco | Deployed in `security` namespace via the `falcosecurity/falco` chart (`https://falcosecurity.github.io/charts`). Must use `driver.kind: modern_ebpf` on Talos — kernel module requires `insmod` (unavailable on immutable OS), legacy eBPF requires kernel headers (not exposed by Talos). Modern eBPF uses CO-RE + BTF (`/sys/kernel/btf/vmlinux`), no host OS access needed, works on Talos 1.x out of the box. Falcosidekick enabled, routes to Gotify via `GOTIFY_TOKEN` from `security/falco-gotify-secret` (provisioned by `gotify-bootstrap`). DaemonSet runs on all 3 CP nodes; no special tolerations needed since `allowSchedulingOnControlPlanes: true` removes the NoSchedule taint. |
| K8s-Cleaner | Deployed in `kube-system` via OCI chart (`oci://ghcr.io/gianlucam76/charts`, chart `k8s-cleaner`). Cleaner CRs (cluster-scoped, `apps.projectsveltos.io/v1alpha1`) live in a separate `k8s-cleaner-rules` Flux Kustomization that depends on `k8s-cleaner` — CRDs must be installed before Cleaner resources are applied. Two rules: `succeeded-pods` (every 6h on the hour) and `failed-pods` (every 6h at :30) delete pods by phase across all namespaces. **`aggregatedSelection` must return `{resources = {{resource = obj}, …}}`, not a bare list of objects.** The controller unmarshals `resources` into `[]ResourceResult{ Resource *unstructured.Unstructured \`json:"resource"\` }`, so a bare object leaves `Resource` nil and it **segfaults** on `GetKind()` (`executor/worker.go:721`). The result loop only runs when the list is non-empty, so the wrong shape crashes *only when there is something to delete* — both rules shipped broken and silently deleted nothing for 68 days, showing up merely as periodic controller restarts (exit 2) every ~6h. Fixed 2026-08-01. If restarts reappear on a 6h boundary, check the Lua return shape first. |
| `apk add` in a manifest must be bounded | Any `apk add` in a Job/CronJob/initContainer needs `timeout 300` and must **not** be silenced with `>/dev/null 2>&1`. `apk` has no default timeout, so a network stall hangs it forever; with `set -e` blocking the rest of the script the pod stays **Running and Ready with zero log output** — the Job never completes and never fails, so `backoffLimit` never fires and nothing retries. A transient stall on 2026-08-15 wedged **three** Jobs this way (`gotify-bootstrap` 39h, `gitea-db-bootstrap` 40h, `kavita-bootstrap` 46h), each stalling its Kustomization's health check and everything `dependsOn` it. Blast radius was severe because `gotify-bootstrap` gates `vm-stack` (Alertmanager), `gatus` **and** `flux-notifications` — so all three alerting paths were down, and a Postgres primary crash-looping through the same window went unreported for four days. Diagnose with `kubectl exec <pod> -- ps -o pid,etime,args` (an `apk` with a multi-day ELAPSED); `kubectl logs` is empty and tells you nothing. All six call sites are fixed; `backup-tools`' Dockerfile is deliberately exempt (build-time, fails the Actions job loudly). The real fix is to not need `apk` at runtime at all — `gotify-bootstrap` moved to the `backup-tools` image on 2026-08-29, leaving five. |
| manifest-scan kube-linter gate | The gate was **vacuous from creation until 2026-08-22**: `.github/kube-linter-config.yaml` used an object-shaped `checks.exclude` (`- id: … objects: […]`), but `exclude` takes a **flat list of check-name strings** — kube-linter exits 1 on config load, the workflow captured that error *into* its JSON output (`> file 2>&1`), and `jq '.Reports | length' … || echo "0"` made both base and PR count 0, so delta was always 0 and every PR read `✅ no new violations`. Same shape as the k8s-cleaner Lua bug: passing loudly while doing nothing. **Fixing the config alone was not enough** — kube-linter treats any dir holding a `kustomization.yml` as a kustomize root, renders it and **does not descend**, and every `apps/<ns>/kustomization.yml` renders to Flux `Kustomization` CRs + a `Namespace` (no pod specs), so `lint kubernetes/` saw **6 files** (3 Flux, 3 Talos) and **zero workloads**; a valid config would merely have moved the gate from 0-vs-0 to 19-vs-19. Now `.github/kube-linter-run.sh` **discovers** leaf dirs containing raw workloads (so a new app is covered automatically), runs the **default** check set — `addAllBuiltIn: true` was dropped, it added ~460 style/convention findings incl. `minimum-three-replicas`, wrong for 3 nodes — and **fails the job** when no parseable report comes back rather than counting 0. Baseline is 27 findings suppressed via `ignore-check.kube-linter.io/<check>` annotations on **top-level** metadata (never the pod template — that's a Job spec change). Note kube-linter cannot render `HelmRelease` CRs, so app-template workloads are never linted. kubeconform was checked and is fine — it gates on `exit_code`, so its `|| echo "0"` is safe. |
| YAML block scalar + Python | Python code at 0-indent inside a YAML `|` block scalar breaks the kustomize YAML parser (scanner sees the unindented line as a new mapping key). Fix: put Python scripts as separate ConfigMap keys (each key is a file mounted alongside the shell script), or use a tool like jq that can be invoked inline without multi-line code blocks. |
| Descheduler | Deployed in `kube-system` via the `kubernetes-sigs/descheduler` chart (HelmRepository: `https://kubernetes-sigs.github.io/descheduler/`). Runs as a CronJob every 5 minutes with default policies. Depends on `cilium` Flux Kustomization. |
| kubent | Weekly CronJob in `security` namespace (Monday 08:00, alongside `security-report`). Two-container pod: **initContainer `scan`** runs the official scratch image `ghcr.io/doitintl/kube-no-trouble` directly (`-o text -O /work/kubent.out`, no runtime apk/GitHub download — that download was the source of intermittent `DeadlineExceeded` failures) and writes findings to a shared `emptyDir`; **main container `notify`** (`curlimages/curl`) reads the file and posts pass/fail to Gotify via `gotify-secret` (`optional: true`), echoing findings to its own stdout for `kubectl logs -c notify`. No `--exit-error` (a "deprecated APIs found" result is a Gotify message, not a job failure); a genuine kubent error still exits non-zero → failed job. `ttlSecondsAfterFinished: 86400` so a failed Job self-cleans instead of lingering and re-tripping `KubeJobFailed` every ~12h — but **that TTL also deletes the evidence**, which is how the 2026-08-24 run failed unnoticed: two `KubeJobFailed` notifications, then the Job vanished and the alert auto-resolved, leaving no trace except `lastSuccessfulTime` stuck at 2026-08-17 (found 08-28). **`activeDeadlineSeconds` is 900, not 300** — observed runtimes span 23s to **4m53s (293s)**, so the old 300s ceiling had a 7-second margin. Check `kubectl get cronjob kubent -n security -o jsonpath='{.status}'` and compare `lastSuccessfulTime` against `lastScheduleTime`; a weekly job that self-deletes its failures is otherwise invisible. ClusterRole grants read-all (shared by the initContainer). Run `kubectl create job --from=cronjob/kubent` before any Talos/k8s upgrade. |
| Monitoring stack | VictoriaMetrics (vm-stack) + Grafana in the `monitoring` namespace, with alerting routed to Gotify. `monitoring` namespace has PSA `privileged`. Replaces the former Swarm Prometheus/Loki/Grafana stack. Per-subject monitoring goes in its own Flux Kustomization under `monitoring/` (`proxmox-monitoring`, `minecraft-monitoring`) holding scrape CR + `VMRule` + Grafana dashboard ConfigMap (`grafana_dashboard: "1"`); vmagent runs `selectAllByDefault`, so no vm-stack HelmRelease edit is needed to add one. **Scrape-selector gotcha:** Flux `commonMetadata` rewrites `app.kubernetes.io/name` on every resource in a Kustomization, so a `VMServiceScrape` selector must key off a dedicated label (convention here: `monitoring.blackcats.cc/scrape: <target>`) — selecting on `app.kubernetes.io/name` silently matches the wrong Services or none. Validate rules before pushing with `promtool check rules` (it checks annotation templates too, not just PromQL). **Duplicate-series gotcha:** the kubelet exports `container_*` metrics (`container_memory_working_set_bytes`, `container_cpu_usage_seconds_total`, …) from **two** scrape endpoints — `/metrics/cadvisor` and `/metrics/resource` — so an unfiltered selector matches each container **twice**. In an alert that means every notification is delivered twice (Gotify showed near-simultaneous pairs with values differing only in the last decimal); in a `sum by (...)` dashboard panel it silently **doubles the number** (the Minecraft CPU panel read 0.03 against an actual 0.015). Always pin `metrics_path="/metrics/cadvisor"` on `container_*` selectors. Metrics from kube-state-metrics (`kube_pod_container_*`) have a single source and are unaffected. Verify with `count by (metrics_path) (<metric>{...})`. |
| Minecraft monitoring | `monitoring/minecraft-monitoring`: **mc-monitor** (`itzg/mc-monitor`, `export-for-prometheus`) in `monitoring` pings both servers cross-namespace via `EXPORT_SERVERS` and exports `minecraft_status_{healthy,players_online_count,players_max_count,response_time_seconds}`. Chosen because it is **protocol-level, not plugin-level** — identical for Paper and vanilla, and unaffected by MC version upgrades. **There is no TPS metric**: a server-side plugin is the only source, and as of 2026-08 none is maintained (sladkoff's exporter last released Feb 2025; Modrinth `prometheus-exporter` is a 0.0.1 beta capped at MC 26.1.2) — tick health is inferred from `minecraft_status_response_time_seconds` (the ping is answered on the main thread) plus container CPU. **Velocity exports no Prometheus metrics at all**, so mc-monitor pings `minecraft-proxy` as a *third* target alongside the two backends — that ping is the only proxy-health signal, and because the backends are pinged directly it distinguishes a proxy fault from a backend fault (`MinecraftServerDown` excludes the proxy target; `MinecraftProxyDown` matches only it). 8 alerts incl. `MinecraftProxyDown` (single point of failure for both servers). **Storage alerts no longer use `kubelet_volume_stats`** (2026-08-29): that metric reports the node filesystem for hostpath claims, so `MinecraftWorldVolumeAlmostFull`/`MinecraftBackupVolumeAlmostFull` were measuring cp-N's `/var` and the Synology volume under Minecraft names, and were removed. Real world size now comes from a **`world-size` sidecar** in each game pod (`world-size-configmap.yml`, a stdlib-only Python exporter on :9109 scraped via the `matcha-metrics`/`vanilla-metrics` Services) exporting `minecraft_world_size_bytes{server,world}` and `minecraft_server_data_size_bytes{server}`; it counts `st_blocks*512` to match `du` on sparse region files and rescans on a 300s timer so a scrape never blocks on disk. Alerts are `MinecraftWorldGrowingFast` (rate), `MinecraftWorldLarge` (absolute) and `MinecraftWorldSizeExporterStale` — the last one matters because a dead exporter otherwise reads as "no growth". The sidecar is liveness-only and `publishNotReadyAddresses: true`, so it can never gate pod readiness and disconnect players. Note openebs-hostpath **cannot expand in place** and enforces no quota, so there is no "exceeded its claim" condition to alert on — the 20Gi in `pvc.yml` is advisory. Gatus additionally does `tcp://` checks on the public hostnames, exercising the full DNS → .51 → router → backend path that the in-cluster scrape bypasses. **Player join/leave notifications are a separate component**, `media/minecraft-events`: a Deployment that streams the *Velocity* pod's log via `kubectl logs --follow` and posts to Gotify. Watches the proxy because every session crosses it exactly once (tailing both backends double-counts `/server` switches, and a container inside a game pod strips pod readiness and drops players). Velocity prints a **pair** of lines per join — `[connected player] N (/ip) has connected` then `[server connection] N -> matcha has connected` — and pairing them is what separates "joined" from "switched". Not doable from `minecraft_status_players_online_count` (60s poll, no names, and an Alertmanager alert stays firing until the last player leaves). Names are charset-validated before JSON interpolation; the stream self-recycles hourly and both probes read a heartbeat file, because a dropped `logs --follow` otherwise leaves the pod Running and silent forever. Uses `backup-tools` (already has bash+curl+kubectl, so no start-time `apk add`); token in `minecraft-events-gotify-secret` read from an **optional mounted secret volume, re-read per event** (not `envFrom`), so it self-heals once gotify-bootstrap runs and picks up rotations without a restart; deliberately **not** `dependsOn: gotify-bootstrap`. |

## IP map (quick reference)

```
172.16.20.2    Synology RS1219+   — physical, NFS storage only (Btrfs /volume2)
172.16.20.3    Proxmox host       — physical, hypervisor (LVM-thin; hosts the Talos VMs)
               Intel Core Ultra 5 235HX (Arrow Lake-HX, 6P+8E) — verified from /proc/cpuinfo.
               Minisforum **MS-02 Ultra**. NOT an MS-A2 — that model is AMD, and the
               Proxmox hostname `pve-msa2` is where that misidentification came from.
               The CPU identity underpins every number in design/llm-inference.md:
               6 P-cores is why llama.cpp runs --threads 6, and no AVX-512 is why the
               immich pgvector image must be built with an explicit OPTFLAGS.
172.16.20.4    DGX Spark          — physical, GPU box, WOL (not a k8s node)
172.16.20.10   API VIP            — kube-apiserver endpoint (floats via leader election)
172.16.20.11   cp-1               — Talos control plane (schedulable, runs workloads)
172.16.20.12   cp-2               — Talos control plane (schedulable, runs workloads)
172.16.20.13   cp-3               — Talos control plane (schedulable, runs workloads)
172.16.20.14   llm-1              — Talos worker, LLM inference only (tainted workload=llm)
172.16.20.23   VPN VM             — ZeroTier (plain compose, outside cluster)
172.16.20.50   Gateway VIP        — pool-a, shared Gateway ingress (Cilium L2)
172.16.20.51   pool-b             — Plex direct/GDM LoadBalancer
172.16.20.52   pool-b             — minecraft-proxy / Velocity LoadBalancer
172.16.20.254  UDM SE             — gateway + DNS resolver + ad blocking
```

Netbird (`wt0`, 100.80.x.x/16) runs as a Talos extension on every node, not on a VM.
See [design/AI_CONTEXT.md](design/AI_CONTEXT.md) for the IP isolation guards in `talconfig.yaml`.

## IaC stack

- **Packer** — base VM templates (Debian, Talos) stored in Proxmox; see [infra/packer/](infra/packer/)
- **OpenTofu** — VM provisioning + Cloudflare DNS + Zitadel OIDC bootstrap; state in Synology PostgreSQL (`tofu_state`); see [infra/terraform/](infra/terraform/). `tofu apply` is always manual.
- **Talos + talhelper** — immutable node OS, config in `kubernetes/talos/` (SOPS-encrypted secrets)
- **FluxCD** — GitOps reconciliation of everything under `kubernetes/apps/`
- **Secrets** — Sealed Secrets for app secrets; SOPS + age for Talos/Terraform secrets (single age key)
- **Task runner** — `justfile`
- **k8s tooling** — `kubectl`, `flux`, `kubeseal`, `talosctl`, `talhelper`, `helm`, `kubeconform` are managed by **mise**; invoke via `mise exec -- <tool>` (they may not be on `PATH`)
- **CI/CD** — GitHub Actions: image builds → GHCR (`backup-tools`, `postgres-cnpg-immich`) and two PR gates: `manifest-scan` (kubeconform + kube-linter) and `image-scan` (grype + osv-scanner CVE delta on every changed container image). Renovate opens dependency-bump PRs. CI never auto-applies to the cluster — Flux does that from `main`. See [design/docs/gitops.md](../design/docs/gitops.md) — note `image-scan` must diff whole-file image sets, not diff hunks, because Renovate usually changes only a `tag:` line.