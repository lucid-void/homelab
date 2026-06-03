# Service Inventory

All services are managed by FluxCD via HelmReleases or raw manifests in `kubernetes/apps/`.
Every service is reachable only on the internal network or via Netbird VPN.

## Infrastructure / Platform

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Cilium | kube-system | HelmRelease | — | — | CNI, Gateway API controller, kube-proxy replacement |
| Gateway API CRDs | kube-system | Kustomization (git) | — | — | v1.5.1 experimental channel; installs HTTPRoute, GRPCRoute, etc. |
| Sealed Secrets | kube-system | HelmRelease | — | — | Controller name `sealed-secrets-controller`; key rotation disabled |
| Reflector | kube-system | HelmRelease | — | — | Mirrors Secrets/ConfigMaps across namespaces |
| Reloader | kube-system | HelmRelease | — | — | Rolling restarts on ConfigMap/Secret changes; image tag override required (chart v1.0.112 double-v bug) |
| Descheduler | kube-system | HelmRelease | — | — | CronJob every 5 min, default policies |
| K8s-Cleaner | kube-system | HelmRelease | — | — | Deletes Succeeded/Failed pods every 6h |
| Spegel | kube-system | HelmRelease | — | — | P2P image cache across nodes |
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
| CNPG cluster `postgres` | postgres | Cluster (CNPG) | Per-DB roles | 2 instances (1 primary + 1 replica); custom image with VectorChord + pgvector; connects via `postgres-rw.postgres.svc.cluster.local:5432` |
| postgres-backup | postgres | CronJob | — | Daily 03:30; dumps all k8s DBs; restic → rclone-filen |

---

## Auth

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Zitadel | auth | HelmRelease | `auth.blackcats.cc` | Self (OIDC provider) | Single user store; Go binary backed by CNPG Postgres; gRPC-Web via Cilium GRPCRoute + h2c |
| Mailrise | auth | Deployment | — | — | SMTP→Apprise relay for Zitadel email notifications |
| Zitadel bootstrap | auth | Job | — | — | Provisions OIDC clients for all apps via Terraform + Zitadel API; writes `*-oidc-secret` Secrets into app namespaces |

---

## Monitoring & Alerting

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Gotify | monitoring | HelmRelease | `gotify.blackcats.cc` | SealedSecret admin creds | `gotify/server:2.6.0`; SQLite on `nfs-client` PVC; push notifications hub |
| gotify-bootstrap | monitoring | Job | — | — | Creates app/client tokens via Gotify REST API; writes `gotify-secret` into each app namespace; idempotent |
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

The `security` namespace has `pod-security.kubernetes.io/enforce: privileged` — required for Falco (privileged container + hostPath volumes).

---

## Applications

| Service | Namespace | Kind | Hostname | Auth | Storage |
|---|---|---|---|---|---|
| Immich | immich | HelmRelease | `immich.blackcats.cc` | Zitadel OIDC (Web app type) | `nfs-client` PVC (library); CNPG Postgres |
| immich-backup | immich | CronJob | — | — | Daily 03:00; Postgres dump + library PVC; restic → rclone-filen |
| Paperless-ngx | paperless | HelmRelease | `paperless.blackcats.cc` | Zitadel OIDC (django-allauth 65.x) | `nfs-client` PVCs (data + media); CNPG Postgres |
| paperless-backup | paperless | CronJob | — | — | Daily 04:00; Postgres dump + data/media PVCs; restic → rclone-filen |
| Gitea | gitea | HelmRelease | `gitea.blackcats.cc` | Zitadel OIDC | `nfs-client` PVC (repos/LFS/attachments); CNPG Postgres |
| gitea-backup | gitea | CronJob | — | — | Daily 05:00; Postgres dump + data PVC; restic → rclone-filen |
| FreshRSS | freshrss | HelmRelease | `rss.blackcats.cc` | Zitadel OIDC (Apache mod_auth_openidc) | `nfs-client` PVC (config); CNPG Postgres |
| Homebox | homebox | HelmRelease | `homebox.blackcats.cc` | Built-in | `nfs-client` PVC (SQLite data dir) |
| homebox-backup | homebox | CronJob | — | — | Daily 02:00; SQLite data dir; restic → rclone-filen |
| Homepage | homepage | HelmRelease | `home.blackcats.cc` | — | ConfigMap-only config |
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
| Plex | `plex.blackcats.cc` | `lscr.io/linuxserver/plex:1.41.7` | Config PVC (`openebs-hostpath`, pinned to k8s-cp-1) + `media-nfs` (readOnly) |
| Tranga | `tranga.blackcats.cc` | `glax/tranga-api:latest` + `glax/tranga-website:latest` | `tranga-config` PVC (`nfs-client`) + `media-nfs` subPath `Manga`; CNPG Postgres (`tranga` DB) |
| Kavita | `kavita.blackcats.cc` | `lscr.io/linuxserver/kavita:0.9.0` | `kavita-config` PVC (`nfs-client`, internal SQLite) + `media-nfs` subPath `Manga` (readOnly) |

Plex uses `openebs-hostpath` for its config PVC — SQLite WAL locking errors occur over NFS. Config is on local disk on whichever node the PVC first bound to (k8s-cp-1).

Sonarr and Radarr use CNPG Postgres (migrated from SQLite; migration Jobs in `kubernetes/apps/media/sonarr/app/migration-job.yml` and `radarr/`).

**Manga stack** — Tranga downloads/monitors manga (Tachiyomi-style sources, mangal-free; actively maintained), Kavita reads it. Tranga is a split frontend/backend: the `website` (nginx) reverse-proxies `/api/` to the `api` controller via `API_URL`, so only `tranga-website:80` is exposed; the `api` controller is internal and talks to CNPG over separate `POSTGRES_HOST/DB/USER/PASSWORD` env (`tranga-role-secret` reflected into `media`). Both Tranga and Kavita write/read `/volume2/Media/Manga/` (PUID/PGID `2202`/`2200`). Tranga images publish only rolling channel tags (no semver) — `latest` is the stable channel, an accepted deviation from the image-pin policy. A **FlareSolverr** controller (`tranga-flaresolverr:8191`) in the Tranga HelmRelease solves Cloudflare challenges so protected connectors return chapter lists. Kavita's first admin is provisioned by the `kavita-bootstrap` Job (creds in `kavita-admin-secret`); subsequent users come from Zitadel OIDC.

---

## OIDC Callback URIs (non-obvious)

| App | Callback URI | Notes |
|---|---|---|
| Immich | `https://immich.blackcats.cc/api/oauth/mobile-redirect` | Web app type in Zitadel (not Native); proxies to `app.immich:///oauth-callback` |
| Paperless | `https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/` | django-allauth 65.x path; provider_id must be `zitadel` |
| FreshRSS | `https://rss.blackcats.cc/i/oidc/` | Apache mod_auth_openidc; NOT `/i/?get=oidc` |
| Gitea | `https://gitea.blackcats.cc/user/oauth2/Zitadel/callback` | Provider name segment is case-sensitive |
| Kavita | `https://kavita.blackcats.cc/signin-oidc` | ASP.NET OIDC middleware path; creds read from `/config/appsettings.json` (`OpenIdConnectSettings`), merged in by an initContainer |
| Goldilocks | TBD | Standard OIDC redirect |
| Gatus | TBD | Standard OIDC redirect |
