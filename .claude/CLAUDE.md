# Homelab — Claude Context

## What this repo is

Infrastructure-as-Code repository for a personal homelab that doubles as a production environment. Everything from VM provisioning to service deployment is managed declaratively.

## Design specs

All design specs live in **[design/](design/)** — committed alongside the code. Read only the file(s) relevant to your current task.

The **[docs/](docs/)** directory is committed and published to GitHub Pages (MkDocs Material site). It mirrors the design files in a user-facing format. Both must be kept in sync — see the "Keeping design and docs in sync" section. The recovery runbook lives at **[docs/operations/incident-response.md](docs/operations/incident-response.md)** (committed, accessible from GitHub even when local systems are down).

| File | Covers |
|---|---|
| [CLAUDE.md](design/CLAUDE.md) | k8s quick-ref: conventions, adding services, sealed secrets, what not to do |
| [AI_CONTEXT.md](design/AI_CONTEXT.md) | k8s canonical context: topology, network, ingress, auth, secrets, GitOps, service inventory, gotchas |
| [ARCHITECTURE.md](design/ARCHITECTURE.md) | k8s design decisions and rationale |
| [RUNBOOK.md](design/RUNBOOK.md) | k8s bootstrap, upgrades, recovery procedures |
| [docs/networking.md](design/docs/networking.md) | IP plan, Cilium, L2 pools, Gateway API hierarchy, cert-manager, external-dns |
| [docs/services.md](design/docs/services.md) | Full service inventory: namespace, hostname, auth, storage |
| [docs/gitops.md](design/docs/gitops.md) | Flux structure, Kustomization tree, adding a service end-to-end |
| [docs/secrets.md](design/docs/secrets.md) | Sealed Secrets, CNPG password + Reflector, Zitadel bootstrap secret formats |
| [docs/storage.md](design/docs/storage.md) | Storage classes, static NFS PV, OpenEBS hostpath, PVC patterns |
| [TODO.md](design/TODO.md) | Known gaps and planned work |

### Which design files to read

| If your task involves... | Read |
|---|---|
| Adding/modifying a Swarm service | `design_old/compute.md`, `design_old/storage.md` |
| Changing DNS records or NTP | `design_old/network.md` |
| Provisioning a new VM or LXC | `design_old/compute.md`, `design_old/iac-pipeline.md` |
| Adding NFS mounts or Synology shares | `design_old/storage.md` |
| Certificates or HTTPS setup | `design_old/certificates.md` |
| Ansible playbooks or Tofu modules | `design_old/iac-pipeline.md` |
| Dashboards, alerts, or exporters | `design_old/monitoring.md` |
| Gitea, CI runners, or pipelines | `design_old/iac-pipeline.md` |
| SSO, OIDC, or auth middleware | `design_old/sso.md` |
| Backup or recovery procedures | `design_old/storage.md`, `design_old/incident-response.md`; recovery runbook at `docs/operations/incident-response.md` |
| Documentation site changes | `design_old/docs-site.md` |
| Kubernetes cluster (Talos, Cilium, CNPG, FluxCD) | `design/CLAUDE.md` (start here), then `design/AI_CONTEXT.md` and relevant `design/docs/` file |

## Keeping design and docs in sync with implementation

Design files (`design/`) and the docs site (`docs/`) describe the **intended and implemented** state of the homelab — not just plans. Once implementation begins, reality takes precedence over the design.

**When you make any change to IaC (Ansible, Tofu, Packer, compose files, scripts):**
- If the change differs from what the relevant design file describes, update the design file to match what was actually built.
- If the change affects something documented in `docs/`, update the docs page too.
- Do not leave design files describing a plan that was superseded by implementation choices.

**Specifically:**
- Changed a VM's resource allocation, IP, or placement → update `design/compute.md` and `docs/stack/compute.md`
- Changed a ZFS dataset, NFS export, or backup script → update `design/storage.md` and `docs/stack/storage.md`
- Changed a DNS record, VLAN, or firewall rule → update `design/network.md` and `docs/stack/network.md`
- Added or removed a Swarm service → update `design/compute.md`, `docs/stack/services.md`, and relevant design files
- Changed a monitoring exporter, alert, or scrape target → update `design/monitoring.md` and `docs/operations/alerting.md`
- Changed secrets handling, CI pipeline, or SOPS config → update `design/iac-pipeline.md` and `docs/automation/`
- Changed the backup script or recovery procedure → update `design/storage.md`, `docs/stack/storage.md`, and `docs/operations/incident-response.md`

