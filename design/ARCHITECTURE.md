# Architecture

## Cluster at a Glance

A 3-node Talos Linux cluster running on Proxmox VMs alongside the existing Docker Swarm homelab. All three nodes are control planes that also run workloads. FluxCD drives all reconciliation from the git repository.

```
172.16.20.19  API VIP        (floats via leader election)
172.16.20.20  k8s-cp-1  ─┐
172.16.20.21  k8s-cp-2  ─┼─ etcd (3-node quorum), kube-apiserver, workloads
172.16.20.22  k8s-cp-3  ─┘
172.16.20.50  Gateway VIP   (Cilium L2 announcement → shared Gateway)
```

---

## Node Design

### Talos Linux

Talos is an immutable, minimal OS purpose-built for Kubernetes. No SSH, no shell — all management via `talosctl`. Every node is fully reproducible from `talconfig.yaml` + `talsecret.sops.yaml`. Losing one of three control planes requires no etcd snapshot restore — the rebuilt node rejoins the live cluster automatically.

| Property | Value |
|---|---|
| Talos version | v1.13.2 |
| Kubernetes version | v1.36.1 |
| Node config tool | talhelper |
| Cluster name | `homelab-k8s` |
| Control planes | 3 |
| Workers | None — CPs run workloads (`allowSchedulingOnControlPlanes: true`) |
| API endpoint | VIP `172.16.20.19` |

### Node Spec

| VM | IP | vCPU | RAM | Disk |
|---|---|---|---|---|
| k8s-cp-1 | 172.16.20.20 | 4 | 16 GB | 60 GB |
| k8s-cp-2 | 172.16.20.21 | 4 | 16 GB | 60 GB |
| k8s-cp-3 | 172.16.20.22 | 4 | 16 GB | 60 GB |

### Talos Extensions

Declared in `talconfig.yaml` under each node's `schematic.customization.systemExtensions`. Talhelper registers schematics with `factory.talos.dev` automatically.

| Extension | Purpose |
|---|---|
| `siderolabs/qemu-guest-agent` | Proxmox VM management (clean shutdown, snapshots) |
| `siderolabs/lldpd` | LLDP neighbour discovery for switch port mapping |
| `siderolabs/netbird` | WireGuard mesh VPN — remote access without port forwarding |
| `siderolabs/nut-client` | UPS monitoring (disabled — no UPS yet) |

### etcd Durability

3-node embedded etcd: losing any one CP maintains quorum (2 of 3). The rebuilt CP rejoins automatically — no snapshot needed for single-node loss. An etcd snapshot CronJob is planned (not yet implemented) for the all-three-CPs-lost scenario. See TODO.md.

---

## Networking

See `docs/networking.md` for the full reference. Summary:

- **CNI:** Cilium — VXLAN encapsulation, WireGuard encryption, full kube-proxy replacement
- **Ingress:** Cilium Gateway API — single `shared` Gateway at `172.16.20.50` (L2 announcement)
- **TLS:** cert-manager, wildcard `*.blackcats.cc` via Let's Encrypt DNS-01/Cloudflare
- **DNS:** external-dns, opt-in annotation per HTTPRoute/GRPCRoute
- **Netbird VPN** runs as a Talos extension on every node; three talconfig guards prevent its `100.80.x.x/16` IPs from polluting Kubernetes networking

---

## Storage

See `docs/storage.md` for the full reference. Summary:

- **Default StorageClass:** `nfs-client` (democratic-csi, NFS subdirectory from Synology `tank/kubernetes.nfs`)
- **Local StorageClass:** `openebs-hostpath` (OpenEBS LocalPV at `/var/openebs/local`) — Plex only
- **Shared media:** Static NFS PV/PVC (`media-nfs`) pointing to Synology `/volume2/Media`
- **No iSCSI** — dropped; democratic-csi NFS handles all use cases including CNPG

---

## Databases

One shared CloudNativePG cluster (`postgres` namespace, 2 instances). Custom image `ghcr.io/lucid-void/postgres-cnpg-immich` bundles VectorChord + pgvector for Immich. All databases in the cluster use this image.

Per-database roles managed by CNPG `spec.managed.roles`. DB passwords live in `{app}-role-secret` in the `postgres` namespace, mirrored to app namespaces by Reflector.

---

## Auth

Zitadel is the single identity provider for all services. It runs in the `auth` namespace backed by CNPG Postgres.

- **Native OIDC apps** (Immich, Paperless, FreshRSS, Gitea, Goldilocks, Gatus) connect directly to Zitadel
- **No forward-auth proxy** — every integrated service handles OIDC itself
- Zitadel bootstrap Job provisions OIDC clients via Terraform + Zitadel API on first deploy

