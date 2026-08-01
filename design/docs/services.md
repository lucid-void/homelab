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
| gotify-bootstrap | monitoring | Job | — | — | Creates app/client tokens via Gotify REST API; writes `gotify-secret` into each app namespace; idempotent — Gotify 3 only discloses tokens on create/rotate, so the destination Secret is the source of truth and is reused when present |
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

The `security` namespace has `pod-security.kubernetes.io/enforce: privileged` — required for Falco (privileged container + hostPath volumes).

---

## Applications

| Service | Namespace | Kind | Hostname | Auth | Storage |
|---|---|---|---|---|---|
| Immich | immich | HelmRelease | `immich.blackcats.cc` | Zitadel OIDC (Web app type) | `nfs-client` PVC (library); CNPG Postgres (VectorChord embeddings) |
| immich-backup | immich | CronJob | — | — | Daily 03:00; Postgres dump + library PVC; restic → rclone-filen |
| Paperless-ngx | paperless | HelmRelease | `paperless.blackcats.cc` | Zitadel OIDC (django-allauth 65.x) | `nfs-client` PVCs (data + media); CNPG Postgres |
| paperless-backup | paperless | CronJob | — | — | Daily 04:00; Postgres dump + data/media PVCs; restic → rclone-filen |
| Gitea | gitea | HelmRelease | `gitea.blackcats.cc` | Zitadel OIDC | `nfs-client` PVC (repos/LFS/attachments); CNPG Postgres; bundled `valkey-cluster` for cache/session/queue |
| gitea-valkey-cluster | gitea | StatefulSet (chart dep) | — | — | Cache/session/queue backing Gitea. 3 primaries + 1 replica each (`nodes: 6`, `replicas: 1`); `valkey-data-*` `nfs-client` PVCs. Disposable data — recreate rather than repair |
| gitea-backup | gitea | CronJob | — | — | Daily 05:00; Postgres dump + data PVC; restic → rclone-filen |
| FreshRSS | freshrss | HelmRelease | `rss.blackcats.cc` | Zitadel OIDC (Apache mod_auth_openidc) | `nfs-client` PVC (config); CNPG Postgres |
| Homebox | homebox | HelmRelease | `homebox.blackcats.cc` | Built-in | `nfs-client` PVC (SQLite data dir) |
| homebox-backup | homebox | CronJob | — | — | Daily 02:00; SQLite data dir; restic → rclone-filen |
| Joplin Server | joplin | HelmRelease | `joplin.blackcats.cc` | **Zitadel SAML** (not OIDC) + local break-glass admin | `joplin-blobs` PVC (`nfs-client`, RWO — attachments); CNPG Postgres |
| joplin-backup | joplin | CronJob | — | — | Daily 06:00; Postgres dump + blobs PVC in one snapshot; restic → rclone-filen |
| Homepage | homepage | HelmRelease | `home.blackcats.cc` | — | ConfigMap-only config |
| Degoog | degoog | HelmRelease | `degoog.blackcats.cc` | — | Self-hosted search engine aggregator; `ghcr.io/degoog-org/degoog:0.18.0`; `nfs-client` PVC for engines/plugins/themes data |

