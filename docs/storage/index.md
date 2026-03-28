---
tags:
  - storage
  - truenas
  - zfs
  - nfs
---

# Storage Overview

TrueNAS DXP4800 (`172.16.20.2`, 10GbE) is the single storage authority. Compute nodes mount shares over NFS. Docker config files and ephemeral volumes stay local to each host — only data that must survive a host rebuild lives on TrueNAS.

### Data Flow

```mermaid
graph LR
    subgraph Compute["Compute Nodes"]
        SVC[Services VM .13]
        MED[Media VM .12]
        PBS[PBS VM .10]
    end

    subgraph TrueNAS["TrueNAS .2"]
        NFS[NFS Server]
        ZFS[(ZFS Pool<br/>tank)]
        PG[Postgres]
        MB[MariaDB]
        S3[MinIO S3]
    end

    SVC -->|NFS mount| NFS
    MED -->|NFS mount| NFS
    PBS -->|NFS mount| NFS
    NFS --> ZFS
    SVC -->|TCP :5432| PG
    SVC -->|TCP :9000| S3
    PG -->|local bind| ZFS
    MB -->|local bind| ZFS
    S3 --> ZFS

    style ZFS fill:#a6da95,stroke:#a6da95,color:#1e2030
    style NFS fill:#8aadf4,stroke:#8aadf4,color:#1e2030
    style S3 fill:#eed49f,stroke:#eed49f,color:#1e2030
```

## ZFS Dataset Tree

All datasets live under a single pool named `tank`, backed by a RAIDZ pool.

```
tank/
├── media/
│   ├── series          recordsize=1M  · compression=off  · atime=off
│   ├── movies          recordsize=1M  · compression=off  · atime=off
│   ├── downloads       recordsize=1M  · compression=off  · atime=off
│   ├── images          recordsize=128K · compression=lz4  · atime=off   ← Immich
│   ├── paperless       recordsize=128K · compression=zstd · atime=off
│   ├── gitea           recordsize=128K · compression=zstd · atime=off
│   └── authentik       recordsize=128K · compression=zstd · atime=off
│
├── services/                                                            ← For local containers on TrueNAS
│   ├── databases/
│   │   ├── postgres    recordsize=8K  · compression=lz4  · atime=off   ⚠ see note
│   │   ├── mariadb     recordsize=16K · compression=lz4  · atime=off   ⚠ see note
│   │   ├── pgadmin     recordsize=128K · compression=zstd · atime=off
│   │   └── databassus  recordsize=128K · compression=zstd · atime=off
│
├── s3/                 recordsize=1M  · compression=lz4  · atime=off   ← MinIO data
│
├── backups/
│   ├── databases/
│   │   ├── daily/      recordsize=128K · compression=zstd · atime=off  ← Databassus backups
│   │   └── weekly/     recordsize=128K · compression=zstd · atime=off  ← Scripted backups
│   ├── keys/           compression=zstd · ZFS native encryption (AES-256-GCM)
│   ├── pbs/            recordsize=1M  · compression=lz4  · atime=off   ← PBS chunk store
│   └── services/       recordsize=128K · compression=zstd · atime=off  ← File backup
│
└── repos/              recordsize=128K · compression=zstd · atime=off
```

<iframe
  src="storage-diagram.html"
  style="width:100%;border:none;border-radius:6px;"
  title="Storage architecture">
</iframe>

### ZFS Property Rationale

| Property | Value | Why |
|---|---|---|
| `atime=off` | All datasets | Eliminates write-on-read overhead |
| `compression=off` | Video datasets | Already compressed; CPU cost with zero gain |
| `compression=lz4` | Images, DB live, PBS, S3 | Near-zero CPU cost, moderate gain |
| `compression=zstd` | Documents, dumps, configs, repos | Good ratio, worth the CPU |
| `recordsize=1M` | Video, PBS, S3 | Large sequential reads/writes |
| `recordsize=128K` | General files | TrueNAS default; suits mixed workloads |
| `recordsize=8K` | `postgres` | **Must match Postgres page size exactly** |
| `recordsize=16K` | `mariadb` | **Must match InnoDB page size exactly** |

