# Service Inventory

All services are managed by FluxCD via HelmReleases or raw manifests in `kubernetes/apps/`.
Every service is reachable only on the internal network or via Netbird VPN.

## Infrastructure / Platform

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Cilium | kube-system | HelmRelease | — | — | CNI, Gateway API controller, kube-proxy replacement |
| Gateway API CRDs | kube-system | Kustomization (git) | — | — | v1.6.1 experimental channel; installs HTTPRoute, GRPCRoute, TCPRoute (v1), etc. |
| Sealed Secrets | kube-system | HelmRelease | — | — | Controller name `sealed-secrets-controller`; key rotation disabled |
| Reflector | kube-system | HelmRelease | — | — | Mirrors Secrets/ConfigMaps across namespaces |
| Reloader | kube-system | HelmRelease | — | — | Rolling restarts on ConfigMap/Secret changes; image tag override required (chart v1.0.112 double-v bug) |
| Descheduler | kube-system | HelmRelease | — | — | CronJob every 5 min, default policies |
| K8s-Cleaner | kube-system | HelmRelease | — | — | Deletes Succeeded/Failed pods every 6h |
| Spegel | kube-system | HelmRelease | — | — | P2P image cache across nodes |
| etcd-snapshot | kube-system | CronJob | — | — | Daily 01:00; `talosctl etcd snapshot` from first reachable CP (`.11`→`.12`→`.13`); restic → `rclone:filen:backups/restic/etcd-snapshot`, 30-day retention |
| CNPG operator | cnpg-system | HelmRelease | — | — | CloudNativePG; manages `postgres` cluster |
| democratic-csi | democratic-csi | HelmRelease | — | — | `nfs-client` StorageClass (default); controller mounts Synology share |
| OpenEBS | openebs | HelmRelease | — | — | `openebs-hostpath` StorageClass; LocalPV at `/var/openebs/local` |
| cert-manager | cert-manager | HelmRelease | — | — | ClusterIssuer `letsencrypt-production` (DNS-01/Cloudflare) |
| external-dns | network | HelmRelease | — | — | `gateway-httproute` + `gateway-grpcroute` sources; opt-in annotation |
| Shared Gateway | gateway | Gateway (Cilium) | `*.blackcats.cc` → 172.16.20.50 | — | Single HTTPS Gateway; wildcard cert `shared-tls` |
| VPA | goldilocks | HelmRelease | — | — | Recommender-only mode (no mutation/admission) |
| Goldilocks | goldilocks | HelmRelease | `goldilocks.blackcats.cc` | Zitadel OIDC | VPA recommendation dashboard; monitors all namespaces except system ones |

---

## Databases

| Service | Namespace | Kind | Auth | Notes |
|---|---|---|---|---|
| CNPG cluster `postgres` | postgres | Cluster (CNPG) | Per-DB roles | 2 instances (1 primary + 1 replica); custom image with pgvector + VectorChord (Immich vector search on VectorChord); connects via `postgres-rw.postgres.svc.cluster.local:5432` |
| postgres-backup | postgres | CronJob | — | Daily 03:30; dumps all k8s DBs; restic → rclone-filen |

---

## Auth

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Zitadel | auth | HelmRelease | `auth.blackcats.cc` | Self (OIDC provider) | Single user store; Go binary backed by CNPG Postgres; gRPC-Web via Cilium GRPCRoute + h2c |
| Mailrise | auth | Deployment | — | — | SMTP→Apprise relay for Zitadel email notifications |
| Zitadel bootstrap | auth | Job | — | — | Provisions OIDC clients for all apps via Terraform + Zitadel API; writes `*-oidc-secret` Secrets into app namespaces (incl. `proxmox-oidc-secret` in `auth` for the external Proxmox host) |

---

