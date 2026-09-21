# Architecture

Canonical topology and design-decision reference for the `homelab-k8s` Kubernetes
cluster. Deep dives live in `design/docs/`; per-service gotchas live in
`design/decisions/`.

---

## Cluster Topology

| Node | IP | Role | Spec |
|---|---|---|---|
| cp-1 | 172.16.20.11 | Control plane + workloads | 8 vCPU, 30 GB RAM, 100 GB disk |
| cp-2 | 172.16.20.12 | Control plane + workloads | 8 vCPU, 30 GB RAM, 100 GB disk |
| cp-3 | 172.16.20.13 | Control plane + workloads | 8 vCPU, 30 GB RAM, 100 GB disk |
| llm-1 | 172.16.20.14 | Worker — LLM inference only | 8 vCPU, 64 GB RAM, 250 GB disk |
| API VIP | 172.16.20.10 | Kubernetes API server endpoint | Floats via leader election |
| Gateway VIP | 172.16.20.50 | Ingress for all HTTP/HTTPS | Cilium L2 announcement |

**Cluster name:** `homelab-k8s`. All three control planes run user workloads
(`allowSchedulingOnControlPlanes: true`) — no dedicated workers. Losing one node keeps
etcd quorum (2 of 3); the rebuilt node rejoins automatically, no snapshot needed for a
single-node loss.

`llm-1` is the one worker: a non-etcd node dedicated to LLM inference, tainted
`workload=llm:NoSchedule` and labelled `workload=llm`. It exists because a large
mmap'd model on an etcd member evicts etcd's page cache, and etcd is
fsync-latency-sensitive, so the memory pressure causes leader-election churn — see
`design/decisions/llm.md`.

**The control-plane RAM figure above is the committed IaC, not what's provisioned.**
`llm-1` is defined in `infra/terraform/kubernetes.tf` and
`kubernetes/talos/talconfig.yaml` but not yet provisioned (no `tofu apply`, no
`talhelper apply`) — the control planes still run at 32 GB; dropping them to 30 GB to
fund `llm-1` is pending and needs a rolling reboot, one node at a time. Total commit is
154 GB, 4 GB over the 150 GB budget the design doc assumes — verify installed host RAM
before applying. Steady-state control-plane usage is ~9–13 GiB, so 30 GB is comfortable
day to day; the pressure point is draining a node during a Talos upgrade, where cp-3's
35.1 GiB limit sum leaves little slack.

### Talos Extensions

Declared in `talconfig.yaml` under each node's
`schematic.customization.systemExtensions`. Talhelper registers schematics with
`factory.talos.dev` automatically.

| Extension | Purpose |
|---|---|
| `siderolabs/qemu-guest-agent` | Proxmox VM management (clean shutdown, snapshots) |
| `siderolabs/lldpd` | LLDP neighbour discovery for switch port mapping |
| `siderolabs/netbird` | WireGuard mesh VPN — remote access without port forwarding |
| `siderolabs/nut-client` | UPS monitoring (disabled — no UPS yet) |

---

## Key decisions

### Platform & networking

| Topic | Decision |
|---|---|
| Compute platform | Talos Linux k8s cluster, FluxCD GitOps. 3 control planes `cp-1/2/3` (`.11`–`.13`, schedulable), API VIP `.10`, Gateway VIP `.50`. No dedicated workers. |
| Internet exposure | Cloudflare DNS used only for valid TLS certs (DNS-01); all A records → internal IPs; no port forwarding on UDM SE; remote access requires Netbird VPN. No Cloudflare proxy. |
| Netbird / ZeroTier | Netbird = primary remote-access VPN, runs as a Talos extension on every node (`wt0`, isolated from k8s networking). ZeroTier = gaming with friends only, on a separate VM outside the cluster (plain compose). |
| Cloudflare API tokens | One token per consumer (external-dns, cert-manager, Proxmox), Zone→DNS→Edit on blackcats.cc only; isolated for independent revocation. |
| NFS / Postgres traffic | Cleartext on internal VLAN — accepted risk; private network, VPN-gated. |

### Storage, secrets, backups

| Topic | Decision |
|---|---|
| Tofu state | Stored in PostgreSQL on the Synology (`tofu_state` database); if lost, run `tofu apply` fresh. |
| UniFi backup | Not backed up — VLAN/firewall rules reconfigured manually after reset. |

