# Storage

## Storage Classes

| StorageClass | Provisioner | Access Modes | Default | Use case |
|---|---|---|---|---|
| `nfs-client` | democratic-csi (NFS subdirectory) | RWO / RWX | **Yes** | All app data, CNPG Postgres instances |
| `openebs-hostpath` | OpenEBS LocalPV | RWO | No | Workloads where SQLite-over-NFS causes locking (Plex) |

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

Used only for Plex config, where SQLite WAL locking errors occur over NFS.

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
| Local persistent (RWO, SQLite-hostile NFS) | `openebs-hostpath` | Plex config |
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
