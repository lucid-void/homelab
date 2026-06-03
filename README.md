# Homelab

Personal homelab managed as a single Infrastructure-as-Code repository. The primary
compute platform is a Talos Kubernetes cluster; a small Docker Swarm side runs the
handful of workloads that still need host networking or GPU passthrough.

Everything is declarative — VM provisioning, OS configuration, cluster bootstrap, and
service deployment are all driven from git.

---

## What runs here

Self-hosted services for daily use, all behind a single OIDC provider:

| Area | Services |
|---|---|
| Photos | Immich (mobile sync + ML) |
| Documents | Paperless-ngx |
| Media | Plex, Sonarr, Radarr, Prowlarr, SABnzbd, Seerr |
| Reading | FreshRSS |
| Code / Git | Gitea (self-hosted, mirrored from GitHub) |
| Inventory | Homebox |
| Identity | Zitadel (single OIDC provider for everything above) |
| Dashboard | Homepage |
| Observability | Gatus, Goldilocks, Gotify, Falco, Trivy Operator, kubent |
| De-Google | freshrss, immich, gitea, paperless (the `degoog` stack) |

Netbird (primary VPN) runs as a Talos extension on every cluster node. ZeroTier (gaming)
is the only workload left outside the cluster, on a small VM via plain Docker Compose.

---

## Stack

| Layer | Tool |
|---|---|
| OS | Talos Linux (immutable) |
| CNI | Cilium — eBPF, WireGuard pod-to-pod encryption, Hubble |
| Ingress | Cilium **Gateway API** (`HTTPRoute` / `GRPCRoute` — no `Ingress` objects) |
| CSI | democratic-csi → Synology NFS (RWX, default) + static NFS PVs |
| Local storage | OpenEBS hostpath for SQLite-hostile workloads |
| Databases | CloudNativePG — one shared cluster, per-app databases |
| Secrets | Sealed Secrets in-cluster; SOPS + age for Talos cluster secrets |
| GitOps | FluxCD |
| TLS | cert-manager + Let's Encrypt DNS-01 (Cloudflare) |
| Identity | Zitadel (OIDC) |
| Runtime security | Falco + Trivy Operator + kubent (weekly) |
| Backups | Per-app CronJobs → restic → rclone → Filen (offsite) |
| DNS | UDM SE (local override for `*.blackcats.cc`) + external-dns to Cloudflare |
| Remote access | Netbird (primary VPN), ZeroTier (gaming) |
| VM provisioning | Packer (Debian + Talos templates) + OpenTofu |
| CI / CD | GitHub Actions (image builds → GHCR, manifest + security scans) + Renovate |

No port forwarding on the WAN. All external hostnames resolve to internal IPs;
access requires LAN, Netbird, or ZeroTier.

---

## Repository layout

```
Homelab/
├── kubernetes/              # the cluster — manifests, bootstrap, Talos config
│   ├── apps/                # one directory per namespace; one Flux Kustomization per app
│   ├── bootstrap/           # one-time pre-Flux bootstrap (helmfile) + Flux entry
│   ├── flux/                # Flux root: vars, repositories, root Kustomization
│   ├── talos/               # talconfig.yaml + SOPS-encrypted cluster secrets
│   └── images/              # custom container images (built in CI, pushed to GHCR)
├── infra/
│   ├── packer/              # Debian + Talos VM templates
│   └── terraform/           # VM + DNS + Zitadel OIDC provisioning (OpenTofu)
├── .github/workflows/       # CI: image builds, manifest + security scans
├── design/                  # design specs, runbook, decisions (operator-only)
├── INSTALLATION.md          # cluster bootstrap procedure (Phase 1 → live cluster)
├── justfile                 # task runner
└── README.md                # this file
```

---

## Where to find things

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
