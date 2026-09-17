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
| [design/decisions/](design/decisions/) | **Per-service decision bodies** — the gotchas behind the pointer rows below. Read the one matching the service you're touching; never read the whole directory. |
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

**Do not record deployed version numbers in this file.** Image tags and chart versions
move on every Renovate PR, and a stale pin here is worse than no pin — it reads as
authoritative and gets copied into manifests. The manifest is the source of truth; check
it with `kubectl`/`grep` when the running version actually matters. Versions belong here
only when they are *durable facts* rather than current state: a constraint (`rclone
v1.69+` for the filen backend, Gateway API `<1.7.0`), a known-bad or minimum version
(homebox `0.25.0`), or a recorded incident (`allauth 65.16` changed the token-auth
inference). Those stay true after the next bump; "we run X.Y.Z" does not.

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
| Compute platform | Talos Linux k8s cluster, FluxCD GitOps. 3 control planes `cp-1/2/3` (`.11`–`.13`, schedulable), API VIP `.10`, Gateway VIP `.50`. No dedicated workers. |
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
| UniFi backup | Not backed up — VLAN/firewall rules reconfigured manually after reset. |

### Auth & identity
| Topic | Decision |
|---|---|

### Service-specific

Pointers, not the decisions themselves. **Read the linked file before editing the named
paths** — each holds gotchas that cost real debugging time and are invisible in the
manifests. Do not re-litigate them without reason.

