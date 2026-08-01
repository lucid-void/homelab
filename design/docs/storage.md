# Storage

## Storage Classes

| StorageClass | Provisioner | Access Modes | Default | Use case |
|---|---|---|---|---|
| `nfs-client` | democratic-csi (NFS subdirectory) | RWO / RWX | **Yes** | All app data, CNPG Postgres instances |
| `openebs-hostpath` | OpenEBS LocalPV | RWO | No | Workloads NFS is bad for: SQLite locking (Plex), latency-sensitive random I/O (Minecraft worlds) |

---

## democratic-csi (`nfs-client`)

**Provisioner:** `org.democratic-csi.nfs-client`

The controller mounts the Synology parent NFS share (`172.16.20.2:/volume2/kubernetes.nfs`) at startup via a `postStart` lifecycle hook. For each PVC, it creates a subdirectory under that share. No Synology REST API is needed.

**Controller requirements:** `hostNetwork: true`, `hostIPC: true`, `SYS_ADMIN`, `privileged: true`.
The `democratic-csi` namespace has `pod-security.kubernetes.io/enforce: privileged` to permit this.

Dynamic PVCs mount **inside privileged containers** — they are not subject to the Talos kubelet mount namespace restriction (see below). NFS v4 vs v4.1 is irrelevant for dynamic PVCs.

### Usage

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: myapp-data
  namespace: myapp
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: nfs-client
  resources:
    requests:
      storage: 5Gi
```

Manifests: `kubernetes/apps/democratic-csi/`

---

## Static NFS PVC (`media-nfs`)

The Synology `Media` share (`172.16.20.2:/volume2/Media`) is exposed as a **static** PV/PVC pair named `media-nfs` in the `media` namespace. This is not provisioned by democratic-csi — it is a manual binding to a pre-existing NFS export.

**Critical:** Talos kernel only supports **NFSv4** (not NFSv4.1) for host-level static NFS mounts. Always set `nfsvers=4` in PV `mountOptions`. Using `nfsvers=4.1` fails with "Protocol not supported" at the kernel level.

```yaml
# PV
apiVersion: v1
kind: PersistentVolume
metadata:
  name: media-nfs
spec:
  capacity:
    storage: 10Ti
  accessModes: [ReadWriteMany]
  nfs:
    server: 172.16.20.2
    path: /volume2/Media
  mountOptions:
    - nfsvers=4   # NOT nfsvers=4.1 — Talos kernel limitation
  persistentVolumeReclaimPolicy: Retain
