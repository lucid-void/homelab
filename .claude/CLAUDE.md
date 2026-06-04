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
| Compute platform | Talos Linux k8s cluster, FluxCD GitOps. 3 control planes (`.20`–`.22`, schedulable), API VIP `.19`, Gateway VIP `.50`. No dedicated workers. |
| Ingress | Cilium Gateway API only — `HTTPRoute`/`GRPCRoute` → `shared` Gateway in `gateway` namespace. Never `Ingress` objects. No Traefik. |
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
| Offsite backups | restic over rclone crypt → Filen cloud; rclone needs v1.69+ for the `filen` backend (official binary, not Alpine apk). |
| Application backups | Per-app `CronJob`s using `ghcr.io/lucid-void/backup-tools`; separate restic repo per job at `rclone:filen:backups/restic/{name}`; 30-day retention. See the k8s decisions row for the full schedule/quiescing behavior. |
| PBS / VM backups | None — primary recovery path is `tofu apply` + talhelper + Flux reconciliation, data restored from Filen. |
| Image pinning | Minor semver or exact tag (e.g. `traefik:v3.1`) — never rolling major tags (`latest`, `3`), no digest pinning. |
| UniFi backup | Not backed up — VLAN/firewall rules reconfigured manually after reset. |

### Auth & identity
| Topic | Decision |
|---|---|
| SSO provider | Zitadel at `zitadel.blackcats.cc` — single user store, manages all credentials and 2FA; Go binary backed by Postgres. |
| SSO model | Apps with native OIDC connect directly to Zitadel; client/secret provisioned per-app by the Terraform bootstrap job (see k8s Zitadel decisions). |

### Service-specific
| Topic | Decision |
|---|---|
| Immich OAuth | Zitadel Web app type + `/api/oauth/mobile-redirect` endpoint as redirect URI (proxies to `app.immich:///oauth-callback`); Web type required because Native type rejects https:// redirect URIs. |
| Immich user migration | Must transfer `asset` + `album` + `person` rows; omitting `person` breaks mobile sync (FK violation on `asset_face_entity`). |
| Plex | Runs in k8s (`media` namespace, `replicas: 1`, `lscr.io/linuxserver/plex`). Web via HTTPRoute; direct/GDM via a `pool-b` LoadBalancer at `172.16.20.51:32400` (`ADVERTISE_IP` set accordingly). Transcoding is CPU-only today — no GPU device plugin wired in yet. |

