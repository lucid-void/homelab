# Homelab — agent context

**Output shape:** follow `.agents/skills/i-have-adhd/SKILL.md` for every response in this repo.

Talos Linux Kubernetes cluster on the `blackcats.cc` domain, managed by FluxCD.
Everything is declarative and driven from git: VM templates (Packer), VMs + Cloudflare
DNS (OpenTofu, always applied by hand), node OS (talhelper, SOPS+age),
cluster workloads (Flux, from `main`). A tiny compose remnant survives only for ZeroTier.

## Hard rules

- **Never write a raw `Secret`** — seal it: `mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < /tmp/secret.yaml > <name>-sealed.yml`
- **Never use `Ingress`** — use `HTTPRoute`/`GRPCRoute` with `parentRefs: [{name: shared, namespace: gateway}]`
- **Never `kubectl apply` a config change** — it goes through git and Flux; `kubectl` is for diagnostics only
- **Never set `spec.targetNamespace`** on a Kustomization whose resources span namespaces — it overrides every explicit namespace field
- **Never use `nfsvers=4.1`** in a static PV — the Talos kernel supports NFSv4 only
- **Never use a rolling image tag** (`latest`, `3`) — and linuxserver.io images need the FULL tag, their short `X.Y.Z` is mutable
- **Never put Python at 0-indent** inside a YAML `|` block scalar — it breaks the kustomize parser
- **Never `apk add` without `timeout 300`** in a Job or initContainer — an unbounded stall wedges the pod Running with empty logs forever
- **Never run an `apk add` Job as non-root** — set `runAsUser: 0` AND `runAsNonRoot: false`; `runAsUser: 0` alone is rejected by the kubelet, and non-root fails at exit 99 with empty logs
- **Never install rclone from Alpine apk** where the filen backend is needed — it needs v1.69+, from `downloads.rclone.org`
- **Never trust `optional: true` on an app-template `envFrom`** — chart 3.7.3 strips it silently
- **Never assume a Helm values path took effect** — a wrong path is a silent no-op; render or check the live object
- **Never size a workload from Goldilocks/VPA** — there is no metrics-server, so its numbers are fabricated floors

## Tools

All k8s tooling is managed by mise and is NOT on `PATH`:
`mise exec -- kubectl|flux|kubeseal|talosctl|talhelper|helm|kubeconform`

After editing a manifest, run `.agents/scripts/validate-manifests.sh <path>` — a schema
violation that reaches `main` wedges the whole Flux Kustomization, not just that file.

Repo task runner: `justfile`.

## Network

```
172.16.20.2    Synology RS1219+     NFS only (/volume2)
172.16.20.3    Proxmox host         hypervisor (LVM-thin), hosts the Talos VMs
172.16.20.4    DGX Spark            GPU box, WOL, not a k8s node
172.16.20.10   API VIP              kube-apiserver endpoint
172.16.20.11/.12/.13  cp-1/2/3      Talos control plane, schedulable
172.16.20.14   llm-1                Talos worker, tainted workload=llm
172.16.20.23   ZeroTier VM          plain compose, outside the cluster
172.16.20.50   Gateway VIP          pool-a, shared Gateway ingress (Cilium L2)
172.16.20.51   pool-b               Plex direct/GDM LB — must not drift to .52
172.16.20.52   pool-b               minecraft-proxy / Velocity LB
172.16.20.254  UDM SE               gateway + DNS resolver + ad blocking
```

Netbird (`wt0`, `100.80.x.x/16`) runs as a Talos extension on every node, not on a VM.

Storage: media lives on the Synology `media-nfs` RWX PVC, per-app config on `nfs-client`
dynamic PVCs — but CNPG database data lives on cluster storage, never on the media share.

Proxmox CPU: Intel Core Ultra 5 235HX (Arrow Lake-HX, 6P+8E), a Minisforum MS-02 Ultra
— NOT an MS-A2, despite the `pve-msa2` hostname. 6 P-cores is why llama.cpp runs
`--threads 6`; no AVX-512 is why the immich pgvector image needs an explicit `OPTFLAGS`.