---

## GitOps

See `docs/gitops.md` for the full reference. Summary:

- FluxCD with single root Kustomization pointing to `kubernetes/apps/`
- No `infrastructure/` split — operators (Cilium, Sealed Secrets, CNPG, democratic-csi) live in `kubernetes/apps/kube-system/` and `kubernetes/apps/cnpg-system/` etc., ordered by `dependsOn` chains
- Bootstrap (pre-Flux): `kubernetes/bootstrap/helmfile.yml` installs prometheus-operator-crds, Cilium, Spegel, Sealed Secrets
- All subsequent changes go through git → Flux reconciliation

---

## Secrets

See `docs/secrets.md` for the full reference. Summary:

- **k8s app secrets:** Sealed Secrets — encrypted SealedSecret CRDs committed to git
- **Talos secrets:** SOPS + age via talhelper (`talsecret.sops.yaml`)
- Controller in `kube-system`, name `sealed-secrets-controller`, key rotation disabled
- Backup: `sops -e` → Synology `/volume2/backups/keys/sealed-secrets-key.sops.yaml`

---

## Backups

| Job | Schedule | What | Destination |
|---|---|---|---|
| `homebox-backup` | 02:00 | SQLite data PVC | `rclone:filen:backups/restic/homebox` |
| `immich-backup` | 03:00 | Postgres dump + library PVC | `rclone:filen:backups/restic/immich` |
| `postgres-backup` | 03:30 | All k8s DB dumps (CNPG read replica) | `rclone:filen:backups/restic/postgres` |
| `paperless-backup` | 04:00 | Postgres dump + data/media PVCs | `rclone:filen:backups/restic/paperless` |
| `gitea-backup` | 05:00 | Postgres dump + data PVC | `rclone:filen:backups/restic/gitea` |

All jobs: `ghcr.io/lucid-void/backup-tools` image, restic over rclone-filen, 30-day retention. Scale-to-zero before backup where needed (Immich, Paperless, Gitea, Homebox). Gotify notifications on success/failure; failure messages include last 10 log lines.

---

## Key Decisions

| Topic | Decision | Rationale |
|---|---|---|
| OS | Talos Linux | Immutable, API-only, fully reproducible from config; no SSH attack surface |
| Control plane count | 3, all schedulable | etcd quorum survives 1-node loss; no workers simplifies VM management |
| CNI | Cilium | eBPF dataplane, replaces kube-proxy, built-in Gateway API, Hubble observability |
| Routing mode | VXLAN | Deployed and stable; native routing offers no practical benefit at homelab scale |
| Ingress | Cilium Gateway API (not Traefik) | Cilium already required for CNI; single component to manage; Gateway API is the k8s standard |
| TLS | cert-manager + wildcard cert | Single cert covers all services; DNS-01 requires no inbound ports |
| DNS | external-dns with opt-in annotation | Prevents accidental wildcard DNS record creation; explicit per-route control |
| L2 vs BGP | L2 announcements | No BGP router available; L2 sufficient for /24 flat network |
| Storage default | democratic-csi NFS (not iSCSI) | NFS driver simpler (no Synology REST API); handles CNPG; controller-side mount avoids node-level NFS issues |
| Local storage | OpenEBS hostpath | SQLite workloads (Plex) need local disk; simpler than local-path-provisioner; `extraMounts` patch already applied |
| Database | Shared CNPG cluster | Reduces PVC count and operational overhead vs per-app clusters; per-database roles provide isolation |
| CNPG image | Custom (VectorChord + pgvector) | Immich requires pgvector extensions; single image used for all databases in cluster |
| Secrets | Sealed Secrets | GitOps-compatible; encrypted ciphertext safe to commit; no external key management infrastructure |
| Talos secrets | SOPS + age | Reuses existing homelab SOPS infrastructure; no new key management |
| Key rotation | Disabled | Single stable key simplifies backup/restore; re-sealing all secrets on rotation would be significant churn |
| Image builds | GitHub Actions + GHCR (bootstrap-critical images) | Gitea is inside the cluster; circular dependency for images needed to bootstrap the cluster |
| Flux structure | Flat `apps/` with `dependsOn` (not infrastructure/configs/apps split) | Simpler; ordering fully captured by `dependsOn` without separate top-level layers |
| Plex storage | openebs-hostpath | SQLite WAL locking errors on NFS ("retrying busy db"); local disk is the fix |
| Swarm coexistence | Swarm VMs `.10`–`.17` run unchanged | k8s is an addition, not a forced migration; services moved deliberately |
| etcd backup | Planned CronJob (not yet built) | 3-node quorum handles single-node loss without snapshots; full-wipe scenario deferred |