!!! danger "Set at creation time"
    `recordsize` and encryption must be set **at dataset creation time**. They cannot be changed after data is written. Setting these after container initialization has no effect and cannot be corrected without destroying and recreating the dataset.

### `backups/keys` — ZFS Native Encryption

`tank/backups/keys` uses ZFS native encryption (AES-256-GCM). Stores SSH keys, rclone crypt password, and long-lived secrets. No NFS export — accessible on TrueNAS locally only.

## Database Live Data Directories

`tank/services/databases/postgres` and `tank/services/databases/mariadb` hold the **live container data directories**, bind-mounted directly into their respective Docker containers running on TrueNAS (.2). These datasets are **not NFS-exported** — the database engines and their data are co-located on the same host.

!!! warning "Critical constraints for database datasets"
    - `recordsize=8K` for Postgres and `recordsize=16K` for MariaDB must be set **before** the containers first write data
    - ZFS snapshots of live DB data directories are **not crash-consistent while the engine is running** — use `pg_dump` / `mysqldump` into `backups/databases/` instead
    - All Swarm services connecting to a database must use `172.16.20.2` as the host — databases are outside the Swarm overlay network

## NFS Exports

| Dataset | Exported to | Mount point on client |
|---|---|---|
| `tank/media/series` | Media VM (.12) | `/media/series` |
| `tank/media/movies` | Media VM (.12) | `/media/movies` |
| `tank/media/downloads` | Media VM (.12) | `/media/downloads` |
| `tank/media/images` | Services VM (.13) | `/mnt/media/images` |
| `tank/media/paperless` | Services VM (.13) | `/mnt/media/paperless` |
| `tank/media/gitea` | Services VM (.13) | `/mnt/media/gitea` |
| `tank/media/authentik` | Services VM (.13) | `/mnt/media/authentik` |
| `tank/services/databases/*` | **No NFS export** | Local bind mounts on TrueNAS only |
| `tank/backups/pbs` | PBS VM (.10) | `/mnt/datastore` |
| `tank/backups/databases/weekly` | Services VM (.13) | `/mnt/backups/databases/weekly` |
| `tank/backups/services` | Services VM (.13) | `/mnt/backups/services` |
| `tank/repos` | Linux workstation | `~/repos` |
| `tank/backups/keys` | **No NFS export** | Local to TrueNAS only |

NFS options: `sync`, `no_subtree_check`.

!!! tip "NFS-export tier naming"
    New NFS-mounted datasets for Swarm services go under `tank/media/<service>`, not `tank/services/`. The `services/` tier is reserved for containers running directly on TrueNAS.

## Docker Volume Strategy

| Data type | Location | Rationale |
|---|---|---|
| Docker compose files, `.env` | Local host | Config is in git; Ansible restores on rebuild |
| Ephemeral volumes (Valkey, Traefik ACME) | Local host | Intentionally non-persistent |
| Immich photos | `tank/media/images` NFS | Irreplaceable user data |
| Paperless documents | `tank/media/paperless` NFS | Irreplaceable user data |
| Gitea data | `tank/media/gitea` NFS | Application data, mirrors GitHub |
| Authentik media | `tank/media/authentik` NFS | Custom assets, media uploads |
| Postgres data dir | `tank/services/databases/postgres` — local bind mount on TrueNAS | Engine and data co-located; no NFS |
| MariaDB data dir | `tank/services/databases/mariadb` — local bind mount on TrueNAS | Engine and data co-located; no NFS |
| pgadmin / adminer / databassus state | `tank/services/databases/<name>` — local bind mount on TrueNAS | Co-located with engines |
| PBS datastore | `tank/backups/pbs` NFS | PBS manages its own chunk store |
| reactive_resume files | TrueNAS S3 bucket | No SeaweedFS container needed |