### Design rationale

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
| Local storage | OpenEBS hostpath | SQLite/latency-sensitive workloads need local disk; simpler than local-path-provisioner; `extraMounts` patch already applied |
| Database | Shared CNPG cluster | Reduces PVC count and operational overhead vs per-app clusters; per-database roles provide isolation |
| CNPG image | Custom (pgvector + VectorChord) | Immich vector search runs on VectorChord (pgvector also bundled); single image used for all databases in cluster |
| Secrets | Sealed Secrets | GitOps-compatible; encrypted ciphertext safe to commit; no external key management infrastructure |
| Talos secrets | SOPS + age | Reuses existing homelab SOPS infrastructure; no new key management |
| Key rotation | Disabled | Single stable key simplifies backup/restore; re-sealing all secrets on rotation would be significant churn |
| Image builds | GitHub Actions + GHCR (bootstrap-critical images) | Gitea is inside the cluster; circular dependency for images needed to bootstrap the cluster |
| Flux structure | Flat `apps/` with `dependsOn` (not infrastructure/configs/apps split) | Simpler; ordering fully captured by `dependsOn` without separate top-level layers |
| Swarm coexistence | Swarm VMs `.10`–`.17` run unchanged | k8s is an addition, not a forced migration; services moved deliberately |

---

## Networking

See `design/docs/networking.md` for the full reference. Summary:

- **CNI:** Cilium — VXLAN encapsulation, WireGuard node-to-node encryption, full
  kube-proxy replacement
- **Ingress:** Cilium Gateway API — single `shared` Gateway at `172.16.20.50` (L2
  announcement)
- **TLS:** cert-manager, wildcard `*.blackcats.cc` via Let's Encrypt DNS-01/Cloudflare
- **DNS:** external-dns, opt-in annotation per HTTPRoute/GRPCRoute
- **Netbird VPN** runs as a Talos extension on every node, adding a `wt0` interface at
  `100.80.x.x/16`. Three guards in `talconfig.yaml` stop those IPs polluting Kubernetes
  networking: etcd `advertisedSubnets`/`listenSubnets`, kubelet
  `nodeIP.validSubnets`, and a per-node kube-apiserver `advertise-address` — all pinned
  to `172.16.20.0/24`.

---

## Storage

See `design/docs/storage.md` for the full reference. Summary:

- **Default StorageClass:** `nfs-client` (democratic-csi, NFS subdirectory from
  Synology `tank/kubernetes.nfs`)
- **Local StorageClass:** `openebs-hostpath` (OpenEBS LocalPV at
  `/var/openebs/local`) — for workloads NFS is bad for: SQLite locking, latency-sensitive
  random I/O, fsync-per-write with long-lived file locks
- **Shared media:** Static NFS PV/PVC (`media-nfs`) pointing to Synology
  `/volume2/Media`
- **No iSCSI** — dropped; democratic-csi NFS handles all use cases including CNPG

---

## Databases

One shared CloudNativePG cluster (`postgres` namespace, 2 instances: primary + read
replica). Custom image `ghcr.io/lucid-void/postgres-cnpg-immich` bundles pgvector +
VectorChord; the cluster loads `shared_preload_libraries: [vchord.so]`. All databases
in the cluster use this image.

---

## Auth

Keycloak is the identity provider for all services. It runs in the `keycloak`
namespace backed by CNPG Postgres, deployed by the official Keycloak Operator.

- Native OIDC apps connect directly to Keycloak — no forward-auth proxy
- OIDC clients are `KeycloakOIDCClient` CRs, each with a sealed client secret that git
  owns rather than one the IdP issues
- Zitadel still runs in the `auth` namespace but serves no application: its only
  consumer, Joplin over SAML, was deleted on 2026-09-21. Removal is pending

---

## GitOps

See `design/docs/gitops.md` for the full reference. Summary:

- **Git source:** `github.com/lucid-void/Homelab`, branch `main`, path `kubernetes/`
- FluxCD with single root Kustomization pointing to `kubernetes/apps/`
- No `infrastructure/` split — operators (Cilium, Sealed Secrets, CNPG,
  democratic-csi) live in `kubernetes/apps/kube-system/` and
  `kubernetes/apps/cnpg-system/` etc., ordered by `dependsOn` chains
- Bootstrap (pre-Flux): `kubernetes/bootstrap/helmfile.yml` installs
  prometheus-operator-crds, Cilium, Spegel, Sealed Secrets
- All subsequent changes go through git → Flux reconciliation

---

## Secrets

See `design/docs/secrets.md` for the full reference. Summary:

- **k8s app secrets:** Sealed Secrets — encrypted SealedSecret CRDs committed to git
- **Talos secrets:** SOPS + age via talhelper (`talsecret.sops.yaml`)
- Controller in `kube-system`, name `sealed-secrets-controller`, key rotation disabled
- Backup: `sops -e` → Synology `/volume2/backups/keys/sealed-secrets-key.sops.yaml`
