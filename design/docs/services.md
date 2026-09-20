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
| Goldilocks | goldilocks | HelmRelease | `goldilocks.blackcats.cc` | **none** | VPA recommendation dashboard; monitors all namespaces except system ones. Not on SSO — it has never had an OIDC client or secret |

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
| Keycloak | keycloak | Keycloak CR (operator) | `sso.blackcats.cc` | Self (OIDC provider) | **The identity provider.** Official Keycloak Operator from a tag-pinned GitRepository; realm `homelab` from a `KeycloakRealmImport`; CNPG Postgres. Service is `keycloak-service`. Realm mail via a socat sidecar to the Proton Bridge |
| Keycloak clients | keycloak | KeycloakOIDCClient CRs | — | — | One CR per app under `kubernetes/apps/keycloak/clients/`, each with a sealed `<app>-client-secret` that git owns. Requires the EXPERIMENTAL `client-admin-api:v2` server feature |
| Zitadel | auth | HelmRelease | `auth.blackcats.cc` | Self (OIDC provider) | **Joplin's SAML IdP only** — every OIDC app moved to Keycloak. Retired when Joplin is deleted. Go binary backed by CNPG Postgres; gRPC-Web via Cilium GRPCRoute + h2c |
| Mailrise | auth | Deployment | — | — | SMTP→Apprise relay for Zitadel email notifications |
| Zitadel bootstrap | auth | Job | — | — | Terraform + Zitadel API. Now owns **only** Joplin's SAML application, its attribute-rename action and the `homelab` project — the eighteen OIDC resources were dropped from state with `removed { lifecycle { destroy = false } }`. Writes no Secrets into app namespaces any more |

---

## Monitoring & Alerting

