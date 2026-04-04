---
tags:
  - operations
  - incident-response
  - recovery
  - backup
  - disaster-recovery
---

# Incident Response & Recovery Runbooks

Lightweight containment and recovery procedures for a compromised host, a failed service, or a storage loss event. Not a formal IR plan — homelab-scale runbooks that leverage existing tooling.

!!! note "Tofu state is not critical"
    OpenTofu state is stored in MinIO on TrueNAS (`terraform-state` bucket) but is **not treated as irreplaceable**. If the state is lost, run `tofu apply` from scratch — Tofu will create new resources. The only truly irreplaceable artifacts are the **SOPS age key** (recovery copy in `tank/backups/keys/`) and the **DB/media backups on Filen**. Everything else is reconstructable from the IaC repo.

---

## Prerequisites

Before any recovery scenario, verify you have access to the following:

| Artifact | Location | Why it matters |
|---|---|---|
| SOPS age key (primary) | `~/.ssh/` on workstation | Decrypt all secrets in the repo |
| SOPS age key (recovery copy) | `tank/backups/keys/` (ZFS-encrypted, local to TrueNAS only) | Emergency fallback if workstation is lost |
| Offline paper copy of recovery age key | Physical storage | Fallback if TrueNAS is also lost |
| Filen credentials + rclone crypt password | `tank/backups/keys/` + offline paper | Decrypt offsite backups |
| This git repo | GitHub (source of truth) | All IaC, stack definitions, runbooks |

---

## Scenario A — Compromised Host or Service

### A1. Immediate Containment

| Step | Action | How |
|---|---|---|
| 1 | **Identify scope** | Check Gotify alerts, Grafana security dashboards, Loki logs for the affected host/service |
| 2 | **Isolate the host** | SSH in and block all traffic: `nft add rule inet filter input drop` / `nft add rule inet filter output drop` — or shut down the VM via Proxmox UI if SSH is untrusted |
| 3 | **Preserve logs** | Export Loki logs before 30-day retention expires: `logcli query '{host="<name>"}' --from=<time> --output=jsonl > /tmp/incident-<date>.jsonl` |
| 4 | **Revoke credentials** | Use the [credential rotation runbook](../automation/secrets.md) to rotate all credentials the compromised host had access to. Priority: SOPS key (if runner), Cloudflare tokens, database passwords, MinIO keys |
| 5 | **Notify** | Send Gotify message with incident summary for personal audit trail |

### A2. Recovery

| Step | Action | How |
|---|---|---|
| 6 | **Destroy compromised VM** | `qm destroy <vmid>` on Proxmox — or wipe the LXC. Do not attempt to "clean" a compromised host |
| 7 | **Rebuild from scratch** | `ansible-playbook site.yml --limit <host>` — provisions fresh VM, joins Swarm, deploys services |
| 8 | **Restore data if needed** | Database: import from daily dump. Files: ZFS snapshot rollback or `rclone copy` from Filen |
| 9 | **Verify** | Confirm service health checks pass, no unexpected processes, Loki logs on rebuilt host look clean |
| 10 | **Post-mortem** | Document: what happened, how it was detected, blast radius, what to improve |

### A3. Blast Radius Reference

Which credentials does each host have access to?

| Host | Credentials at risk if compromised |
|---|---|
| Services VM (.13) | Cloudflare token (cf-traefik), OIDC client secrets, Valkey passwords, Gotify tokens, all Swarm secrets |
| Runner LXC (.17) | SOPS age key, SSH to all hosts, Proxmox API token, MinIO keys — **highest blast radius** |
| Monitoring VM (.16) | Prometheus scrape targets (read-only), Gotify app token, UDM SE read-only account |
| Media VM (.12) | NFS mount access to media datasets — no credentials beyond SSH |
| TrueNAS (.2) | All database passwords (local), MinIO keys, NFS exports, ZFS encryption key |
| Proxmox (.3) | VM management, PBS access — physical access to all VMs |
| Raspberry Pi (.1) | DNS zone data, NTP authority — DNS poisoning affects entire lab; **high impact** |
| DNS VM (.11) | Secondary Technitium — zone config tampering, DNS manipulation; Swarm overlay access |
| DGX Spark (.4) | Swarm overlay access, GPU workloads — **normally off (WOL-gated), limited attack window** |

!!! warning "Highest-value targets"
    The **runner LXC (.17)** and **TrueNAS (.2)** are the highest-value targets. Runner compromise exposes the SOPS age key — all secrets become decryptable. TrueNAS compromise means all data access and database access. Both warrant the most vigilant monitoring.