## Monitoring & Alerting

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Gotify | monitoring | HelmRelease | `gotify.blackcats.cc` | SealedSecret admin creds | `gotify/server:3.0.0`; SQLite on `nfs-client` PVC; push notifications hub |
| gotify-bootstrap | monitoring | Job | — | — | Creates app/client tokens via Gotify REST API; writes `gotify-secret` into each app namespace; idempotent — Gotify 3 only discloses tokens on create/rotate, so the destination Secret is the source of truth and is reused when present. Re-runs ~hourly (TTL 3600 + 30m reconcile) and reports unexplained repairs as drift; script lives in a hash-suffixed generated ConfigMap, so editing it recreates the Job (`force: true`) |
| flux-notifications | monitoring | Provider + Alert | — | — | Flux notification-controller `Provider` (generic webhook → Gotify) + `Alert` at `eventSeverity: error`. Closes the "meta-hole": Flux itself can't fail silently. Watches `Kustomization` cluster-wide (all in flux-system — gapless backstop) + `HelmRelease` per app namespace. Token: `flux` app token in `monitoring/flux-gotify` (`headers: X-Gotify-Key`, written by gotify-bootstrap) |
| gotify-telegram | monitoring | Deployment | — | — | Python WebSocket bridge: Gotify `/stream` → Telegram Bot API; priority colours: 🔴 ≥8, 🟡 ≥5, 🟢 <5 |
| am-gotify-bridge | monitoring | Deployment | — | — | Python HTTP bridge: AlertManager webhook → Gotify; listens :5000; reads `gotify-secret`; priority 8 firing / 5 resolved |
| Gatus | monitoring | HelmRelease | `gatus.blackcats.cc` | Zitadel OIDC | HTTP/HTTPS health checks + cert expiry for all services |
| VictoriaMetrics Stack | monitoring | HelmRelease | — | — | `victoria-metrics-k8s-stack` v0.76.0; includes VMSingle (30Gi openebs-hostpath), VMAgent, VMAlert, AlertManager, Grafana, kube-state-metrics, node-exporter |
| Grafana | monitoring | HelmRelease (subchart) | `grafana.blackcats.cc` | Zitadel OIDC | Dashboards for VictoriaMetrics data; credentials from `grafana-oidc-secret` (written by Terraform bootstrap) |

---

## Security

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Falco | security | HelmRelease | — | — | Runtime security; `driver.kind: modern_ebpf` (Talos-compatible); Falcosidekick → Gotify |
| Trivy Operator | security | HelmRelease | — | — | In-cluster CVE + config audit scanning; `scanJobsConcurrentLimit: 2` |
| security-report | security | CronJob | — | — | Weekly (Mon 08:00); queries Trivy CRDs; posts Critical/High findings to Gotify |
| kubent | security | CronJob | — | — | Weekly (Mon 08:00); deprecated API detection; run before any k8s upgrade |

---

## Applications

| Service | Namespace | Kind | Hostname | Auth | Storage |
|---|---|---|---|---|---|
| Immich | immich | HelmRelease | `immich.blackcats.cc` | Zitadel OIDC (Web app type) | `nfs-client` PVC (library); CNPG Postgres (VectorChord embeddings) |
| immich-backup | immich | CronJob | — | — | Daily 03:00; Postgres dump + library PVC; restic → rclone-filen |
| Paperless-ngx | paperless | HelmRelease | `paperless.blackcats.cc` | Zitadel OIDC (django-allauth 65.x) | `nfs-client` PVCs (data + media); CNPG Postgres |
| paperless-backup | paperless | CronJob | — | — | Daily 04:00; Postgres dump + data/media PVCs; restic → rclone-filen |
| Proton Mail Bridge | paperless | HelmRelease | — (ClusterIP `protonmail-bridge:143`, IMAP only) | Proton account, entered once via CLI; state in the pod's vault | `openebs-hostpath` PVC (vault + gluon SQLite + message cache). Not backed up — recovery is a re-login |
| Gitea | gitea | HelmRelease | `gitea.blackcats.cc` (HTTPS via HTTPRoute; git-over-SSH on `:22` via TCPRoute) | Zitadel OIDC (web); SSH keys (git) | `nfs-client` PVC (repos/LFS/attachments); CNPG Postgres; bundled `valkey-cluster` for cache/session/queue |
| gitea-valkey-cluster | gitea | StatefulSet (chart dep) | — | — | Cache/session/queue backing Gitea. 3 primaries + 1 replica each (`nodes: 6`, `replicas: 1`); `valkey-data-*` `nfs-client` PVCs. Disposable data — recreate rather than repair |
| gitea-backup | gitea | CronJob | — | — | Daily 05:00; Postgres dump + data PVC; restic → rclone-filen |
| FreshRSS | freshrss | HelmRelease | `rss.blackcats.cc` | Zitadel OIDC (Apache mod_auth_openidc) | `nfs-client` PVC (config); CNPG Postgres |
| Homebox | homebox | HelmRelease | `homebox.blackcats.cc` | Built-in | `nfs-client` PVC (SQLite data dir) |
| homebox-backup | homebox | CronJob | — | — | Daily 02:00; SQLite data dir; restic → rclone-filen |
| Joplin Server | joplin | HelmRelease | `joplin.blackcats.cc` | **Zitadel SAML** (not OIDC) + local break-glass admin | `joplin-blobs` PVC (`nfs-client`, RWO — attachments); CNPG Postgres |
| joplin-backup | joplin | CronJob | — | — | Daily 06:00; Postgres dump + blobs PVC in one snapshot; restic → rclone-filen |
| CouchDB (Obsidian LiveSync) | obsidian | HelmRelease | `obsidian.blackcats.cc` | **CouchDB HTTP Basic** (SealedSecret) — not Zitadel | `couchdb-data` PVC (`openebs-hostpath`, RWO — shards + _users) |
| obsidian-backup | obsidian | CronJob | — | — | Daily 02:30; quiesced CouchDB data dir; restic → rclone-filen |
| Homepage | homepage | HelmRelease | `home.blackcats.cc` | — | ConfigMap-only config |
| llama-swap | ai | HelmRelease | — (ClusterIP `llama-swap:8080`) | none — ClusterIP only, reached via LiteLLM | `llama-models` PVC (`openebs-hostpath`, RWO, node-local on `llm-1`). **No memory limit** — llama.cpp mmaps the GGUF and a limit near the working set causes reclaim-thrash, not a clean OOMKill. Weights are NOT backed up (re-downloadable) |
| model-fetch | ai | Job | — | — | One-shot, on `llm-1`, writes the same node-local PVC llama-swap reads. Pins each GGUF's exact byte size; a mismatch fails the job rather than leaving a truncated file |
| LiteLLM | ai | HelmRelease | `llm.blackcats.cc` | LiteLLM virtual keys (one per client), master key in `litellm-secret` | CNPG Postgres (`litellm`) for keys/budgets/spend. Memory limit **4Gi** — OOMKilled at 1Gi during the Prisma migration with no log output |
| Open WebUI | ai | HelmRelease | `chat.blackcats.cc` | Zitadel OIDC | `open-webui-data` PVC (`nfs-client`, chroma vector store + uploads); CNPG Postgres (`openwebui`) for users/chats |
| Degoog | degoog | HelmRelease | `degoog.blackcats.cc` | — | Self-hosted search engine aggregator; `ghcr.io/degoog-org/degoog:0.18.0`; `nfs-client` PVC for engines/plugins/themes data |

