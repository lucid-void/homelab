# Homelab — agent context

**Output shape:** lead with the action or answer; number multi-step work; say where we
are each turn; give time estimates in real units; end on one concrete next step; no
preamble, recap or sign-off. Full rules, only if asked about them:
`.agents/skills/i-have-adhd/SKILL.md`.

Talos Linux Kubernetes cluster on the `blackcats.cc` domain, managed by FluxCD.
Everything is declarative and driven from git: VM templates (Packer), VMs + Cloudflare
DNS (OpenTofu, always applied by hand), node OS (talhelper, SOPS+age),
cluster workloads (Flux, from `main`). A tiny compose remnant survives only for ZeroTier.

## Hard rules

- **Never write a raw `Secret`** — seal it: `mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < /tmp/secret.yaml > <name>-sealed.yml`; the one exception is a Secret whose values are only `${SECRET_*}` placeholders that Flux fills from `cluster-secrets` (the two `cloudflare-secret.yml` files)
- **Never read or print secret material** — `recovery.key`, gitignored `*-secret.yml` plaintext, `kubernetes/flux/vars/cluster-secrets.yml`, `infra/terraform/terraform.tfstate*`, `kubernetes/talos/clusterconfig/`, decrypted SOPS output, `kubectl get secret -o yaml|json`; credentials have leaked into agent transcripts before, and each one then has to be rotated
- **Never use `Ingress`** — use `HTTPRoute`/`GRPCRoute` with `parentRefs: [{name: shared, namespace: gateway}]`
- **Never `kubectl apply` a config change** — it goes through git and Flux; `kubectl` is for diagnostics only; the bootstrap and recovery procedures in `design/runbook.md` are the only sanctioned imperative steps
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

After editing a manifest, run `.agents/scripts/validate-manifests.sh <file>` — a schema
violation that reaches `main` wedges the whole Flux Kustomization, not just that file.
It takes ONE file: loop over files for more than one, a directory checks nothing. A
`SKIPPED:` line means the file was NOT validated.

Repo task runner: `justfile` — Packer, OpenTofu and SOPS helpers only, no Talos or
Kubernetes recipes.

## Network

`172.16.20.0/24`, one flat subnet. Full IP plan and hardware: `design/docs/networking.md`.

```
172.16.20.2        Synology — NFS only (/volume2)
172.16.20.10       API VIP
172.16.20.11-.13   cp-1/2/3 — control planes, schedulable
172.16.20.14       llm-1 — worker, tainted workload=llm
172.16.20.50       Gateway VIP — every HTTPRoute lands here
172.16.20.51-.54   pinned LoadBalancer IPs: Plex, Velocity, Blocky, Blocky log DB — must not drift
```

Storage: media lives on the Synology `media-nfs` RWX PVC, per-app config on `nfs-client`
dynamic PVCs — CNPG database data is on `nfs-client` too (the Synology, via the
democratic-csi share), never on the media share.

## Which file to read

Read only the file for your task. A bare name is under `design/decisions/`. When a file
opens with a **Contents** list, read that, then only the section you need — never a
large file whole (`design/runbook.md` above all).

- adding or changing a service, Flux structure: design/docs/gitops.md
- a pod stuck Running or at `Status: Error`, zero logs, `backoffLimit` never fires, exit 99: jobs-and-scripts.md
- Flux Kustomizations, Flux version pins, `force: true` re-runs: flux.md
- HTTPRoute, DNS, certs, Gateway API: design/docs/networking.md
- Blocky, ad blocking, LAN resolvers, per-device DNS dashboards, node `nameservers`: dns.md
- Cilium config, MTU, ALPN, TCPRoute: cilium-gateway.md
- Postgres, CNPG, DB passwords, Reflector: cnpg.md
- PVCs, storage classes, NFS, Synology shares, `extraMounts`: design/docs/storage.md
- Sealed Secrets, OIDC bootstrap secrets: design/docs/secrets.md
- HelmRelease values, app-template, Reloader: helm-charts.md
- restic, rclone offsite copies, etcd snapshots, backup schedules: backups.md
- VictoriaMetrics, Grafana, VMRule alert rules, Goldilocks, Gatus, coverage-audit, kromgo README badges: monitoring.md
- Minecraft metrics, mc-monitor, world-size alerts: minecraft-monitoring.md
- Gotify, its app tokens, the Telegram alerts module: gotify.md
- Telegram bot (core, modules, commands), flight tracker, AeroDataBox: flight-tracker.md
- Trivy, Falco, kubent, kube-linter, k8s-cleaner: security-tooling.md
- pinning an image tag, lscr tags, Renovate regexes, the image-scan CI gate: images.md
- Keycloak, SSO, OIDC clients, realm, client roles: keycloak.md
- Gitea: gitea.md
- FreshRSS, or Paperless (OIDC and the app itself): oidc-apps.md
- Immich: immich.md
- Plex: plex.md
- Jellyfin the deployment, its Keycloak wiring: jellyfin.md
- Jellyfin plugins, its theme, the SSO plugin fork: jellyfin-ui.md
- YouTube → Jellyfin downloads, ytdl-sub: youtube-queue.md
- watch history sync between Plex and Jellyfin, CrossWatch: watch-sync.md
- sonarr/radarr/prowlarr/sabnzbd/seerr/suwayomi/kavita: media-stack.md
- Tdarr, transcoding, the `.tdarr-trash` directory, shrinking the media library: tdarr.md
- Minecraft servers, Velocity proxy, world data: minecraft.md
- RomM: romm.md
- Homebox: homebox.md
- Obsidian LiveSync: obsidian-livesync.md
- Proton Mail Bridge: protonmail-bridge.md
- changedetection.io, sockpuppetbrowser, its non-SSO login: changedetection.md
- Proxmox OIDC: proxmox-oidc.md
- the LLM stack: llm.md
- service inventory, hostnames, auth model: design/docs/services.md
- UPS, NUT Pi, power-loss shutdown order: ups.md
- topology, nodes, IP plan, Netbird, `talconfig.yaml`: design/architecture.md
- bootstrap, upgrades, restore, recovery, VM provisioning (Packer, OpenTofu): design/runbook.md
- open work, known gaps: design/TODO.md

## Keeping docs in sync

When you change IaC, update the one file this list routes to — design files describe the
**implemented** state, so reality wins over the plan. New gotchas go to
`design/decisions/<topic>.md`, never into this file; only add a line here if the rule
applies repo-wide. Never record a deployed version number: a stale pin reads as
authoritative and gets copied into manifests. Versions belong in a decision file only as
durable constraints (`rclone v1.69+`, Gateway API `<1.7.0`, homebox `0.25.0`).
`docs/superpowers/` holds historical plans and specs — not kept current, never the
implemented state.