### Kubernetes stack
| Topic | Decision |
|---|---|
| app-template service naming | app-template v3.7.3: single service named `app` → k8s Service is `{release-name}` (no suffix); two or more services → all get `{release-name}-{service-name}`. Same rule applies to Deployments: single controller named `app` → Deployment is `{release-name}` (e.g. `homebox`, `gitea`); multiple controllers → `{release-name}-{controller}` (e.g. `paperless-app`). HTTPRoute backendRef and RBAC resourceNames must match exactly — verify with `kubectl get deployments -n <ns>` before writing. |
| FreshRSS OIDC | Uses Apache mod_auth_openidc (`OIDC_ENABLED=1`). Redirect URI is `https://rss.blackcats.cc/i/oidc/` (NOT `/i/?get=oidc` — that's the old PHP lib path). Required env vars: `OIDC_CLIENT_CRYPTO_KEY` (session passphrase), `OIDC_REMOTE_USER_CLAIM`, `OIDC_X_FORWARDED_HEADERS` uses real header names (`X-Forwarded-Host`) not PHP var names. Client/secret come from `freshrss-oidc-secret` written by Terraform bootstrap. |
| Paperless OIDC | Via django-allauth 65.x: `PAPERLESS_SOCIALACCOUNT_PROVIDERS` JSON env var with `openid_connect.APPS[].provider_id = "zitadel"`. Callback URI: `https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/` (allauth 65.x path is `/accounts/oidc/<provider_id>/` — NOT `/accounts/<provider_id>/`). Must also set `PAPERLESS_APPS: "allauth.socialaccount.providers.openid_connect"` — without this the provider is not registered in INSTALLED_APPS and the login button never appears. Also needs `PAPERLESS_ACCOUNT_DEFAULT_HTTP_PROTOCOL=https` and `PAPERLESS_ACCOUNT_EMAIL_VERIFICATION=none`. Config written by Terraform into `paperless-oidc-secret`. |
| Stakater Reloader image tag | Chart `v1.0.112` has a bug: `appVersion: vv1.0.112` (double-v) causes ImagePullBackOff. Override the image tag in HelmRelease values: `reloader.deployment.image.tag: "v1.0.112"`. |
| rclone filen backend | The `filen` rclone backend was added in rclone v1.69. Alpine's `apk add rclone` installs an older version that lacks it. Use the official rclone binary from `downloads.rclone.org` in Dockerfiles — do not rely on distro packages for the filen backend. |
| application backups | immich-backup (03:00), postgres-backup (03:30), paperless-backup (04:00), gitea-backup (05:00), homebox-backup (02:00) run as `CronJob`s in each app namespace using `ghcr.io/lucid-void/backup-tools`. Restic over rclone-filen; separate repo per job at `rclone:filen:backups/restic/{name}`; 30-day retention. Scale-down jobs (immich, paperless, gitea, homebox) bring deployment to 0 replicas via `trap cleanup EXIT` — homebox and gitea back up SQLite/repos, immich/paperless dump Postgres + PVCs. `postgres-backup` reads CNPG read replica, no quiescing. Gotify notifications: priority 5 success / 8 failure (last 10 log lines on failure); token from `gotify-secret` Secret managed by `gotify-bootstrap` Job (not a SealedSecret); `optional: true` so jobs run before bootstrap completes. |
| Gotify | Deployed in `monitoring` namespace at `gotify.blackcats.cc` (`gotify/server:2.6.0`, SQLite on nfs-client PVC, admin credentials in `gotify-admin-secret` SealedSecret). Token provisioning via `gotify-bootstrap` Job: creates app/client tokens through Gotify REST API, writes plain k8s Secrets into each namespace (`gotify-secret` with `GOTIFY_TOKEN`); client token in `monitoring/gotify-client-secret`. Idempotent — re-run after a DB reset to refresh all tokens. |
| Homebox | `replicas: 1` must be pinned explicitly in the HelmRelease — without it an out-of-band scale-down sticks since Flux won't reconcile replicas unless chart/values change. v0.11.1 crashes on startup with `NOT NULL constraint failed: new_users.group_users` (known migration bug); minimum working version is 0.25.0. |
| Zitadel bootstrap RBAC | `zitadel-bootstrap` kustomization has `targetNamespace: auth` which overrides ALL namespace fields (even explicit ones). Cross-namespace Roles/RoleBindings for other app namespaces must live in `kubernetes/apps/auth/bootstrap-rbac/` (separate kustomization, no `targetNamespace`), which `zitadel-bootstrap` depends on. |
| Flux targetNamespace | `spec.targetNamespace` in a Flux Kustomization overrides the namespace on ALL namespaced resources unconditionally — including those with explicit `metadata.namespace`. Only put resources in a targetNamespace kustomization if they all belong in that one namespace. |
| Reflector | Deployed in `kube-system` (emberstack/reflector chart `7.*`). Mirrors Secrets/ConfigMaps across namespaces via annotations on the source object. Used to mirror `{app}-role-secret` from `postgres` namespace into each app namespace so apps can reference the CNPG-managed DB password directly without a duplicate SealedSecret. |
| DB password management | CNPG managed-role secrets (`{app}-role-secret`) in `postgres` namespace are the single source of truth for DB passwords. Reflector auto-mirrors each secret to the app namespace via `reflector.v1.k8s.emberstack.com/reflection-auto-*` annotations on the SealedSecret template. Apps reference `password` key via `secretKeyRef`. Gitea is the exception — chart only has `extraEnvFrom` (no per-key `valueFrom`), so a `gitea-db-bootstrap` Job remaps `password` → `GITEA__database__PASSWD` in a separate Secret consumed via `extraEnvFrom`. |
| Gitea chart | Official `gitea-charts/gitea` v10 from `https://dl.gitea.com/charts/`. Disable bundled deps: `postgresql.enabled: false`, `postgresql-ha.enabled: false`, `redis-cluster.enabled: false`. External CNPG Postgres; memory cache/session/queue. HTTP service name: `{release-name}-http` port 3000. HTTPRoute backendRef must use `gitea-http`. Creates a **Deployment** (not StatefulSet). DB password via `extraEnvFrom: gitea-db-env` (written by `gitea-db-bootstrap` Job). |
| Gitea OIDC | Callback URI: `https://gitea.blackcats.cc/user/oauth2/Zitadel/callback` — the provider name segment is case-sensitive and must match `gitea.oauth[].name` exactly. Terraform writes `gitea-oidc-secret` with a `values.yaml` key containing the full `gitea.oauth` list (including `key`, `secret`, `autoDiscoverUrl`). HelmRelease uses two `valuesFrom` entries: the static sealed secret (admin password only) and the Terraform-written OIDC secret. `DISABLE_REGISTRATION: false` + `ALLOW_ONLY_EXTERNAL_REGISTRATION: true` to allow OIDC self-register. `passwordMode: initialOnlyNoReset` on admin account (`initialSetup` is invalid in chart v10 — valid values: `keepUpdated`, `initialOnlyNoReset`, `initialOnlyRequireReset`). |
| Zitadel bootstrap secret formats | Env-var style (FreshRSS, Paperless, Immich): Terraform writes flat key=value data in the Secret, consumed via `envFrom: secretRef` or directly mounted. Helm-valuesFrom style (Gitea): Terraform writes `data["values.yaml"]` containing a YAML fragment, consumed via `valuesFrom: [{kind: Secret, name: ..., valuesKey: values.yaml}]` in HelmRelease. Use the Helm-valuesFrom style when the credentials need to populate a chart values list (e.g. `gitea.oauth`). |
| media stack | sonarr/radarr/prowlarr/sabnzbd/seerr in `media` namespace, all `bjw-s/app-template` v3.7.3. Linuxserver images with PUID=2202/PGID=2200. Seerr has no linuxserver image — use `ghcr.io/seerr-team/seerr` with pod `securityContext` (runAsUser/runAsGroup/fsGroup) instead of PUID/PGID env vars. Shared `media-nfs` RWX PVC for the Synology Media share; per-service `nfs-client` PVC for config. |
| manga stack | Suwayomi-Server (downloader) + Kavita (reader) in `media` namespace, both `app-template` v3.7.3, writing/reading `media-nfs` subPath `Manga`. **Tranga was tried and removed** — its 4-connector set (WeebCentral/MangaDex/AsuraComic/Mangaworld) can't reliably source licensed English titles (e.g. Witch Hat Atelier: MangaDex licensed-empty; WeebCentral renders but its image-URL extraction is broken upstream). **Kaizoku + mangal are archived — do not deploy those either.** Suwayomi uses the **Tachiyomi/Mihon extension ecosystem** (hundreds of sources installed at runtime via the web UI), so it has far broader coverage. Single `app-template` HelmRelease: controller `app` (`ghcr.io/suwayomi/suwayomi-server`, port 4567, embedded H2 DB — **no CNPG**) + controller `flaresolverr` (`ghcr.io/flaresolverr/flaresolverr` v3.5.0, service `suwayomi-flaresolverr:8191`); services are `suwayomi-app`/`suwayomi-flaresolverr` (HTTPRoute → `suwayomi-app:4567`). All config via env (`BIND_PORT`, `DOWNLOAD_AS_CBZ=true`, `AUTH_MODE=none`, `FLARESOLVERR_ENABLED`/`FLARESOLVERR_URL`); see `server-reference.conf` upstream. Runs as uid 2202/gid 2200 (the image `chmod 777`s `/home/suwayomi` so arbitrary UIDs work). Data dir `/home/suwayomi/.local/share/Tachidesk` on the `suwayomi-config` nfs-client PVC; **downloads are a nested mount** — `media-nfs` subPath `Manga` mounted at `…/Tachidesk/downloads` so CBZs land on the Synology share for Kavita. No auth of its own (VPN-gated like the rest of the media stack). **Suwayomi ships with NO extension repos** (legal) — the `EXTENSION_REPOS` env seeds the Keiyoushi repo (`["https://github.com/keiyoushi/extensions/tree/repo"]`) so sources are installable; installing a source + adding a manga is runtime app-state (done in the web UI, not GitOps). For licensed English titles (e.g. Witch Hat Atelier) use a source like ComicK/Bato/WeebCentral (Keiyoushi), **not** MangaDex (licensed-empty). Kavita's first admin is created by an idempotent `kavita-bootstrap` Job (POST `/api/account/register`; first user auto-becomes admin; creds from `kavita-admin-secret` SealedSecret; HTTP 400 = admin already exists, treated as success). |
| Kavita OIDC | Kavita reads OIDC creds **only from `/config/appsettings.json`** under key `OpenIdConnectSettings` (`Authority`+`ClientId`+`Secret`, all three required for `Enabled`) — it does NOT bind env vars and manages this file itself. Callback URI: `https://kavita.blackcats.cc/signin-oidc` (ASP.NET middleware const). Wiring: Terraform registers the Zitadel app + writes flat `kavita-oidc-secret` (`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`) in `media`; an `alpine`+`jq` initContainer (`oidc-config`) idempotently merges `{Authority,ClientId,Secret}` into appsettings.json on each boot (`Authority` static). app-template **initContainer** quirks: env is the raw k8s array schema and only allows `value`/`fieldRef`/`resourceFieldRef` (no `secretKeyRef`), and `secretRef.optional` is stripped — so the secret is injected via non-optional `envFrom`; the pod waits in CreateContainerConfigError until `zitadel-bootstrap` writes it, then self-heals. `reloader.stakater.com/auto` restarts Kavita on credential rotation. Behavioral toggles (account provisioning, role sync) are set in Kavita's admin UI. |
| static NFS PV nfsvers | Talos kernel only supports NFSv4 for host-level static PV mounts — always use `nfsvers=4` in PV `mountOptions`. `nfsvers=4.1` fails with "Protocol not supported". Democratic-csi dynamic PVCs mount inside privileged containers and are unaffected by this restriction. |
| gotify-telegram bridge | Python WebSocket bridge in `monitoring/gotify-telegram`; consumes Gotify `/stream?token=CLIENT_TOKEN`; forwards to Telegram Bot API. Pip deps installed via `pip install --target /tmp/pylib` + `PYTHONPATH=/tmp/pylib` — uid 65534 (nobody) cannot write to `/.local`. Use `python -u` for unbuffered output or logs won't appear in `kubectl logs`. |
| backup failure notifications | Backup scripts capture all output via `exec > >(tee "$LOG") 2>&1`. On failure, the handler awk-JSON-escapes `tail -10 "$LOG"` and appends it to the Gotify message body for immediate triage without needing kubectl access. |
| backup-tools image contents | `ghcr.io/lucid-void/backup-tools` bundles bash, curl, kubectl, restic, rclone, postgresql17-client — but NOT jq or python3. Scripts that need JSON parsing must use a different image. Established pattern: `alpine:3.21` + `apk add bash curl jq kubectl` at container startup (mirrors gotify-bootstrap). |
| security namespace PSA | The `security` namespace has `pod-security.kubernetes.io/enforce: privileged` — required for Falco (privileged container + hostPath volumes). Non-privileged workloads (trivy-operator, kubent, security-report) schedule fine under privileged policy. Without this label the cluster-default `baseline` enforcement blocks Falco pods. |
| Trivy Operator config | Chart `aquasecurity/trivy-operator` v0.32.1. Mode `standalone`: each scan job downloads the vuln DB independently (~300 MB each). Set `scanJobsConcurrentLimit: 2` to prevent disk exhaustion on initial scan burst. Do NOT override `trivy.dbRepository` with a full `ghcr.io/...` path — the chart prepends the registry, causing `mirror.gcr.io/ghcr.io/...` double-prefix. Leave at chart default (`aquasec/trivy-db`). Operator needs ≥1 Gi memory or OOMKills under full scan load. Set `operator.infraAssessmentScannerEnabled: false` — node-collector tries to `mkdir /etc/systemd` which fails on Talos's read-only root filesystem. |
| Goldilocks | Deployed in `goldilocks` namespace via Fairwinds charts (`https://charts.fairwinds.com/stable`). Requires VPA (`fairwinds-stable/vpa` v4.11.0) in recommender-only mode — admission controller and updater both disabled (recommendations only, no mutation). Goldilocks chart v10.3.0: `controller.flags.on-by-default: true` monitors all namespaces; excludes `kube-system,flux-system,kube-public,kube-node-lease,default,goldilocks`. Dashboard at `goldilocks.blackcats.cc` → `goldilocks-dashboard:80`. |
| Falco | Deployed in `security` namespace via `falcosecurity/falco` chart v8.0.5 (`https://falcosecurity.github.io/charts`). Must use `driver.kind: modern_ebpf` on Talos — kernel module requires `insmod` (unavailable on immutable OS), legacy eBPF requires kernel headers (not exposed by Talos). Modern eBPF uses CO-RE + BTF (`/sys/kernel/btf/vmlinux`), no host OS access needed, works on Talos 1.x out of the box. Falcosidekick enabled, routes to Gotify via `GOTIFY_TOKEN` from `security/falco-gotify-secret` (provisioned by `gotify-bootstrap`). DaemonSet runs on all 3 CP nodes; no special tolerations needed since `allowSchedulingOnControlPlanes: true` removes the NoSchedule taint. |
| K8s-Cleaner | Deployed in `kube-system` via OCI chart (`oci://ghcr.io/gianlucam76/charts`, chart `k8s-cleaner` v0.20.0). Cleaner CRs (cluster-scoped, `apps.projectsveltos.io/v1alpha1`) live in a separate `k8s-cleaner-rules` Flux Kustomization that depends on `k8s-cleaner` — CRDs must be installed before Cleaner resources are applied. Two rules: `succeeded-pods` (every 6h on the hour) and `failed-pods` (every 6h at :30) delete pods by phase across all namespaces. |
| gotify-bootstrap Job immutability | Job spec is immutable after creation. When the manifest changes while the completed Job is still within its 24h `ttlSecondsAfterFinished` window, Flux dry-run fails with "field is immutable". Fix: `kubectl delete job gotify-bootstrap -n monitoring` and let Flux recreate on next reconciliation. After adding a new app token, also update the echo log at the end of the job script. |
| YAML block scalar + Python | Python code at 0-indent inside a YAML `|` block scalar breaks the kustomize YAML parser (scanner sees the unindented line as a new mapping key). Fix: put Python scripts as separate ConfigMap keys (each key is a file mounted alongside the shell script), or use a tool like jq that can be invoked inline without multi-line code blocks. |
| Descheduler | Deployed in `kube-system` via `kubernetes-sigs/descheduler` chart v0.36.0 (HelmRepository: `https://kubernetes-sigs.github.io/descheduler/`). Runs as a CronJob every 5 minutes with default policies. Depends on `cilium` Flux Kustomization. |
| kubent | Weekly CronJob in `security` namespace (Monday 08:00, alongside `security-report`). Uses `alpine:3.21` + downloads `ghcr.io/doitintl/kube-no-trouble` v0.7.3 binary at runtime. ClusterRole grants read-all access for deprecated API detection. Posts pass/fail to Gotify via `gotify-secret` (`optional: true`). Run `kubectl create job --from=cronjob/kubent` before any Talos/k8s upgrade. |
| Monitoring stack | VictoriaMetrics (vm-stack) + Grafana in the `monitoring` namespace, with alerting routed to Gotify. `monitoring` namespace has PSA `privileged`. Replaces the former Swarm Prometheus/Loki/Grafana stack. |

## IP map (quick reference)

```
172.16.20.2    Synology RS1219+   — physical, NFS storage only (Btrfs /volume2)
172.16.20.3    Proxmox MS-A2      — physical, hypervisor (hosts the Talos VMs)
172.16.20.4    DGX Spark          — physical, GPU box, WOL (not a k8s node)
172.16.20.19   API VIP            — kube-apiserver endpoint (floats via leader election)
172.16.20.20   k8s-cp-1           — Talos control plane (schedulable, runs workloads)
172.16.20.21   k8s-cp-2           — Talos control plane (schedulable, runs workloads)
172.16.20.22   k8s-cp-3           — Talos control plane (schedulable, runs workloads)
172.16.20.23   VPN VM             — ZeroTier (plain compose, outside cluster)
172.16.20.50   Gateway VIP        — pool-a, shared Gateway ingress (Cilium L2)
172.16.20.51   pool-b             — direct LoadBalancer services (e.g. Plex)
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
- **CI/CD** — GitHub Actions: image builds → GHCR (`backup-tools`, `postgres-cnpg-immich`) and PR gates (`manifest-scan` = kubeconform + kube-linter). Renovate opens dependency-bump PRs. CI never auto-applies to the cluster — Flux does that from `main`.