---

## Media

All media services are in the `media` namespace using `bjw-s/app-template` v3.7.3.
Linuxserver images with `PUID=2202` / `PGID=2200`. Shared `media-nfs` RWX PVC mounts Synology `/volume2/Media`.

| Service | Hostname | Image | Storage |
|---|---|---|---|
| Sonarr | `sonarr.blackcats.cc` | `lscr.io/linuxserver/sonarr:4.0.13` | Config PVC (`nfs-client`) + `media-nfs` |
| Radarr | `radarr.blackcats.cc` | `lscr.io/linuxserver/radarr:5.23.3` | Config PVC (`nfs-client`) + `media-nfs` |
| Prowlarr | `prowlarr.blackcats.cc` | `lscr.io/linuxserver/prowlarr:1.36.3` | Config PVC (`nfs-client`) |
| SABnzbd | `nzb.blackcats.cc` | `lscr.io/linuxserver/sabnzbd:4.5.1` | Config PVC (`nfs-client`) + `media-nfs` |
| Seerr | `seerr.blackcats.cc` | `ghcr.io/seerr-team/seerr:v3.2.0` | Config PVC (`nfs-client`) — pod `securityContext` instead of PUID/PGID |
| Plex | `plex.blackcats.cc` | `lscr.io/linuxserver/plex:1.41.7` | Config PVC (`openebs-hostpath`, pinned to cp-1) + `media-nfs` (readOnly) |
| Suwayomi | `suwayomi.blackcats.cc` | `ghcr.io/suwayomi/suwayomi-server:v2.2.2100` (+ `flaresolverr` v3.5.0) | `suwayomi-config` PVC (`nfs-client`, embedded H2) + `media-nfs` subPath `Manga` (downloads) |
| Kavita | `kavita.blackcats.cc` | `lscr.io/linuxserver/kavita:0.9.0` | `kavita-config` PVC (`nfs-client`, internal SQLite) + `media-nfs` subPath `Manga` (readOnly) |
| RomM | `romm.blackcats.cc` | `rommapp/romm:5.0.0` | `romm-config` PVC (`nfs-client`) + `media-nfs` subPath `Games` (ROM library) + `emptyDir` at `/redis-data` — CNPG Postgres for the app DB |
| matcha (Minecraft) | `matcha.blackcats.cc` (TCP 25565 via Velocity) · `matcha-files.blackcats.cc` · `matcha-map.blackcats.cc` | `itzg/minecraft-server:2026.7.2-java25` (Paper 26.2) | `matcha-data` PVC (`openebs-hostpath`) + `mc-backups` (`nfs-client`, RWX) |
| vanilla (Minecraft) | `vanilla.blackcats.cc` (TCP 25565 via Velocity) · `vanilla-files.blackcats.cc` · `vanilla-map.blackcats.cc` | `itzg/minecraft-server:2026.7.2-java25` (Paper 26.2) | `vanilla-data` PVC (`openebs-hostpath`) + `mc-backups` (`nfs-client`, RWX) |
| minecraft-proxy | — (LoadBalancer `172.16.20.52:25565`) | `itzg/mc-proxy:java25` (Velocity 3.5.1) | none |
| minecraft-valkey | — (ClusterIP `:6379`) | `valkey/valkey:9.1-alpine` | none |
| minecraft-events | — (no Service) | `ghcr.io/lucid-void/backup-tools` | none |