**Joplin Server** (notes sync, `joplin/server:3.7.1`, port 22300) is the only service authenticating by **SAML** rather than OIDC — upstream has no OIDC support ([#14252](https://github.com/laurent22/joplin/issues/14252)) and offers SAML only. Single `app-template` controller `app` → Deployment/Service `joplin`. `STORAGE_DRIVER: "Type=Filesystem; Path=/mnt/joplin-blobs"` is set deliberately: the default `Type=Database` would push every note attachment into the shared CNPG cluster's 20Gi PVC. `SIGNUP_ENABLED=false`; `LOCAL_AUTH_ENABLED=true` is kept on so `admin@localhost` remains a break-glass account while the SAML sync target is still upstream-beta. See the SAML section below for the wiring and its gotchas.

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
| matcha (Minecraft) | `matcha.blackcats.cc` (TCP 25565 via mc-router) · `matcha-files.blackcats.cc` · `matcha-map.blackcats.cc` | `itzg/minecraft-server:2026.7.2-java25` (Paper 26.2) | `matcha-data` PVC (`openebs-hostpath`) + `mc-backups` (`nfs-client`, RWX) |
| vanilla (Minecraft) | `vanilla.blackcats.cc` (TCP 25565 via mc-router) · `vanilla-files.blackcats.cc` · `vanilla-map.blackcats.cc` | `itzg/minecraft-server:2026.7.2-java25` (Paper 26.2) | `vanilla-data` PVC (`openebs-hostpath`) + `mc-backups` (`nfs-client`, RWX) |
| mc-router | — (LoadBalancer `172.16.20.51:25565`) | `itzg/mc-router:1.45.2` | none |

Plex uses `openebs-hostpath` for its config PVC — SQLite WAL locking errors occur over NFS. Config is on local disk on whichever node the PVC first bound to (cp-1).

Sonarr and Radarr use CNPG Postgres (migrated from SQLite; migration Jobs in `kubernetes/apps/media/sonarr/app/migration-job.yml` and `radarr/`).

**RomM** (game ROM manager, à la Sonarr/Radarr for consoles) scans a ROM library, fetches box art/metadata, and serves the library to Tinfoil/DBI on a modded Switch. Single `app-template` controller (`app` → Deployment/Service `romm`, port 8080). External CNPG Postgres (`ROMM_DB_DRIVER=postgresql`, password mirrored from `romm-role-secret`); embedded Valkey persists to an `emptyDir` at `/redis-data` (kept off NFS). Library on `media-nfs` subPath `Games` at `/romm/library` — organise ROMs as `Games/roms/<platform>/…` (e.g. `Games/roms/switch/*.nsp`). Unlike the linuxserver media apps, the `rommapp/romm` image runs as **root** and ignores `PUID/PGID` ([rommapp/romm#1302](https://github.com/rommapp/romm/issues/1302)) — it primarily reads the share. `ROMM_AUTH_SECRET_KEY` (session signing) comes from the `romm-secret` SealedSecret; `HASHEOUS_API_ENABLED=true` gives keyless metadata out of the box, IGDB creds (Twitch dev app) are an optional add-on. OIDC via Zitadel is enabled purely by the presence of `romm-oidc-secret` (Terraform-written, optional `envFrom`); the first user is created through RomM's own setup wizard.

**Minecraft stack** — two servers, `matcha` and `vanilla`, both **Paper 26.2** on `itzg/minecraft-server:2026.7.2-java25`. The names distinguish content, not engine: `matcha` carries plugins, `vanilla` runs plugin-free. Each is an `app-template` HelmRelease with three containers in one pod: the server, an `itzg/mc-backup` sidecar, and a `filebrowser` sidecar.

**The `-javaNN` image variant must be ≥ the Java the server jar was compiled against.** MC 26.2 needs Java 25 (class file version 69.0); pinning `-java21` (class file 65.0) crash-loops with `UnsupportedClassVersionError` before the server ever starts. Bump this alongside `VERSION`.

*Storage:* world data is on `openebs-hostpath`, **not** NFS. Region files are random small I/O inside large files, and a chunk load that blocks the single-threaded tick loop shows up directly as TPS drop; NFS also puts the world behind the same spindles serving Plex/SAB. Node-pinning costs nothing here because all three CPs are VMs on one Proxmox host. The `mc-backup` sidecar takes RCON-quiesced tarballs (`save-off` → `save-all` → `save-on`) every 2h onto the RWX `mc-backups` NFS PVC, and the `minecraft-backup` CronJob (07:00) restics those to Filen. Continuous rsync of a live world is deliberately avoided — it produces torn region files that only fail at restore time.

*Routing:* Minecraft is raw TCP and never touches the Gateway. `mc-router` holds `172.16.20.51:25565` (sharing pool-b's single IP with Plex via `lbipam.cilium.io/sharing-key`) and demultiplexes on the hostname in the client handshake. It discovers backends by watching Services for `mc-router.itzg.me/externalServerName`, so adding a server is a new HelmRelease plus that annotation plus a name on the mc-router Service's `external-dns` hostname list. This is the only reason external-dns has `service` in its `sources`.

*Monitoring:* `monitoring/minecraft-monitoring` — an `mc-monitor` Deployment pings both servers over the Server List Ping protocol (so it works identically for Paper and vanilla, and survives version upgrades), plus mc-router's own Prometheus metrics on a separate ClusterIP. 8 `VMRule` alerts → Alertmanager → Gotify, a Grafana dashboard (`uid: minecraft`), and two Gatus `tcp://` checks against the public hostnames. There is deliberately **no TPS metric** — that needs a server-side plugin and none is maintained for MC 26.2, so tick health is inferred from ping response time (answered on the main thread) and container CPU.

*Content:* the Paper/Fabric jar is never uploaded — `TYPE` + `VERSION` make the image fetch and update it. Plugins are declared as `MODRINTH_PROJECTS` / `SPIGET_RESOURCES` / `PLUGINS` env, so the plugin set lives in git and survives losing the PVC. Before adding a Modrinth slug, check its `loaders` include `paper` and its `game_versions` include the pinned `VERSION` — an unresolvable project fails startup, and plenty of well-known plugins (`spark`) publish mod-loader builds to Modrinth but not Paper ones. The filebrowser sidecar (`{server}-files.blackcats.cc`, local auth from `minecraft-secret`, not Zitadel) exists only for what those can't express: datapacks, world imports, in-browser config edits. Its readiness probe is deliberately disabled — any container's readiness gates Pod readiness, and a file-manager hiccup must not pull the Service endpoint and cut players off.

**Manga stack** — Suwayomi-Server downloads manga via the Tachiyomi/Mihon extension ecosystem (hundreds of sources installed at runtime), Kavita reads it. (Tranga was tried first but removed — its 4-connector set couldn't reliably source licensed English titles like Witch Hat Atelier.) Suwayomi is a single HelmRelease with two controllers: `app` (`suwayomi-app:4567`, embedded H2 — no CNPG) and `flaresolverr` (`suwayomi-flaresolverr:8191`) for Cloudflare-gated sources. Config is all env (`DOWNLOAD_AS_CBZ=true`, `AUTH_MODE=none`, `FLARESOLVERR_*`). Runs as uid/gid `2202`/`2200`; data dir on `suwayomi-config`, with `media-nfs` subPath `Manga` nested-mounted at `…/Tachidesk/downloads` so CBZs land on `/volume2/Media/Manga/` for Kavita. Kavita's first admin is provisioned by the `kavita-bootstrap` Job (creds in `kavita-admin-secret`); subsequent users come from Zitadel OIDC.

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
| Goldilocks | TBD | Standard OIDC redirect |
| Gatus | TBD | Standard OIDC redirect |

Joplin is **not** in this table — it uses SAML, not OIDC. See below.

---

## SAML (Joplin only)

Joplin Server is the single SAML service in the cluster. Wiring:

| Piece | Where |
|---|---|
| SP metadata (static XML) | `kubernetes/apps/joplin/joplin/app/saml-sp-configmap.yml` → mounted at `/saml-sp/sp.xml` |
| IdP metadata | fetched from `https://zitadel.blackcats.cc/saml/v2/metadata` at pod start by the `saml-idp-metadata` initContainer → `/saml-idp/idp.xml` |
| Zitadel SAML app | `zitadel_application_saml.joplin` in the zitadel-bootstrap Terraform |
| Attribute rename action | `zitadel_action.joplin_saml_attributes` + `zitadel_trigger_actions` on `FLOW_TYPE_SAML_RESPONSE` / `TRIGGER_TYPE_PRE_SAML_RESPONSE_CREATION` |

ACS URL is `https://joplin.blackcats.cc/api/saml` (fixed by Joplin, `routes/api/login.ts`); entityID is `https://joplin.blackcats.cc`. **The SP metadata exists in two places and must stay byte-identical** — the ConfigMap Joplin feeds to samlify, and the `metadata_xml` Terraform registers with Zitadel. A mismatch in entityID or ACS Location fails the assertion audience check.

The IdP metadata is fetched fresh each boot rather than pinned in git because Zitadel's signing certificate rotates. The initContainer retries for 5 minutes so a cold start where Zitadel isn't serving yet doesn't wedge the pod.

Required env beyond `SAML_ENABLED`: `API_BASE_URL` must equal `APP_BASE_URL` (SAML is unsupported on a split API domain), and `DELETE_EXPIRED_SESSIONS_SCHEDULE=""` disables the 6-hourly session purge, which assumes clients can silently re-login over the API — impossible when login is a manual browser flow.

**Client support is narrower than OIDC apps:** desktop needs the separate **"Joplin Server (Beta, SAML)"** sync target (login happens in a browser via a `joplin://` callback, so only one Joplin instance may run); the **CLI does not support SAML at all**.