**This applies to the key decisions table too.** If a decision changes during implementation (e.g. a service is swapped, a tool is replaced, an approach is simplified), update the key decisions entry — don't leave it describing the original plan.

## Visual companion

Topology diagrams and service placement maps are generated during brainstorming sessions using a local browser companion. Saved diagrams live in `.superpowers/brainstorm/`.

To resume visual brainstorming in a new session, start the server:

```bash
/home/arcana/.claude/plugins/cache/claude-plugins-official/superpowers/5.0.6/skills/brainstorming/scripts/start-server.sh \
  --project-dir /home/arcana/repos/Homelab
```

The server returns a JSON object with `url`, `screen_dir`, and `state_dir`. Open the URL in a browser, then write HTML content fragments to `screen_dir` to display diagrams.

## Synology share naming convention (important)

| Shared folder | Path | Purpose |
|---|---|---|
| Media | `/volume2/Media/` | Single NFS export → `/mnt/media` on Media VM (.12) and Services VM (.13); contains `Series/`, `Movies/`, `Downloads/`, `Photos/`, `Paperless/`, `Gitea/` subdirs |
| Backups | `/volume2/backups/` | DB dumps, offsite sync staging, recovery keys |

All application data (Immich photos, Paperless docs, Gitea repos) lives as subdirectories of the single `Media` share. DB live data never goes on NFS — local ext4 on DB VM only.

DB live data lives on the DB VM (.10) local ext4 disk (`/opt/volumes/<engine>/`) — never NFS-exported.

## Key decisions (do not re-litigate without reason)