```

This restriction applies **only** to host-level (static PV) mounts. Democratic-csi dynamic PVCs mount inside privileged containers and are unaffected.

Manifest: `kubernetes/apps/media/media-nfs/`

---

## OpenEBS LocalPV (`openebs-hostpath`)

**Provisioner:** `openebs.io/local`
**Base path:** `/var/openebs/local`
**Node affinity:** Set automatically when the PVC first binds — the PV is pinned to that node permanently.

### Capacity is not enforced

An `openebs-hostpath` PVC is a **plain directory on the node's `EPHEMERAL` partition**, not a quota-backed volume. The `storage:` request in the PVC spec is advisory — a claim that says `20Gi` will happily grow past that, and will keep going until the *node* runs out of disk. Consequences worth remembering:

- **`kubelet_volume_stats_*` for these PVCs report the node filesystem**, not the directory. A "volume 85% full" alert on a hostpath PVC means *the node* is 85% full — which may have nothing to do with the workload named in the alert.
- **They cannot be expanded in place**, because there is nothing to expand; the only lever is growing the node disk (see RUNBOOK → *Grow the node disks*).
- Everything on the node competes for the same space: container images, etcd, logs, and every other hostpath PVC.

Nodes are 100 GB (`EPHEMERAL` ≈ 105 GB after Talos claims the other partitions). Current consumers:

| Node | `/var/lib/containerd` | `/var/openebs` |
|---|---|---|
| cp-1 | ~14 GB | ~15 GB (Plex config) |
| cp-2 | ~30 GB | ~1.4 GB → ~2.5 GB (matcha world, pregenerated) |
| cp-3 | ~28 GB | ~1.4 GB → ~2.5 GB (vanilla world, pregenerated) |

Container images dominate, not application data. See *Image garbage collection* below.

### Image garbage collection

kubelet's defaults (`imageGCHighThresholdPercent: 85`, low 80) never fired on these nodes — they sat at 72–76% full while `/var/lib/containerd` accumulated ~30 GB of unused images from Renovate bumps, Trivy scan jobs and rolled-back tags. Reclaiming only at 85% also means the first cleanup coincides with disk pressure and pod eviction.

`talconfig.yaml` now sets, under `machine.kubelet.extraConfig`:

```yaml
imageGCHighThresholdPercent: 70
imageGCLowThresholdPercent: 55
```

This is a **ceiling, not a cleanup** — it prunes only once a node crosses 70%. Applying it does not reclaim anything immediately. Verify with:

```bash
kubectl get --raw "/api/v1/nodes/<node>/proxy/configz" | jq .kubeletconfig.imageGCHighThresholdPercent
```

Two consumers, for two different reasons:

- **Plex config** — SQLite WAL locking errors occur over NFS.
- **Minecraft worlds** (`matcha-data`, `vanilla-data`) — Anvil region files are random small I/O inside large files. Every chunk load over NFS costs a network round-trip, and a load that blocks the single-threaded tick loop shows up as TPS drop and rubber-banding. The Synology is also HDD-backed and simultaneously serving the media stack, so seek contention with a Plex scan or a SAB download would land directly on gameplay. A third hazard: NFS mounts must be `hard` (a `soft` mount risks silent corruption), so a NAS stall blocks the JVM in uninterruptible I/O rather than degrading it.

Node-pinning is the price, and it is cheap here: all three control planes are VMs on the same Proxmox host, so a node failure worth rescheduling around is either a VM failure (data intact on the host, just wait for the node) or a host failure (everything is down regardless). See `design/docs/services.md` → Media for how the world data is still backed up off-node.

### Talos Mount Namespace Constraint

The Talos kubelet runs in a **private mount namespace**. Pod-created `hostPath` directories are invisible to the kubelet unless explicitly shared. For the OpenEBS base path to work, all nodes must have this patch applied via `talconfig.yaml`:

```yaml
machine:
  kubelet:
    extraMounts:
      - destination: /var/openebs/local
        type: bind
        source: /var/openebs/local
        options: [bind, rshared, rw]
```

This is already in `controlPlane.patches` in `talconfig.yaml`. Apply with `talosctl apply-config` — no reboot required.

Manifests: `kubernetes/apps/openebs/`

---

## Storage Taxonomy

| Category | StorageClass | Examples |
|---|---|---|
| App persistent data (RWO) | `nfs-client` | CNPG instances, Immich library, Paperless docs, Gitea repos, Gotify SQLite, Gatus |
| Shared media (RWX) | Static NFS PV | `media-nfs` — Synology `/volume2/Media` shared by all media services |
| Local persistent (RWO, NFS-hostile I/O) | `openebs-hostpath` | Plex config, Minecraft worlds |
| Ephemeral | `emptyDir` | Valkey caches, Tika, Gotenberg |
| Config/secrets | ConfigMap + SealedSecret | All app configuration |

---

## CNPG Postgres Storage

CloudNativePG provisions one `nfs-client` PVC per cluster instance. The shared `postgres` cluster has 2 instances = 2 PVCs.

The custom Postgres image (`ghcr.io/lucid-void/postgres-cnpg-immich`) bundles pgvector + VectorChord — Immich's vector search runs on VectorChord (`vchord`). All databases in the cluster use this image.

**No CNPG-native backup is configured** — there is no `ScheduledBackup`, no `Backup`, and no `barmanObjectStore`, so there is no WAL archiving and no point-in-time recovery. The only database backup is the `postgres-backup` CronJob (03:30), which takes logical `pg_dump`s from the read replica and ships them to Filen via restic. See TODO.md.

A side benefit: CloudNativePG 1.31 removes in-tree Barman Cloud support, and this cluster is unaffected because it never used it.

---

## Synology Share Layout

| Synology path | Kubernetes use |
|---|---|
| `/volume2/kubernetes.nfs/` | democratic-csi parent share — one subdirectory per PVC |
| `/volume2/Media/` | Static `media-nfs` PVC for all media services |
| `/volume2/backups/keys/` | Sealed Secrets key backup, SOPS age key backup |