## Which file to read

| Your task touches...                                 | Read (only this)                      |
|------------------------------------------------------|---------------------------------------|
| adding or changing a service, Flux structure          | design/docs/gitops.md                 |
| a pod stuck Running or at `Status: Error`, zero logs, `backoffLimit` never fires, exit 99 | design/decisions/jobs-and-scripts.md |
| Flux Kustomizations, Flux version pins, `force: true` re-runs | design/decisions/flux.md        |
| HTTPRoute, DNS, certs, Gateway API                    | design/docs/networking.md             |
| Cilium config, MTU, ALPN, TCPRoute                    | design/decisions/cilium-gateway.md    |
| Postgres, CNPG, DB passwords, Reflector               | design/decisions/cnpg.md              |
| PVCs, storage classes, NFS, Synology shares, `extraMounts` | design/docs/storage.md           |
| Sealed Secrets, OIDC bootstrap secrets                | design/docs/secrets.md                |
| HelmRelease values, app-template, Reloader            | design/decisions/helm-charts.md       |
| restic, rclone offsite copies, etcd snapshots, backup schedules | design/decisions/backups.md |
| VictoriaMetrics, Grafana, VMRule alert rules, Goldilocks, Gatus, kromgo README badges | design/decisions/monitoring.md |
| Minecraft metrics, mc-monitor, world-size alerts      | design/decisions/minecraft-monitoring.md |
| Gotify, its app tokens, the Telegram bridge           | design/decisions/gotify.md            |
| Trivy, Falco, kubent, kube-linter, k8s-cleaner        | design/decisions/security-tooling.md  |
| pinning an image tag, lscr tags, Renovate regexes     | design/decisions/images.md            |
| Keycloak, SSO, OIDC clients, realm, client roles      | design/decisions/keycloak.md          |
| Gitea                                                 | design/decisions/gitea.md             |
| FreshRSS, or Paperless (OIDC and the app itself)       | design/decisions/oidc-apps.md         |
| Immich                                                | design/decisions/immich.md            |
| Plex                                                  | design/decisions/plex.md              |
| Jellyfin the deployment, its Keycloak wiring          | design/decisions/jellyfin.md          |
| Jellyfin plugins, its theme, the SSO plugin fork      | design/decisions/jellyfin-ui.md       |
| watch history sync, CrossWatch, the Trakt→Seerr queue | design/decisions/watch-sync.md        |
| sonarr/radarr/prowlarr/sabnzbd/seerr/suwayomi/kavita  | design/decisions/media-stack.md       |
| Minecraft servers, Velocity proxy, world data         | design/decisions/minecraft.md         |
| RomM                                                  | design/decisions/romm.md              |
| Homebox                                               | design/decisions/homebox.md           |
| Obsidian LiveSync                                     | design/decisions/obsidian-livesync.md |
| Proton Mail Bridge                                    | design/decisions/protonmail-bridge.md |
| changedetection.io, sockpuppetbrowser, its non-SSO login | design/decisions/changedetection.md |
| Proxmox OIDC                                          | design/decisions/proxmox-oidc.md      |
| the LLM stack                                         | design/decisions/llm.md               |
| service inventory, hostnames, auth model              | design/docs/services.md               |
| topology, nodes, IP plan, Netbird, `talconfig.yaml`    | design/architecture.md                |
| bootstrap, upgrades, restore, recovery, VM provisioning (Packer, OpenTofu) | design/runbook.md — 46 KB, jump to the one `##` section, never read it whole |
| open work, known gaps                                 | design/TODO.md                        |

## Keeping docs in sync

When you change IaC, update the one file this table routes to — design files describe the
**implemented** state, so reality wins over the plan. New gotchas go to
`design/decisions/<topic>.md`, never into this file; only add a row here if the rule
applies repo-wide. Never record a deployed version number: a stale pin reads as
authoritative and gets copied into manifests. Versions belong in a decision file only as
durable constraints (`rclone v1.69+`, Gateway API `<1.7.0`, homebox `0.25.0`).