| Topic | Decision |
|---|---|
| SeaweedFS | Eliminated — reactive_resume also removed from stack; no S3 consumer remains |
| DB backups | Daily coordinated script only (stop → dump → restart → rclone sync); Databasus was evaluated and scrapped |
| DB backup scope | Databases in scope: immich, paperless, gitea, zitadel, freshrss, tofu_state — all on DB VM Postgres; paired dumps (immich/paperless/gitea) go into media share dbdump/ dirs; others to backups/databases/; 30-day retention |
| Backup timeout | 4-hour total timeout on backup script; emits heartbeat timestamp on success; Prometheus alerts if older than 25 hours |
| Offsite backups | rclone crypt remote → Filen cloud; rclone config + credentials deployed by Ansible to DB VM (.10) |
| Database hosting | Postgres, MariaDB, pgadmin, adminer run as Swarm services pinned to DB VM (.10) via `node.hostname == db`; data on local ext4 bind mounts; `db` overlay network; services connect by overlay DNS name (e.g. `POSTGRES_HOST=postgres`); port 5432 published `mode=host` for backup script |
| Docker volumes | Config + ephemeral = local host; Immich/Paperless/Gitea data = Synology NFS; DB data dirs = local ext4 bind mounts on DB VM (.10) |
| Certificates | Each service manages its own — see `design/certificates.md` |
| Cloudflare API tokens | One token per consumer (Traefik, Proxmox, Synology), Zone→DNS→Edit on blackcats.cc only; isolated for independent revocation |
| Traefik certs | Per-service ACME DNS-01; acme.json in local named volume on Services VM (.13) |
| Proxmox cert | Built-in ACME client (`pvenode`), Cloudflare plugin, provisioned by Ansible |
| Synology cert | Built-in ACME via DSM, provisioned by Ansible |
| Traefik Swarm labels | Must be under `deploy: labels:` — top-level `labels:` are invisible to the Swarm API |
| Traefik Swarm mode | `providers.swarm` (not `providers.docker.swarmMode`); middleware/router names get `@swarm` suffix — cross-stack middleware refs must use e.g. `authelia@swarm`; `dnsChallenge.resolvers` must point to 1.1.1.1 |
| DNS | UDM SE at .254 — local overrides for *.blackcats.cc, ad blocking, upstream to 1.1.1.1; no separate DNS VM |
| NTP | Public pool.ntp.org — no local NTP server |
| Redis replacement | Valkey for all cache/broker instances |
| Immich ML | Runs on CPU on Services VM (.13) — DGX is WOL-gated |
| Immich OAuth | Zitadel Web app type + `/api/oauth/mobile-redirect` endpoint as redirect URI (proxies to `app.immich:///oauth-callback`); Web type required because Native type rejects https:// redirect URIs |
| Immich user migration | Must transfer `asset` + `album` + `person` rows; omitting `person` breaks mobile sync (FK violation on `asset_face_entity`); see `docs/operations/db-migrations.md` |
| Traefik websecure timeouts | `readTimeout=600s` + `writeTimeout=600s` on the websecure entrypoint — required for large file uploads (Immich photo backup); default 60s kills multi-photo uploads mid-stream |
| PBS | Removed — no VM-level backups; primary recovery path is Tofu + Ansible rebuild |
| Swarm manager | Single manager at Services VM (.13) |
| Plex | Configured in k8s (`media` namespace, `replicas: 1`). Previously ran as plain Docker Compose on Media VM (.12) with --network host and /dev/dri (GDM discovery + iGPU transcode). k8s deployment does not yet have GPU access. |
| Netbird / ZeroTier | Plain docker compose on Game VM (.14) with --network host, outside Swarm |
| Monitoring stack | Dedicated VM at .11 (Swarm worker); Prometheus + Loki + Grafana; all storage local/ephemeral |
| Monitoring alerting | Grafana built-in alerting → Gotify via Swarm overlay DNS (`http://gotify:80/message`, NOT the domain — avoids circular dependency if .13 is down); no Alertmanager |
| Service health / cert expiry | Uptime Kuma on Monitoring VM (.16) — HTTP/HTTPS checks + cert expiry; replaces blackbox_exporter for this use case |
| Grafana dashboards | All dashboards, data sources, and alert rules provisioned by Ansible; nothing created manually in UI (ephemeral) |
| DGX Spark alerts | WOL-managed (off by default) — no `up == 0` alerts; disk/GPU/resource alerts active when host is up; label `wol_managed: "true"` excludes from down alerts |
| Log shipping | Promtail as Ansible systemd unit on every host (including Proxmox, Synology) |
| Container metrics | cAdvisor as Swarm global service |
| GPU metrics | dcgm-exporter on DGX Spark (.4), scraped by Prometheus |
| Network metrics | unifi-poller on monitoring VM, polls UDM SE local API |
| Synology metrics | synology-exporter on monitoring VM, polls Synology REST API |
| Proxmox metrics | pve_exporter (API, on monitoring VM) + node_exporter with hwmon (on Proxmox host) |
| Gitea | Swarm service on Services VM (.13); data on `tank/media/gitea` NFS; shared Postgres; scheduled mirror from GitHub every 10 min; Gitea Actions CI |
| Gitea runner | Dedicated Debian LXC on Proxmox at 172.16.20.17; act_runner; SOPS age key deployed by Ansible |
| Tofu apply | Always manual — CI never auto-applies |
| SSO provider | Zitadel — single user store, manages all credentials and 2FA; Go binary backed by Postgres only |
| SSO forward auth | oauth2-proxy as Traefik middleware; authenticates against Zitadel via OIDC/PKCE (confidential Web Application — oauth2-proxy requires a client secret even with PKCE); middleware name `oauth2-proxy@file` |
| SSO native OIDC | Apps with native support connect directly to Zitadel; determined per-service at deployment |
| SSO placement | Zitadel + Authelia both on Services VM (.13); dedicated Valkey for Authelia only (Zitadel needs none) |
| Internet exposure | Cloudflare DNS used only for valid TLS certs (DNS-01); all A records → internal IPs; no port forwarding on UDM SE; access requires local network, Netbird (main VPN), or ZeroTier (gaming); no Cloudflare proxy |
| Netbird vs ZeroTier | Netbird = primary remote access VPN; ZeroTier = gaming with friends only |
| Backup script | Lives in IaC repo, deployed by Ansible as cron job to DB VM (.10); runs on DB VM (local DB access + NFS mounts for dump output and rclone sync) |
| Tofu state | Stored in PostgreSQL on TrueNAS (`tofu_state` database); backed up daily by pg_dump alongside other databases; if lost, run `tofu apply` fresh |
| Recovery path | `tofu apply` + `ansible-playbook`; data restore from Filen; no PBS |
| NFS / Postgres traffic | Cleartext on internal VLAN — accepted risk; private network, VPN-gated, mitigated by planned nftables |
| SOPS keys | Single age key for all secrets (no per-function scoping — ineffective against runner compromise anyway); backup recovery key in tank/backups/keys/ + offline paper copy |
| Docker image pinning | Minor semver (e.g. `traefik:v3.1`) — no rolling major tags, no digests |
| Swarm restart policy | `condition: any` — valid values: none, on-failure, any; `unless-stopped` is invalid in Swarm |
| Drift detection | Weekly scheduled CI: `tofu plan` + `ansible-playbook --check --diff` → report to Gotify; read-only, never auto-applies |
| Pi OS | DietPi (low-write, SD card longevity); Technitium query logging disabled/in-memory; SD failure → rebuild via Ansible (no persistent data) |
| Host firewall | Per-host nftables planned (default deny inbound, SSH/node_exporter/Promtail allowlist, per-host service overrides); not yet implemented |
| SSO rate limiting | Traefik rateLimit middleware on Zitadel/Authelia login endpoints; Zitadel built-in lockout; Authelia regulation (max_retries, ban_time) |
| UniFi backup | Not backed up — VLAN/firewall rules reconfigured manually after reset; config is simple enough to accept this |
| Swarm viability | Staying on Docker Swarm; migration trigger: >50 services, need for CRDs/operators, or Swarm dropped from Docker Engine |
| k8s app-template service naming | app-template v3.7.3: single service named `app` → k8s Service is `{release-name}` (no suffix); two or more services → all get `{release-name}-{service-name}`. Same rule applies to Deployments: single controller named `app` → Deployment is `{release-name}` (e.g. `homebox`, `gitea`); multiple controllers → `{release-name}-{controller}` (e.g. `paperless-app`). HTTPRoute backendRef and RBAC resourceNames must match exactly — verify with `kubectl get deployments -n <ns>` before writing. |
| k8s FreshRSS OIDC | Uses Apache mod_auth_openidc (`OIDC_ENABLED=1`). Redirect URI is `https://rss.blackcats.cc/i/oidc/` (NOT `/i/?get=oidc` — that's the old PHP lib path). Required env vars: `OIDC_CLIENT_CRYPTO_KEY` (session passphrase), `OIDC_REMOTE_USER_CLAIM`, `OIDC_X_FORWARDED_HEADERS` uses real header names (`X-Forwarded-Host`) not PHP var names. Client/secret come from `freshrss-oidc-secret` written by Terraform bootstrap. |
| k8s Paperless OIDC | Via django-allauth 65.x: `PAPERLESS_SOCIALACCOUNT_PROVIDERS` JSON env var with `openid_connect.APPS[].provider_id = "zitadel"`. Callback URI: `https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/` (allauth 65.x path is `/accounts/oidc/<provider_id>/` — NOT `/accounts/<provider_id>/`). Must also set `PAPERLESS_APPS: "allauth.socialaccount.providers.openid_connect"` — without this the provider is not registered in INSTALLED_APPS and the login button never appears. Also needs `PAPERLESS_ACCOUNT_DEFAULT_HTTP_PROTOCOL=https` and `PAPERLESS_ACCOUNT_EMAIL_VERIFICATION=none`. Config written by Terraform into `paperless-oidc-secret`. |
| k8s Stakater Reloader image tag | Chart `v1.0.112` has a bug: `appVersion: vv1.0.112` (double-v) causes ImagePullBackOff. Override the image tag in HelmRelease values: `reloader.deployment.image.tag: "v1.0.112"`. |
| k8s rclone filen backend | The `filen` rclone backend was added in rclone v1.69. Alpine's `apk add rclone` installs an older version that lacks it. Use the official rclone binary from `downloads.rclone.org` in Dockerfiles — do not rely on distro packages for the filen backend. |
| k8s application backups | immich-backup (03:00), postgres-backup (03:30), paperless-backup (04:00), gitea-backup (05:00), homebox-backup (02:00) run as `CronJob`s in each app namespace using `ghcr.io/lucid-void/backup-tools`. Restic over rclone-filen; separate repo per job at `rclone:filen:backups/restic/{name}`; 30-day retention. Scale-down jobs (immich, paperless, gitea, homebox) bring deployment to 0 replicas via `trap cleanup EXIT` — homebox and gitea back up SQLite/repos, immich/paperless dump Postgres + PVCs. `postgres-backup` reads CNPG read replica, no quiescing. Gotify notifications: priority 5 success / 8 failure (last 10 log lines on failure); token from `gotify-secret` Secret managed by `gotify-bootstrap` Job (not a SealedSecret); `optional: true` so jobs run before bootstrap completes. |
| k8s Gotify | Deployed in `monitoring` namespace at `gotify.blackcats.cc` (`gotify/server:2.6.0`, SQLite on nfs-client PVC, admin credentials in `gotify-admin-secret` SealedSecret). Token provisioning via `gotify-bootstrap` Job: creates app/client tokens through Gotify REST API, writes plain k8s Secrets into each namespace (`gotify-secret` with `GOTIFY_TOKEN`); client token in `monitoring/gotify-client-secret`. Idempotent — re-run after a DB reset to refresh all tokens. |
| k8s Homebox | `replicas: 1` must be pinned explicitly in the HelmRelease — without it an out-of-band scale-down sticks since Flux won't reconcile replicas unless chart/values change. v0.11.1 crashes on startup with `NOT NULL constraint failed: new_users.group_users` (known migration bug); minimum working version is 0.25.0. |
| k8s Zitadel bootstrap RBAC | `zitadel-bootstrap` kustomization has `targetNamespace: auth` which overrides ALL namespace fields (even explicit ones). Cross-namespace Roles/RoleBindings for other app namespaces must live in `kubernetes/apps/auth/bootstrap-rbac/` (separate kustomization, no `targetNamespace`), which `zitadel-bootstrap` depends on. |
| k8s Flux targetNamespace | `spec.targetNamespace` in a Flux Kustomization overrides the namespace on ALL namespaced resources unconditionally — including those with explicit `metadata.namespace`. Only put resources in a targetNamespace kustomization if they all belong in that one namespace. |
| k8s Reflector | Deployed in `kube-system` (emberstack/reflector chart `7.*`). Mirrors Secrets/ConfigMaps across namespaces via annotations on the source object. Used to mirror `{app}-role-secret` from `postgres` namespace into each app namespace so apps can reference the CNPG-managed DB password directly without a duplicate SealedSecret. |
| k8s DB password management | CNPG managed-role secrets (`{app}-role-secret`) in `postgres` namespace are the single source of truth for DB passwords. Reflector auto-mirrors each secret to the app namespace via `reflector.v1.k8s.emberstack.com/reflection-auto-*` annotations on the SealedSecret template. Apps reference `password` key via `secretKeyRef`. Gitea is the exception — chart only has `extraEnvFrom` (no per-key `valueFrom`), so a `gitea-db-bootstrap` Job remaps `password` → `GITEA__database__PASSWD` in a separate Secret consumed via `extraEnvFrom`. |
| k8s Gitea chart | Official `gitea-charts/gitea` v10 from `https://dl.gitea.com/charts/`. Disable bundled deps: `postgresql.enabled: false`, `postgresql-ha.enabled: false`, `redis-cluster.enabled: false`. External CNPG Postgres; memory cache/session/queue. HTTP service name: `{release-name}-http` port 3000. HTTPRoute backendRef must use `gitea-http`. Creates a **Deployment** (not StatefulSet). DB password via `extraEnvFrom: gitea-db-env` (written by `gitea-db-bootstrap` Job). |
| k8s Gitea OIDC | Callback URI: `https://gitea.blackcats.cc/user/oauth2/Zitadel/callback` — the provider name segment is case-sensitive and must match `gitea.oauth[].name` exactly. Terraform writes `gitea-oidc-secret` with a `values.yaml` key containing the full `gitea.oauth` list (including `key`, `secret`, `autoDiscoverUrl`). HelmRelease uses two `valuesFrom` entries: the static sealed secret (admin password only) and the Terraform-written OIDC secret. `DISABLE_REGISTRATION: false` + `ALLOW_ONLY_EXTERNAL_REGISTRATION: true` to allow OIDC self-register. `passwordMode: initialOnlyNoReset` on admin account (`initialSetup` is invalid in chart v10 — valid values: `keepUpdated`, `initialOnlyNoReset`, `initialOnlyRequireReset`). |
| k8s Zitadel bootstrap secret formats | Env-var style (FreshRSS, Paperless, Immich): Terraform writes flat key=value data in the Secret, consumed via `envFrom: secretRef` or directly mounted. Helm-valuesFrom style (Gitea): Terraform writes `data["values.yaml"]` containing a YAML fragment, consumed via `valuesFrom: [{kind: Secret, name: ..., valuesKey: values.yaml}]` in HelmRelease. Use the Helm-valuesFrom style when the credentials need to populate a chart values list (e.g. `gitea.oauth`). |
| k8s media stack | sonarr/radarr/prowlarr/sabnzbd/seerr in `media` namespace, all `bjw-s/app-template` v3.7.3. Linuxserver images with PUID=2202/PGID=2200. Seerr has no linuxserver image — use `ghcr.io/seerr-team/seerr` with pod `securityContext` (runAsUser/runAsGroup/fsGroup) instead of PUID/PGID env vars. Shared `media-nfs` RWX PVC for the Synology Media share; per-service `nfs-client` PVC for config. |
| k8s static NFS PV nfsvers | Talos kernel only supports NFSv4 for host-level static PV mounts — always use `nfsvers=4` in PV `mountOptions`. `nfsvers=4.1` fails with "Protocol not supported". Democratic-csi dynamic PVCs mount inside privileged containers and are unaffected by this restriction. |
| k8s gotify-telegram bridge | Python WebSocket bridge in `monitoring/gotify-telegram`; consumes Gotify `/stream?token=CLIENT_TOKEN`; forwards to Telegram Bot API. Pip deps installed via `pip install --target /tmp/pylib` + `PYTHONPATH=/tmp/pylib` — uid 65534 (nobody) cannot write to `/.local`. Use `python -u` for unbuffered output or logs won't appear in `kubectl logs`. |
| k8s backup failure notifications | Backup scripts capture all output via `exec > >(tee "$LOG") 2>&1`. On failure, the handler awk-JSON-escapes `tail -10 "$LOG"` and appends it to the Gotify message body for immediate triage without needing kubectl access. |
| k8s backup-tools image contents | `ghcr.io/lucid-void/backup-tools` bundles bash, curl, kubectl, restic, rclone, postgresql17-client — but NOT jq or python3. Scripts that need JSON parsing must use a different image. Established pattern: `alpine:3.21` + `apk add bash curl jq kubectl` at container startup (mirrors gotify-bootstrap). |
| k8s security namespace PSA | The `security` namespace has `pod-security.kubernetes.io/enforce: privileged` — required for Falco (privileged container + hostPath volumes). Non-privileged workloads (trivy-operator, kubent, security-report) schedule fine under privileged policy. Without this label the cluster-default `baseline` enforcement blocks Falco pods. |
| k8s Trivy Operator config | Chart `aquasecurity/trivy-operator` v0.32.1. Mode `standalone`: each scan job downloads the vuln DB independently (~300 MB each). Set `scanJobsConcurrentLimit: 2` to prevent disk exhaustion on initial scan burst. Do NOT override `trivy.dbRepository` with a full `ghcr.io/...` path — the chart prepends the registry, causing `mirror.gcr.io/ghcr.io/...` double-prefix. Leave at chart default (`aquasec/trivy-db`). Operator needs ≥1 Gi memory or OOMKills under full scan load. Set `operator.infraAssessmentScannerEnabled: false` — node-collector tries to `mkdir /etc/systemd` which fails on Talos's read-only root filesystem. |
| k8s Goldilocks | Deployed in `goldilocks` namespace via Fairwinds charts (`https://charts.fairwinds.com/stable`). Requires VPA (`fairwinds-stable/vpa` v4.11.0) in recommender-only mode — admission controller and updater both disabled (recommendations only, no mutation). Goldilocks chart v10.3.0: `controller.flags.on-by-default: true` monitors all namespaces; excludes `kube-system,flux-system,kube-public,kube-node-lease,default,goldilocks`. Dashboard at `goldilocks.blackcats.cc` → `goldilocks-dashboard:80`. |
| k8s Falco | Deployed in `security` namespace via `falcosecurity/falco` chart v8.0.5 (`https://falcosecurity.github.io/charts`). Must use `driver.kind: modern_ebpf` on Talos — kernel module requires `insmod` (unavailable on immutable OS), legacy eBPF requires kernel headers (not exposed by Talos). Modern eBPF uses CO-RE + BTF (`/sys/kernel/btf/vmlinux`), no host OS access needed, works on Talos 1.x out of the box. Falcosidekick enabled, routes to Gotify via `GOTIFY_TOKEN` from `security/falco-gotify-secret` (provisioned by `gotify-bootstrap`). DaemonSet runs on all 3 CP nodes; no special tolerations needed since `allowSchedulingOnControlPlanes: true` removes the NoSchedule taint. |
| k8s K8s-Cleaner | Deployed in `kube-system` via OCI chart (`oci://ghcr.io/gianlucam76/charts`, chart `k8s-cleaner` v0.20.0). Cleaner CRs (cluster-scoped, `apps.projectsveltos.io/v1alpha1`) live in a separate `k8s-cleaner-rules` Flux Kustomization that depends on `k8s-cleaner` — CRDs must be installed before Cleaner resources are applied. Two rules: `succeeded-pods` (every 6h on the hour) and `failed-pods` (every 6h at :30) delete pods by phase across all namespaces. |
| k8s gotify-bootstrap Job immutability | Job spec is immutable after creation. When the manifest changes while the completed Job is still within its 24h `ttlSecondsAfterFinished` window, Flux dry-run fails with "field is immutable". Fix: `kubectl delete job gotify-bootstrap -n monitoring` and let Flux recreate on next reconciliation. After adding a new app token, also update the echo log at the end of the job script. |
| k8s YAML block scalar + Python | Python code at 0-indent inside a YAML `|` block scalar breaks the kustomize YAML parser (scanner sees the unindented line as a new mapping key). Fix: put Python scripts as separate ConfigMap keys (each key is a file mounted alongside the shell script), or use a tool like jq that can be invoked inline without multi-line code blocks. |
| k8s Descheduler | Deployed in `kube-system` via `kubernetes-sigs/descheduler` chart v0.36.0 (HelmRepository: `https://kubernetes-sigs.github.io/descheduler/`). Runs as a CronJob every 5 minutes with default policies. Depends on `cilium` Flux Kustomization. |
| k8s kubent | Weekly CronJob in `security` namespace (Monday 08:00, alongside `security-report`). Uses `alpine:3.21` + downloads `ghcr.io/doitintl/kube-no-trouble` v0.7.3 binary at runtime. ClusterRole grants read-all access for deprecated API detection. Posts pass/fail to Gotify via `gotify-secret` (`optional: true`). Run `kubectl create job --from=cronjob/kubent` before any Talos/k8s upgrade. |

## IP map (quick reference)

```
172.16.20.2   Synology RS1219+      — physical, NFS storage only (Btrfs /volume2)
172.16.20.3   Proxmox MS-A2         — physical, hypervisor
172.16.20.4   DGX Spark             — physical, Swarm worker (GPU), WOL
172.16.20.5–9 reserved              — future physical devices
172.16.20.10  DB VM                 — Swarm worker, Postgres/MariaDB/pgadmin/adminer (Swarm services, `db` overlay, pinned here) + backup cron + rclone (host OS)
172.16.20.16  Monitoring VM         — Swarm worker, Prometheus/Loki/Grafana/exporters
172.16.20.12  Media VM              — Swarm worker, Plex/*arr/download stack
172.16.20.13  Services VM           — Swarm MANAGER, Traefik/Paperless/Immich/Gitea/Zitadel/Authelia/etc.
172.16.20.14  Game VM               — Swarm worker, Satisfactory/Netbird/ZeroTier
172.16.20.15  Lab VM                — Swarm worker, ephemeral/testing
172.16.20.17  Gitea runner LXC      — Proxmox LXC, act_runner, not a Swarm member
172.16.20.18+ future VMs/LXCs
172.16.20.254 UDM SE                — physical, gateway + DNS resolver + ad blocking
```

## IaC stack

- **Packer** — Debian base VM template stored in Proxmox
- **OpenTofu** — VM provisioning + DNS records; state in TrueNAS PostgreSQL (`tofu_state` database)
- **Ansible** — OS config, Docker, Swarm join, stack deployment, certs; hybrid inventory (static physical + dynamic Proxmox API)
- **Secrets** — SOPS + age using SSH key as recipient; encrypted files committed to git
- **Task runner** — `justfile`
- **CI/CD** — Gitea Actions on self-hosted LXC runner at .17; GitHub Actions used only for docs (GitHub Pages)
