---
tags:
  - storage
  - truenas
  - zfs
  - nfs
---

# Storage Overview

TrueNAS DXP4800 (`172.16.20.2`, 10GbE) is the single storage authority. Compute nodes mount shares over NFS. Docker config files and ephemeral volumes stay local to each host — only data that must survive a host rebuild lives on TrueNAS.

## ZFS Dataset Tree

All datasets live under a single pool named `tank`, backed by a RAIDZ pool.

```
tank/
├── media/
│   ├── series          recordsize=1M  · compression=off  · atime=off
│   ├── movies          recordsize=1M  · compression=off  · atime=off
│   ├── downloads       recordsize=1M  · compression=off  · atime=off
│   ├── images          recordsize=128K · compression=lz4  · atime=off   ← Immich
│   └── paperless       recordsize=128K · compression=zstd · atime=off
│
├── services/
│   ├── databases/
│   │   ├── postgres    recordsize=8K  · compression=lz4  · atime=off   ⚠ match Postgres page size
│   │   ├── mariadb     recordsize=16K · compression=lz4  · atime=off   ⚠ match InnoDB page size
│   │   ├── pgadmin     recordsize=128K · compression=zstd · atime=off
│   │   └── databassus  recordsize=128K · compression=zstd · atime=off
│   └── pbs             recordsize=1M  · compression=lz4  · atime=off
│
├── s3/                 recordsize=1M  · compression=lz4  · atime=off   ← MinIO data
│
├── backups/
│   ├── databases/
│   │   ├── daily/      recordsize=128K · compression=zstd · atime=off
│   │   └── weekly/     recordsize=128K · compression=zstd · atime=off
│   ├── keys/           compression=zstd · ZFS native encryption (AES-256-GCM)
│   └── services/       recordsize=128K · compression=zstd · atime=off
│
├── pxe/
│   ├── iso             recordsize=1M  · compression=off  · atime=off
│   └── tftp            recordsize=128K · compression=zstd · atime=off
│
└── repos/              recordsize=128K · compression=zstd · atime=off
```

### ZFS property rationale

| Property | Value | Why |
|---|---|---|
| `atime=off` | All datasets | Eliminates write-on-read overhead |
| `compression=off` | Video datasets | Already compressed; CPU cost with zero gain |
| `compression=lz4` | Images, DB live, PBS, S3 | Near-zero CPU cost, moderate gain |
| `compression=zstd` | Documents, dumps, configs, repos | Good ratio, worth the CPU |
| `recordsize=1M` | Video, PBS, ISO, S3 | Large sequential reads/writes |
| `recordsize=128K` | General files | TrueNAS default; suits mixed workloads |
| `recordsize=8K` | `postgres` | **Must match Postgres page size exactly** |
| `recordsize=16K` | `mariadb` | **Must match InnoDB page size exactly** |

!!! warning
    `recordsize` and encryption must be set **at dataset creation time**. They cannot be changed after data is written.

### `backups/keys` — ZFS native encryption

`tank/backups/keys` uses ZFS native encryption (AES-256-GCM). Stores SSH keys, rclone crypt password, and long-lived secrets. No NFS export — accessible on TrueNAS locally only.

## NFS Exports

| Dataset | Exported to | Mount point on client |
|---|---|---|
| `tank/media/series` | Media VM (.12) | `/media/series` |
| `tank/media/movies` | Media VM (.12) | `/media/movies` |
| `tank/media/downloads` | Media VM (.12) | `/media/downloads` |
| `tank/media/images` | Services VM (.13) | `/mnt/media/images` |
| `tank/media/paperless` | Services VM (.13) | `/mnt/media/paperless` |
| `tank/services/databases/postgres` | Services VM (.13) | `/mnt/services/postgres` |
| `tank/services/databases/mariadb` | Services VM (.13) | `/mnt/services/mariadb` |
| `tank/services/databases/pgadmin` | Services VM (.13) | `/mnt/services/pgadmin` |
| `tank/services/databases/databassus` | Services VM (.13) | `/mnt/services/databassus` |
| `tank/services/pbs` | PBS VM (.10) | `/mnt/datastore` |
| `tank/backups/databases` | Services VM (.13) | `/mnt/backups/databases` |
| `tank/backups/services` | Services VM (.13) | `/mnt/backups/services` |
| `tank/pxe/iso` | Proxmox (.3), Lab VM (.15) | `/mnt/iso` |
| `tank/pxe/tftp` | Pi (.1), DNS VM (.11) | `/mnt/tftp` |
| `tank/repos` | Linux workstation | `~/repos` |
| `tank/backups/keys` | **No export** | Local to TrueNAS only |

NFS options: `sync`, `no_subtree_check`. DB mounts additionally use `no_root_squash` (required for container UID mapping).

## Docker Volume Strategy

| Data type | Location | Rationale |
|---|---|---|
| Docker compose files, `.env` | Local host | Config is in git; Ansible restores on rebuild |
| Ephemeral volumes (Valkey, Traefik ACME) | Local host | Intentionally non-persistent |
| Immich photos | `tank/media/images` NFS | Irreplaceable user data |
| Paperless documents | `tank/media/paperless` NFS | Irreplaceable user data |
| Postgres data dir | `tank/services/databases/postgres` NFS | Survives Services VM rebuild |
| MariaDB data dir | `tank/services/databases/mariadb` NFS | Survives Services VM rebuild |
| Admin tool state | `tank/services/databases/<name>` NFS | Preserves saved connections |
| PBS datastore | `tank/services/pbs` NFS | PBS manages its own chunk store |
| reactive_resume files | TrueNAS S3 bucket | No SeaweedFS container needed |