| Service | Namespace | Kind | Hostname | Auth | Notes |
|---|---|---|---|---|---|
| Gotify | monitoring | HelmRelease | `gotify.blackcats.cc` | SealedSecret admin creds | `gotify/server:3.0.0`; SQLite on `nfs-client` PVC; push notifications hub |
| gotify-bootstrap | monitoring | Job | — | — | Creates app/client tokens via Gotify REST API; writes `gotify-secret` into each app namespace; idempotent — Gotify 3 only discloses tokens on create/rotate, so the destination Secret is the source of truth and is reused when present. Re-runs ~hourly (TTL 3600 + 30m reconcile) and reports unexplained repairs as drift; script lives in a hash-suffixed generated ConfigMap, so editing it recreates the Job (`force: true`) |
| flux-notifications | monitoring | Provider + Alert | — | — | Flux notification-controller `Provider` (generic webhook → Gotify) + `Alert` at `eventSeverity: error`. Closes the "meta-hole": Flux itself can't fail silently. Watches `Kustomization` cluster-wide (all in flux-system — gapless backstop) + `HelmRelease` per app namespace. Token: `flux` app token in `monitoring/flux-gotify` (`headers: X-Gotify-Key`, written by gotify-bootstrap) |
| gotify-telegram | monitoring | Deployment | — | — | Python WebSocket bridge: Gotify `/stream` → Telegram Bot API; priority colours: 🔴 ≥8, 🟡 ≥5, 🟢 <5 |
| am-gotify-bridge | monitoring | Deployment | — | — | Python HTTP bridge: AlertManager webhook → Gotify; listens :5000; reads `gotify-secret`; priority 8 firing / 5 resolved |
| Gatus | monitoring | HelmRelease | `gatus.blackcats.cc` | **none** | HTTP/HTTPS health checks + cert expiry for all services. Not on SSO — it has never had an OIDC client or secret |
| VictoriaMetrics Stack | monitoring | HelmRelease | — | — | `victoria-metrics-k8s-stack` v0.76.0; includes VMSingle (30Gi openebs-hostpath), VMAgent, VMAlert, AlertManager, Grafana, kube-state-metrics, node-exporter |
| Grafana | monitoring | HelmRelease (subchart) | `grafana.blackcats.cc` | Keycloak OIDC | Dashboards for VictoriaMetrics data; credentials from the sealed `grafana-oidc-secret`. Role comes from a JMESPath `role_attribute_path` over `resource_access.grafana.roles` (client roles `admin`/`editor`/`viewer`) |

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
| Immich | immich | HelmRelease | `immich.blackcats.cc` | Keycloak OIDC | `nfs-client` PVC (library); CNPG Postgres (VectorChord embeddings) |
| immich-backup | immich | CronJob | — | — | Daily 03:00; Postgres dump + library PVC; restic → rclone-filen |
| Paperless-ngx | paperless | HelmRelease | `paperless.blackcats.cc` | Keycloak OIDC (django-allauth) | `nfs-client` PVCs (data + media); CNPG Postgres |
| paperless-backup | paperless | CronJob | — | — | Daily 04:00; Postgres dump + data/media PVCs; restic → rclone-filen |
| Proton Mail Bridge | paperless | HelmRelease | — (ClusterIP `protonmail-bridge`: `:143` IMAP for Paperless, `:25` SMTP for Keycloak) | Proton account, entered once via CLI; state in the pod's vault | `openebs-hostpath` PVC (vault + gluon SQLite + message cache). Not backed up — recovery is a re-login |
| Gitea | gitea | HelmRelease | `gitea.blackcats.cc` (HTTPS via HTTPRoute; git-over-SSH on `:22` via TCPRoute) | Keycloak OIDC (web); SSH keys (git) | `nfs-client` PVC (repos/LFS/attachments); CNPG Postgres; bundled `valkey-cluster` for cache/session/queue |
| gitea-valkey-cluster | gitea | StatefulSet (chart dep) | — | — | Cache/session/queue backing Gitea. 3 primaries + 1 replica each (`nodes: 6`, `replicas: 1`); `valkey-data-*` `nfs-client` PVCs. Disposable data — recreate rather than repair |
| gitea-backup | gitea | CronJob | — | — | Daily 05:00; Postgres dump + data PVC; restic → rclone-filen |
| FreshRSS | freshrss | HelmRelease | `rss.blackcats.cc` | Keycloak OIDC (Apache mod_auth_openidc) | `nfs-client` PVC (config); CNPG Postgres |
| Homebox | homebox | HelmRelease | `homebox.blackcats.cc` | Built-in | `nfs-client` PVC (SQLite data dir) |
| homebox-backup | homebox | CronJob | — | — | Daily 02:00; SQLite data dir; restic → rclone-filen |
| Joplin Server | joplin | HelmRelease | `joplin.blackcats.cc` | **Zitadel SAML** (not OIDC; the only remaining Zitadel consumer) + local break-glass admin | `joplin-blobs` PVC (`nfs-client`, RWO — attachments); CNPG Postgres |
| joplin-backup | joplin | CronJob | — | — | Daily 06:00; Postgres dump + blobs PVC in one snapshot; restic → rclone-filen |
| CouchDB (Obsidian LiveSync) | obsidian | HelmRelease | `obsidian.blackcats.cc` | **CouchDB HTTP Basic** (SealedSecret) — not on SSO | `couchdb-data` PVC (`openebs-hostpath`, RWO — shards + _users) |
| obsidian-backup | obsidian | CronJob | — | — | Daily 02:30; quiesced CouchDB data dir; restic → rclone-filen |
| Homepage | homepage | HelmRelease | `home.blackcats.cc` | — | ConfigMap-only config |
| llama-swap | ai | HelmRelease | — (ClusterIP `llama-swap:8080`) | none — ClusterIP only, reached via LiteLLM | `llama-models` PVC (`openebs-hostpath`, RWO, node-local on `llm-1`). **No memory limit** — llama.cpp mmaps the GGUF and a limit near the working set causes reclaim-thrash, not a clean OOMKill. Weights are NOT backed up (re-downloadable) |
| model-fetch | ai | Job | — | — | One-shot, on `llm-1`, writes the same node-local PVC llama-swap reads. Pins each GGUF's exact byte size; a mismatch fails the job rather than leaving a truncated file |
| LiteLLM | ai | HelmRelease | `llm.blackcats.cc` | LiteLLM virtual keys (one per client), master key in `litellm-secret` | CNPG Postgres (`litellm`) for keys/budgets/spend. Memory limit **4Gi** — OOMKilled at 1Gi during the Prisma migration with no log output |
| Open WebUI | ai | HelmRelease | `chat.blackcats.cc` | Keycloak OIDC | `open-webui-data` PVC (`nfs-client`, chroma vector store + uploads); CNPG Postgres (`openwebui`) for users/chats |
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
| Jellyfin | `jellyfin.blackcats.cc` | `lscr.io/linuxserver/jellyfin` | Config PVC (`openebs-hostpath`, pinned to **cp-2** — deliberately not cp-1, where Plex's is) + `media-nfs` (readOnly) + `emptyDir` at `/config/transcodes` |
| Suwayomi | `suwayomi.blackcats.cc` | `ghcr.io/suwayomi/suwayomi-server:v2.2.2100` (+ `flaresolverr` v3.5.0) | `suwayomi-config` PVC (`nfs-client`, embedded H2) + `media-nfs` subPath `Manga` (downloads) |
| Kavita | `kavita.blackcats.cc` | `lscr.io/linuxserver/kavita:0.9.0` | `kavita-config` PVC (`nfs-client`, internal SQLite) + `media-nfs` subPath `Manga` (readOnly) |
| RomM | `romm.blackcats.cc` | `rommapp/romm:5.0.0` | `romm-config` PVC (`nfs-client`) + `media-nfs` subPath `Games` (ROM library) + `emptyDir` at `/redis-data` — CNPG Postgres for the app DB |
| matcha (Minecraft) | `matcha.blackcats.cc` (TCP 25565 via Velocity) · `matcha-files.blackcats.cc` · `matcha-map.blackcats.cc` | `itzg/minecraft-server:2026.7.2-java25` (Paper 26.2) | `matcha-data` PVC (`openebs-hostpath`) + `mc-backups` (`nfs-client`, RWX) |
| vanilla (Minecraft) | `vanilla.blackcats.cc` (TCP 25565 via Velocity) · `vanilla-files.blackcats.cc` · `vanilla-map.blackcats.cc` | `itzg/minecraft-server:2026.7.2-java25` (Paper 26.2) | `vanilla-data` PVC (`openebs-hostpath`) + `mc-backups` (`nfs-client`, RWX) |
| minecraft-proxy | — (LoadBalancer `172.16.20.52:25565`) | `itzg/mc-proxy:java25` (Velocity 3.5.1) | none |
| minecraft-valkey | — (ClusterIP `:6379`) | `valkey/valkey` | none |
| minecraft-events | — (no Service) | `ghcr.io/lucid-void/backup-tools` | none |

Sonarr and Radarr use CNPG Postgres, migrated from SQLite with pgloader. The one-shot
migration Jobs were deleted once the migration was done — they ran
`WITH data only, truncate`, so re-running one would truncate the live database and
reload it from a stale `sonarr.db`. Neither was ever listed in its `kustomization.yml`.

---

## OIDC Callback URIs (non-obvious)

Every client id below is the plain application name (`gitea`, `immich`, …), taken from
the `KeycloakOIDCClient`'s `metadata.name`.

| App | Callback URI | Notes |
|---|---|---|
| Immich | `https://immich.blackcats.cc/auth/login`, `/user-settings`, `/api/oauth/mobile-redirect` | The mobile redirect proxies to `app.immich:///oauth-callback` |
| Paperless | `https://paperless.blackcats.cc/accounts/oidc/keycloak/login/callback/` | django-allauth path; the `keycloak` segment is the `provider_id` in `PAPERLESS_SOCIALACCOUNT_PROVIDERS` and must match it exactly |
| FreshRSS | `https://rss.blackcats.cc/i/oidc/` | Apache mod_auth_openidc; NOT `/i/?get=oidc` |
| Gitea | `https://gitea.blackcats.cc/user/oauth2/Keycloak/callback` | The provider-name segment is case-sensitive and comes from the auth source's name in Gitea's DB, not from the client id |
| Kavita | `https://kavita.blackcats.cc/signin-oidc` | ASP.NET OIDC middleware path; creds read from `/config/appsettings.json` (`OpenIdConnectSettings`), merged in by an initContainer |
| RomM | `https://romm.blackcats.cc/api/oauth/openid` | `client_secret_basic`. All `OIDC_*` vars come from the sealed `media/romm-oidc-secret` — except `OIDC_ALLOW_REGISTRATION`, pinned to `true` in the HelmRelease, which must stay true for a Keycloak user's first login to create the RomM account |
| Proxmox VE | `https://pve.blackcats.cc:8006` (+ `:443`) | Bare-metal host (172.16.20.3), not in-cluster. Redirect = web UI base URL (no path); `client_secret_basic`. Creds in `auth/proxmox-oidc-secret`, copied into the Proxmox `keycloak` realm manually (`pveum`, username claim `username` → `<user>@keycloak`). Not the default realm, so a Keycloak outage cannot lock the hypervisor out. See `design/runbook.md`. |
| Open WebUI | `https://chat.blackcats.cc/oauth/oidc/callback` | `client_secret_post`, pinned client-side via `OAUTH_TOKEN_ENDPOINT_AUTH_METHOD`. **`ENABLE_OAUTH_SIGNUP` defaults to `false`** — without it the button renders and the first login fails with no account to create. **`OAUTH_MERGE_ACCOUNTS_BY_EMAIL` also defaults to `false`**, so a pre-existing account with the same email is orphaned rather than linked |
| Grafana | `https://grafana.blackcats.cc/login/generic_oauth` | Role from `role_attribute_path` over `resource_access.grafana.roles`. **`oauth_allow_insecure_email_lookup` defaults false**, so Grafana matches only on `sub` — a pre-existing user with the same email collides instead of linking |

Goldilocks and Gatus are **not** in this table and never have been — neither has an OIDC
client or a secret. They are not on SSO.

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