---

## Scenario B — Service Failure (No Data Loss)

For a Swarm service that is down or unhealthy but underlying data is intact.

| Step | Action | How |
|---|---|---|
| 1 | **Check service status** | `docker service ps <service>` on Services VM (.13) — look for exit codes and error messages |
| 2 | **Check logs** | `docker service logs <service>` or query Loki in Grafana |
| 3 | **Check NFS mounts** | If the service uses NFS-backed volumes, verify the mount is accessible on the host: `mountpoint /mnt/media/images` |
| 4 | **Restart the service** | `docker service update --force <service>` — forces Swarm to reschedule |
| 5 | **Redeploy from IaC** | `just deploy-stack <stack>` — redeploys the entire stack from the compose definition in the repo |
| 6 | **Check TrueNAS** | If multiple services are failing, check TrueNAS health and NFS export status at `https://truenas.blackcats.cc` |

---

## Scenario C — Storage Failure (TrueNAS)

### C1. Single Drive Failure (RAIDZ1 survives)

The pool tolerates one drive failure. The RAIDZ1 vdev is degraded but fully operational.

| Step | Action |
|---|---|
| 1 | **Alert received** | Gotify alert from truenas-exporter: pool degraded |
| 2 | **Identify failed drive** | TrueNAS UI → Storage → Pool status. Note the failed vdev member |
| 3 | **Replace drive** | Hot-swap the failed drive (or power down if hot-swap not available) |
| 4 | **Start resilver** | TrueNAS → Storage → Pool → Replace → select new drive. Resilver begins automatically |
| 5 | **Monitor resilver** | TrueNAS UI or `zpool status tank` — resilver typically takes several hours for 12 TB drives |
| 6 | **Post-resilver scrub** | After resilver completes, queue a manual scrub: TrueNAS → Storage → Pool → Scrub |

!!! warning "RAIDZ1 risk window"
    During resilver, a second drive failure means total pool loss. Minimise activity on TrueNAS during this window. Resilver progress is visible in TrueNAS UI and truenas-exporter metrics.

### C2. SLOG (SSD) Failure

The SLOG is unmirrored. If the SSD fails during a power event, at most the last few seconds of in-flight synchronous writes are lost. The pool itself remains importable and all RAIDZ1 HDD data is intact.

| Step | Action |
|---|---|
| 1 | **Import pool without SLOG** | `zpool import -f tank` if TrueNAS reboots and the pool does not auto-import |
| 2 | **Remove failed SLOG** | TrueNAS UI → Storage → Pool → SLOG → Remove |
| 3 | **Verify pool health** | `zpool status tank` — should show `ONLINE` without SLOG |
| 4 | **Check data integrity** | Run a scrub: TrueNAS → Storage → Pool → Scrub. Check for checksum errors |
| 5 | **Replace SLOG** | Once a replacement SSD is available, add it back as a new SLOG via TrueNAS UI |

### C3. Pool Unimportable or Total TrueNAS Loss

All drives unreadable, pool corrupted, or TrueNAS hardware failure with no surviving pool.

**Before starting:** Locate the most recent TrueNAS system config backup in Filen (`filen-crypt:services/truenas/truenas-config-<date>.tar.gz`). This contains pool layout, dataset structure, network config, user accounts, ACME config, and credentials — essential for reconstructing the exact environment before restoring data from Filen.

