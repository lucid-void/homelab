# Homelab

[![Nodes](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Flucid-void%2F594c7b29869058bfb7fdb33980b38e15%2Fraw%2Fnodes.json&style=flat-square)](https://kromgo.blackcats.cc) [![Pods](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Flucid-void%2F594c7b29869058bfb7fdb33980b38e15%2Fraw%2Fpods.json&style=flat-square)](https://kromgo.blackcats.cc) [![Uptime](https://img.shields.io/endpoint?url=https%3A%2F%2Fgist.githubusercontent.com%2Flucid-void%2F594c7b29869058bfb7fdb33980b38e15%2Fraw%2Fuptime.json&style=flat-square)](https://kromgo.blackcats.cc)

Personal homelab managed as a single Infrastructure-as-Code repository. Primary compute: Talos Kubernetes cluster managed by FluxCD. All services declarative — VM provisioning, OS configuration, cluster bootstrap, and service deployment driven from git.

---

## Core Stack

| Layer | Tool |
|---|---|
| OS | Talos Linux (immutable, API-only) |
| CNI | Cilium — eBPF, WireGuard pod-to-pod encryption, Hubble, Gateway API |
| Ingress | Cilium Gateway API (`HTTPRoute`/`GRPCRoute`; no `Ingress` objects) |
| CSI | democratic-csi → Synology NFS (RWX, default) + static NFS PVs |
| Local storage | OpenEBS hostpath (SQLite-hostile workloads) |
| Databases | CloudNativePG — one shared cluster, per-app databases |
| Secrets | Sealed Secrets in-cluster; SOPS + age for Talos secrets |
| GitOps | FluxCD |
| TLS | cert-manager + Let's Encrypt DNS-01 (Cloudflare) |
| Identity | Keycloak — the OIDC provider for every service on SSO |
| Runtime security | Falco + Trivy Operator + kubent (weekly) |
| Backups | Per-app CronJobs → restic → rclone → Filen (offsite) |
| DNS | UDM SE (local override for `*.blackcats.cc`) + external-dns to Cloudflare |
| Remote access | Netbird (primary VPN on every node); ZeroTier (gaming, outside cluster) |
| VM provisioning | Packer (Debian + Talos templates) + OpenTofu |
| CI / CD | GitHub Actions (image builds → GHCR, manifest + security scans) + Renovate |

**No port forwarding on WAN.** All external hostnames resolve to internal IPs; access requires LAN, Netbird, or ZeroTier.

---

## User Services

### Photos / ML
- **Immich** — mobile sync + ML, `immich.blackcats.cc`, Keycloak OIDC

### Documents
- **Paperless-ngx** — document management, `paperless.blackcats.cc`, Keycloak OIDC

### Media
- **Plex** — `plex.blackcats.cc`, config on `openebs-hostpath`
- **Jellyfin** — `jellyfin.blackcats.cc`, Keycloak OIDC, config on `openebs-hostpath` (cp-2, deliberately not Plex's node)
- **Sonarr, Radarr, Prowlarr, SABnzbd, Seerr** — `bjw-s/app-template`, `media-nfs` RWX PVC
- **Minecraft** — `matcha` (plugins) & `vanilla` (plugin-free) via Velocity proxy, `172.16.20.52:25565`, `openebs-hostpath` worlds
- **Suwayomi** — manga downloader, `suwayomi.blackcats.cc`
- **Kavita** — ebook reader, `kavita.blackcats.cc`

### Reading
- **FreshRSS** — `rss.blackcats.cc`, Keycloak OIDC

### Code / Git
- **Gitea** — `gitea.blackcats.cc`, mirrored from GitHub, Keycloak OIDC + SSH

### Identity
- **Keycloak** — `sso.blackcats.cc`, the OIDC provider for every service on SSO

### Dashboard
- **Homepage** — `home.blackcats.cc`, no auth

### Observability
- **Gatus** — `gatus.blackcats.cc`, health checks + cert expiry
- **kromgo** — `kromgo.blackcats.cc`, PromQL → shields.io badges (pushed to a gist for the badges above)
- **Goldilocks** — `goldilocks.blackcats.cc`, VPA dashboard
- **Gotify** — `gotify.blackcats.cc`, notification hub
- **Falco** — runtime security, `security` namespace (privileged PSA)
- **Trivy Operator** — CVE + config audit scanning

### De-Google
- **freshrss, immich, gitea, paperless** — the `degoog` stack

### AI / LLM
- **llama-swap** — Qwen3.6-35B-A3B Q8_0 on dedicated tainted `llm-1`, ClusterIP `llama-swap:8080`
- **LiteLLM** — `llm.blackcats.cc`, router with virtual keys (`local-smart`, `local-fast`)
- **Open WebUI** — `chat.blackcats.cc`, Keycloak OIDC

### VPN
- **Netbird** — primary VPN, runs as Talos extension on every node (`wt0` interface)
- **ZeroTier** — gaming only, separate VM outside cluster

---

## Repository Layout

```
Homelab/
├── kubernetes/              # cluster — manifests, bootstrap, Talos config
│   ├── apps/                # one dir per namespace; one Flux Kustomization per app
│   ├── bootstrap/           # pre-Flux bootstrap (helmfile) + Flux entry
│   ├── flux/                # Flux root: vars, repositories, root Kustomization
│   ├── talos/               # talconfig.yaml + SOPS-encrypted cluster secrets
│   └── images/              # custom container images (built in CI, pushed to GHCR)
├── infra/
│   ├── packer/              # Debian + Talos VM templates
│   └── terraform/           # VM + DNS provisioning (OpenTofu)
├── .github/workflows/       # CI: image builds, manifest + security scans
├── design/                  # design specs, runbook, decisions (operator-only)
├── INSTALLATION.md          # cluster bootstrap procedure (Phase 1 → live cluster)
├── justfile                 # task runner
└── README.md                # this file
```

---

## Where to Find Things

- **Bootstrap a fresh cluster** → [INSTALLATION.md](INSTALLATION.md)
- **Architecture, decisions, runbook** → [design/](design/)
- **Per-application detail** → [design/docs/services.md](design/docs/services.md)
- **Adding a new service end-to-end** → [design/docs/gitops.md](design/docs/gitops.md)
- **Secrets model** → [design/docs/secrets.md](design/docs/secrets.md)
- **Networking** → [design/docs/networking.md](design/docs/networking.md)
- **Storage** → [design/docs/storage.md](design/docs/storage.md)
- **Known gaps and planned work** → [design/TODO.md](design/TODO.md)

---

## Prerequisites

Kubernetes tooling (`kubectl`, `flux`, `kubeseal`, `talosctl`, `talhelper`, `helm`,
`helmfile`, `kubeconform`) is managed via [mise](https://mise.jdx.dev/). After cloning:

```bash
mise install
```

For the IaC side, install `packer`, `opentofu`, `sops`, `age`, and `just`
through your package manager.

---

## CI / CD

Pipelines run on GitHub Actions. Renovate opens dependency-bump PRs for Helm charts and
image tags.

| Workflow | Trigger | Job |
|---|---|---|
| `manifest-scan.yml` | PR touching `kubernetes/**` | kubeconform (hard gate) + kube-linter (delta gate) |
| `backup-tools.yml` | push to `kubernetes/images/backup-tools/**` | build + push image → GHCR |
| `postgres-cnpg-immich.yml` | push to `kubernetes/images/postgres-cnpg-immich/**` | build + push image → GHCR |

**Nothing is auto-applied to the cluster.** Flux reconciles cluster state from `main`;
`tofu apply` is always run manually after reviewing the plan.