| Service | Orientation | Read before touching |
|---|---|---|
| Immich | OIDC via Zitadel; embeddings on **VectorChord** in shared CNPG (`DB_VECTOR_EXTENSION` must stay **unset**); custom Postgres image; user migration needs `asset`+`album`+`person`. **pgvector must be built with an explicit `OPTFLAGS`** (Makefile defaults to `-march=native`, which bakes the GitHub runner's ISA into `vector.so`) — image tag **v1.1.1 is permanently broken**, it SIGILLs on our AVX-512-less Arrow Lake CPUs; Renovate is disabled on `imagecatalog.yml`. | `design/decisions/immich.md` — `kubernetes/apps/immich/`, `kubernetes/images/postgres-cnpg-immich/` |
| Plex | `media` ns, `replicas: 1`. Web via HTTPRoute; direct/GDM via pool-b LB pinned to `172.16.20.51` (must not drift to `.52`). CPU-only transcoding. | `design/decisions/plex.md` — `kubernetes/apps/media/plex/` |
| Proxmox OIDC | Proxmox is **bare metal, not a k8s workload**. Zitadel app provisioned by Terraform, secret lands in `auth` ns with no consumer. **Never front Proxmox behind the cluster Gateway** (circular dependency). | `design/decisions/proxmox-oidc.md` — `kubernetes/apps/auth/`, `infra/terraform/` |
| RomM | Game/ROM manager in `media`. External CNPG + embedded Valkey on `emptyDir` (keep off NFS). Runs as root, ignores PUID/PGID. OIDC via optional `envFrom`. | `design/decisions/romm.md` — `kubernetes/apps/media/romm/` |
| Minecraft | Two Paper servers (matcha, vanilla) + **Velocity** proxy on its own pool-b IP `172.16.20.52`. World data on `openebs-hostpath`, **never NFS**. Many traps: proxy IP sharing, memory sizing, quiesced backups, `server.properties` drift, plugin ports, Modrinth loaders. | `design/decisions/minecraft.md` — `kubernetes/apps/media/minecraft*/` |
| Proton Mail Bridge | Makes E2E-encrypted Proton mail readable by Paperless as local IMAP. In the `paperless` ns, IMAP-only ClusterIP, `openebs-hostpath` (gluon = SQLite), not backed up. **Its self-signed cert has one SAN, `IP:127.0.0.1`** — hence the socat sidecar in the Paperless pod; login is interactive and cannot be a Job. Use `ghcr.io/videocurio/…`, **not** `shenxn/…` (publishes stale images despite live commits). | `design/decisions/protonmail-bridge.md` — `kubernetes/apps/paperless/protonmail-bridge/`, `kubernetes/apps/paperless/paperless/` |
| Obsidian LiveSync | CouchDB in its own `obsidian` ns, sync backend for the Obsidian plugin. **Central server, not Syncthing P2P** (deliberate — see file). `openebs-hostpath`, **never NFS**. The one user-facing service **not** behind Zitadel (HTTP Basic; the plugin has no OIDC path). Five traps: a ConfigMap mounted under `/opt/couchdb` kills the entrypoint silently (exit 1, **empty logs**) via its recursive `chown -f` under `set -e` — config must be copied by an initContainer onto an emptyDir; plus `NODENAME`, `single_node`, authenticated `exec` probes, per-platform CORS origins. | `design/decisions/obsidian-livesync.md` — `kubernetes/apps/obsidian/` |
| Joplin | Own `joplin` ns. External CNPG; blobs on a dedicated PVC (**not** `Type=Database`). **SSO is SAML, not OIDC** — SP metadata must stay byte-identical to Terraform, and probes need an explicit `Host` header. | `design/decisions/joplin.md` — `kubernetes/apps/joplin/` |
| LLM stack (`ai` ns) | Qwen3.6-35B-A3B Q8_0 (34.4 GiB) on llama.cpp behind llama-swap, on the dedicated tainted `llm-1`; routed by LiteLLM (`llm.blackcats.cc`), fronted by Open WebUI (`chat.blackcats.cc`). **Text/coding only — no vision** (the model is itself multimodal, so vision is one `--mmproj` flag if ever wanted, not another model). Served as **two LiteLLM names on one process**: `local-smart` and `local-fast` (`enable_thinking: false` via `extra_body`) — thinking cost **572 chars / 152 tokens vs 0 / 2** on the same trivial prompt, and it is *per turn*, so agents get `local-fast`. Decode measured **8.4–8.9 tok/s**. **Four traps, all silent:** `--ctx-size` pre-allocates the entire KV cache and there is **no memory limit** (mmap page-cache accounting), so a bad value takes the *node*; llama-swap group defaults are `swap/exclusive: true`, so without explicit groups an embedding call evicts the 34 GiB chat model; `logToStdout` defaults to `proxy` and **swallows llama-server's log entirely** (invisible in `kubectl logs`, `GET /logs` *and* `/logs/stream/upstream`), which hides model-load failures completely; and `--cache-reuse` is **discarded at startup** for this model (`!llama_memory_can_shift`), so the only prefix caching is llama-server's cross-request prompt cache — which defaults to **8192 MiB and is on unasked**, 8 GiB the memory budget must account for. LiteLLM needs **4Gi** (OOMKilled at 1Gi inside the Prisma migration, exit 137, **zero log output**) and its `/metrics` is **404 on the OSS tier** — the scrape was deleted rather than left red. Open WebUI's `ENABLE_OAUTH_SIGNUP` defaults **false** (login fails with no account to create) and `ENABLE_PERSISTENT_CONFIG` defaults **true** (copies config into the DB, then ignores env). | `design/llm-deployment.md` — `kubernetes/apps/ai/`, `kubernetes/apps/monitoring/ai-monitoring/` |

<!-- Row bodies live in design/decisions/. Adding a service here means adding a POINTER,
     not a body. If a row exceeds ~200 chars, move it out. See .claude/TODO.md. -->


### Kubernetes stack
| Topic | Decision |
|---|---|

## IP map (quick reference)

```
172.16.20.2    Synology RS1219+   — physical, NFS storage only (Btrfs /volume2)
172.16.20.3    Proxmox host       — physical, hypervisor (LVM-thin; hosts the Talos VMs)
               Intel Core Ultra 5 235HX (Arrow Lake-HX, 6P+8E) — verified from /proc/cpuinfo.
               Minisforum **MS-02 Ultra**. NOT an MS-A2 — that model is AMD, and the
               Proxmox hostname `pve-msa2` is where that misidentification came from.
               The CPU identity underpins every number in design/llm-inference.md:
               6 P-cores is why llama.cpp runs --threads 6, and no AVX-512 is why the
               immich pgvector image must be built with an explicit OPTFLAGS.
172.16.20.4    DGX Spark          — physical, GPU box, WOL (not a k8s node)
172.16.20.10   API VIP            — kube-apiserver endpoint (floats via leader election)
172.16.20.11   cp-1               — Talos control plane (schedulable, runs workloads)
172.16.20.12   cp-2               — Talos control plane (schedulable, runs workloads)
172.16.20.13   cp-3               — Talos control plane (schedulable, runs workloads)
172.16.20.14   llm-1              — Talos worker, LLM inference only (tainted workload=llm)
172.16.20.23   VPN VM             — ZeroTier (plain compose, outside cluster)
172.16.20.50   Gateway VIP        — pool-a, shared Gateway ingress (Cilium L2)
172.16.20.51   pool-b             — Plex direct/GDM LoadBalancer
172.16.20.52   pool-b             — minecraft-proxy / Velocity LoadBalancer
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
- **CI/CD** — GitHub Actions: image builds → GHCR (`backup-tools`, `postgres-cnpg-immich`) and two PR gates: `manifest-scan` (kubeconform + kube-linter) and `image-scan` (grype + osv-scanner CVE delta on every changed container image). Renovate opens dependency-bump PRs. CI never auto-applies to the cluster — Flux does that from `main`. See [design/docs/gitops.md](../design/docs/gitops.md) — note `image-scan` must diff whole-file image sets, not diff hunks, because Renovate usually changes only a `tag:` line.