| Step | Action |
|---|---|
| 1 | **Install fresh TrueNAS SCALE** | Boot the DXP4800 from TrueNAS installer USB |
| 2 | **Restore system config** | TrueNAS UI → System → General → Upload Config — use the most recent `truenas-config-<date>.tar.gz` downloaded from Filen |
| 3 | **Verify pool and datasets** | Pool should auto-import if drives are intact. Verify dataset tree matches `design/storage.md` |
| 4 | **Run `tofu apply` fresh if needed** | If VMs were lost, run `tofu apply` from scratch — Tofu state is disposable; it will create new VMs. No need to restore state first. |
| 5 | **Run Ansible** | `ansible-playbook site.yml` — configures all hosts, deploys services, restores NFS mounts |
| 6 | **Restore DB dumps from Filen** | `rclone copy filen-crypt:databases/ /mnt/tank/backups/databases/` — then import each dump (see [Restore Procedures](#restore-procedures) below) |
| 7 | **Restore media from Filen** | `rclone sync filen-crypt:media/images/ /mnt/tank/media/images/` and `filen-crypt:media/paperless/ /mnt/tank/media/paperless/` |
| 8 | **Restart services** | `docker service update --force <service>` for each affected service, or redeploy stacks via Ansible |
| 9 | **Verify** | Check service health checks, confirm data is visible in Immich and Paperless |

!!! note "Tofu state after TrueNAS loss"
    The MinIO `terraform-state` bucket is lost with TrueNAS. This is acceptable — just run `tofu apply` fresh. Tofu will create new VMs from the Packer template. The IaC repo contains everything needed to rebuild; no manual state recovery is required.

---

## Scenario D — Total Site Loss

All hardware destroyed or unrecoverable. Starting from bare metal with only the git repo and Filen offsite backup.

**Recovery order matters.** Services depend on storage; storage depends on compute; compute depends on Proxmox and TrueNAS. Work bottom-up.

```
1. Physical layer
2. TrueNAS (storage + databases)
3. Proxmox (hypervisor)
4. VMs via Tofu + Ansible (compute + services)
5. Data restore from Filen
```

### D1. Restore Physical Layer

| Step | Action |
|---|---|
| 1 | **Rack and cable hardware** | Refer to `design/hardware.md` for switch port assignments and connections |
| 2 | **Install TrueNAS SCALE** | Boot DXP4800 from installer. Assign static IP `172.16.20.2` |
| 3 | **Restore TrueNAS system config** | Download `truenas-config-<date>.tar.gz` from Filen (need rclone + Filen credentials from offline paper). Upload via TrueNAS UI → System → General → Upload Config |
| 4 | **Install Proxmox** | Boot MS-A2 from Proxmox installer. Assign static IP `172.16.20.3` |
| 5 | **Configure Proxmox networking** | Replicate VLAN config from `design/network.md` |

### D2. Provision VMs

| Step | Action |
|---|---|
| 6 | **Build Packer template** | `just build-template` — creates Debian base VM template in Proxmox |
| 7 | **Run `tofu apply` fresh** | `just plan && just apply` — creates all VMs from the Packer template and creates DNS records. Tofu state was in MinIO but is gone; Tofu will create everything from scratch using the IaC definitions in the repo. No state restore needed. |
| 8 | **Run Ansible** | `just configure` — configures OS, installs Docker, joins Swarm workers, deploys all stacks |

!!! note "Why Tofu state loss is fine"
    Tofu state only describes what Tofu last created. If it is gone, Tofu creates new resources. The VMs are stateless (config is in git; data is on TrueNAS). Running `tofu apply` with no prior state is the correct recovery path.

### D3. Restore Data from Filen

All offsite data is encrypted with rclone crypt. You need the rclone config and crypt password (from `tank/backups/keys/` — but TrueNAS is rebuilt first; alternatively from offline paper backup).

| Step | Action |
|---|---|
| 9 | **Configure rclone on TrueNAS** | Deploy rclone config with Filen + filen-crypt remotes. Credentials from offline paper if `tank/backups/keys` not yet restored |
| 10 | **Restore media** | `rclone sync filen-crypt:media/images/ /mnt/tank/media/images/` `rclone sync filen-crypt:media/paperless/ /mnt/tank/media/paperless/` |
| 11 | **Restore DB dumps** | `rclone copy filen-crypt:databases/ /mnt/tank/backups/databases/` |
| 12 | **Import databases** | See [Restore Procedures](#restore-procedures) below |
| 13 | **Restore Gitea data** | `rclone sync filen-crypt:media/gitea/ /mnt/tank/media/gitea/` |
| 14 | **Restart services** | Services should auto-start via Ansible deploy; force reschedule if needed: `docker service update --force <service>` |
| 15 | **Run verification scripts** | Level 1 integrity check + Level 2 test restore (see [Storage → Backup Strategy](../stack/storage.md)) |

**What is not restored from Filen:**

| Data | Reason |
|---|---|
| Video media (series, movies, downloads) | Re-downloadable — not backed up offsite |
| PBS datastore | Local recovery path only — rebuilt by re-running PBS backup jobs |
| Grafana/Prometheus/Loki data | Ephemeral by design — monitoring rebuilds itself |
| Traefik `acme.json` | Traefik re-requests certs via ACME on first startup |
| Valkey (Redis) data | Ephemeral cache/broker — no state to restore |

---

## Restore Procedures

### Restore a Postgres Database from Dump

Dumps live in `tank/backups/databases/` locally and mirrored on Filen. File naming: `<YYYY-MM-DD>-<dbname>.sql`.

```bash
# On TrueNAS (.2) — Postgres is co-located here
# 1. Stop the affected service (via Swarm manager on .13)
ssh services "docker service scale <stack>_<service>=0"

# 2. Drop and recreate the database (if re-importing clean)
psql -h 127.0.0.1 -U postgres -c "DROP DATABASE IF EXISTS <dbname>;"
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE <dbname> OWNER <dbuser>;"

# 3. Import the dump
psql -h 127.0.0.1 -U postgres <dbname> < /mnt/tank/backups/databases/<date>-<dbname>.sql

# 4. Restart the service
ssh services "docker service scale <stack>_<service>=1"
```

For a point-in-time restore from the monthly archive:

```bash
rclone copy "filen-crypt:archive/<YYYY-MM>/<date>-<dbname>.sql" /tmp/
psql -h 127.0.0.1 -U postgres <dbname> < /tmp/<date>-<dbname>.sql
```

### Restore Immich Photos from Filen

```bash
# On TrueNAS (.2) — rclone syncs directly to the NFS dataset
rclone sync filen-crypt:media/images/ /mnt/tank/media/images/ --transfers 4 --progress
```

Immich uses the filesystem directly — no additional import step required after the files are in place. Restart Immich to trigger a library scan if needed.

### Restore Paperless Documents from Filen

```bash
rclone sync filen-crypt:media/paperless/ /mnt/tank/media/paperless/ --transfers 4 --progress
```

Paperless re-indexes documents from the filesystem on restart.

### Roll Back to a ZFS Snapshot (Local Only)

If the issue is on TrueNAS itself and a recent snapshot exists:

```bash
# List available snapshots
zfs list -t snapshot tank/media/images

# Roll back (destroys all data written after the snapshot)
zfs rollback tank/media/images@autosnap-<YYYY-MM-DD>_03.05
```

!!! warning "Rollback is destructive"
    `zfs rollback` destroys all data written after the snapshot point. Use only when you need to undo recent changes, not as a substitute for restore from Filen.

---

## Scenario E — Key Loss (SOPS Age Key)

If the primary SOPS age key is lost (workstation destroyed, key deleted), secrets cannot be decrypted from the repo.

### E1. Recovery key is available (`tank/backups/keys/`)

```bash
# On TrueNAS — the recovery key is in the ZFS-encrypted dataset
# Copy the recovery key to your new workstation
scp truenas:/mnt/tank/backups/keys/age-recovery.key ~/.config/sops/age/keys.txt
chmod 600 ~/.config/sops/age/keys.txt

# Verify decryption works
sops -d infra/ansible/group_vars/all/secrets.sops.yml
```

Then generate a new primary SSH key, add it as a SOPS recipient, and re-encrypt all secrets:

```bash
# Add new public key to .sops.yaml, then:
sops updatekeys infra/ansible/group_vars/all/secrets.sops.yml
sops updatekeys infra/terraform/secrets.sops.tfvars
git add -p && git commit -m "rotate: update SOPS recipients with new primary key"
```

### E2. Only offline paper copy available

Use the age key printed on paper to recover. Type it in or use a camera — the key is a single line starting with `AGE-SECRET-KEY-1...`. Store it in `~/.config/sops/age/keys.txt` with mode `0600`, then follow the steps in E1 above.

### E3. All age keys lost

All secrets are unrecoverable from the encrypted files in git. You must re-enter all credentials manually:

1. Rotate all credentials (generate new values for everything — see credential rotation runbook in [Secrets](../automation/secrets.md))
2. Re-create `secrets.sops.yml` and `secrets.sops.tfvars` from scratch with a new age key
3. Redeploy all services via Ansible

This is a worst-case scenario. The offline paper copy exists to prevent it.

---

## Quick Reference — Recovery Commands

```bash
# Rebuild a single VM
ansible-playbook site.yml --limit <hostname>

# Redeploy a single stack
just deploy-stack <stackname>

# Force restart a Swarm service
docker service update --force <stack>_<service>

# Check Swarm service status
docker service ps <stack>_<service>

# Check NFS mounts on a host
mountpoint /mnt/media/images && echo "OK" || echo "NOT MOUNTED"

# Import Postgres database from dump
psql -h 127.0.0.1 -U postgres <dbname> < /mnt/tank/backups/databases/<date>-<dbname>.sql

# Pull from Filen (restore media)
rclone sync filen-crypt:media/images/ /mnt/tank/media/images/ --transfers 4 --progress

# Run Tofu fresh (no state required)
cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu apply'
```