Sonarr and Radarr use CNPG Postgres (migrated from SQLite; migration Jobs in `kubernetes/apps/media/sonarr/app/migration-job.yml` and `radarr/`).

---

## OIDC Callback URIs (non-obvious)

| App | Callback URI | Notes |
|---|---|---|
| Immich | `https://immich.blackcats.cc/api/oauth/mobile-redirect` | Web app type in Zitadel (not Native); proxies to `app.immich:///oauth-callback` |
| Paperless | `https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/` | django-allauth 65.x path; provider_id must be `zitadel` |
| FreshRSS | `https://rss.blackcats.cc/i/oidc/` | Apache mod_auth_openidc; NOT `/i/?get=oidc` |
| Gitea | `https://gitea.blackcats.cc/user/oauth2/Zitadel/callback` | Provider name segment is case-sensitive |
| Kavita | `https://kavita.blackcats.cc/signin-oidc` | ASP.NET OIDC middleware path; creds read from `/config/appsettings.json` (`OpenIdConnectSettings`), merged in by an initContainer |
| RomM | `https://romm.blackcats.cc/api/oauth/openid` | Web app / `client_secret_basic`; needs "User Info inside ID Token" enabled in Zitadel. All `OIDC_*` vars written into `media/romm-oidc-secret` by Terraform bootstrap — except `OIDC_ALLOW_REGISTRATION` (new in v5, pinned to `true` in the HelmRelease), which must stay true for a Zitadel user's first login to create the RomM account |
| Proxmox VE | `https://pve.blackcats.cc:8006` (+ `:443`) | Bare-metal host (172.16.20.3), not in-cluster. Redirect = web UI base URL (no path); `client_secret_basic`. Creds in `auth/proxmox-oidc-secret`, copied into a Proxmox OIDC realm manually (`pveum`). See RUNBOOK. |
| Open WebUI | `https://chat.blackcats.cc/oauth/oidc/callback` | Web app / `client_secret_post`, pinned client-side via `OAUTH_TOKEN_ENDPOINT_AUTH_METHOD` to match the Zitadel app. **`ENABLE_OAUTH_SIGNUP` defaults to `false`** — without it the button renders and the first login fails with no account to create. Creds in `ai/openwebui-oidc-secret` (keys `OAUTH_CLIENT_ID`/`OAUTH_CLIENT_SECRET`) |
| Goldilocks | TBD | Standard OIDC redirect |
| Gatus | TBD | Standard OIDC redirect |

Joplin is **not** in this table — it uses SAML, not OIDC.

---

## SAML (Joplin only)

Joplin Server is the single SAML service in the cluster. Wiring:

| Piece | Where |
|---|---|
| SP metadata (static XML) | `kubernetes/apps/joplin/joplin/app/saml-sp-configmap.yml` → mounted at `/saml-sp/sp.xml` |
| IdP metadata | fetched from `https://zitadel.blackcats.cc/saml/v2/metadata` at pod start by the `saml-idp-metadata` initContainer → `/saml-idp/idp.xml` |
| Zitadel SAML app | `zitadel_application_saml.joplin` in the zitadel-bootstrap Terraform |
| Attribute rename action | `zitadel_action.joplin_saml_attributes` + `zitadel_trigger_actions` on `FLOW_TYPE_SAML_RESPONSE` / `TRIGGER_TYPE_PRE_SAML_RESPONSE_CREATION` |

**Client support is narrower than OIDC apps:** desktop needs the separate **"Joplin Server (Beta, SAML)"** sync target (login happens in a browser via a `joplin://` callback, so only one Joplin instance may run); the **CLI does not support SAML at all**.
