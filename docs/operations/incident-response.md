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
    OpenTofu state is stored in Postgres on the DB VM (`tofu_state` database) but is **not treated as irreplaceable**. If the state is lost, run `tofu apply` from scratch — Tofu will create new resources. The only truly irreplaceable artifacts are the **SOPS age key** (primary on workstation; offline paper copy) and the **DB/media backups on Filen**. Everything else is reconstructable from the IaC repo.

---

## Prerequisites

Before any recovery scenario, verify you have access to the following:

| Artifact | Location | Why it matters |
|---|---|---|
| SOPS age key (primary) | `~/.ssh/` on workstation | Decrypt all secrets in the repo |
| Offline paper copy of recovery age key | Physical storage | Fallback if workstation is lost |
| Filen credentials + rclone crypt password | Offline paper | Decrypt offsite backups |
| This git repo | GitHub (source of truth) | All IaC, stack definitions, runbooks |

---

## Scenario A — Compromised Host or Service

### A1. Immediate Containment

| Step | Action | How |
|---|---|---|
| 1 | **Identify scope** | Check Gotify alerts, Grafana security dashboards, Loki logs for the affected host/service |
| 2 | **Isolate the host** | SSH in and block all traffic: `nft add rule inet filter input drop` / `nft add rule inet filter output drop` — or shut down the VM via Proxmox UI if SSH is untrusted |
| 3 | **Preserve logs** | Export Loki logs before 30-day retention expires: `logcli query '{host="<name>"}' --from=<time> --output=jsonl > /tmp/incident-<date>.jsonl` |
| 4 | **Revoke credentials** | Use the [credential rotation runbook](../automation/secrets.md) to rotate all credentials the compromised host had access to. Priority: SOPS key (if runner), Cloudflare tokens, database passwords |
| 5 | **Notify** | Send Gotify message with incident summary for personal audit trail |

### A2. Recovery

| Step | Action | How |
|---|---|---|
| 6 | **Destroy compromised VM** | `qm destroy <vmid>` on Proxmox — or wipe the LXC. Do not attempt to "clean" a compromised host |
| 7 | **Rebuild from scratch** | `ansible-playbook site.yml --limit <host>` — provisions fresh VM, joins Swarm, deploys services |
| 8 | **Restore data if needed** | Database: import from daily dump in `/mnt/backups/databases/` or from Filen. Files: `rclone copy` from Filen |
| 9 | **Verify** | Confirm service health checks pass, no unexpected processes, Loki logs on rebuilt host look clean |
| 10 | **Post-mortem** | Document: what happened, how it was detected, blast radius, what to improve |

### A3. Blast Radius Reference

Which credentials does each host have access to?

| Host | Credentials at risk if compromised |
|---|---|
| Services VM (.13) | Cloudflare token (cf-traefik), OIDC client secrets, Valkey passwords, Gotify tokens, all Swarm secrets |
| Runner LXC (.17) | SOPS age key, SSH to all hosts, Proxmox API token — **highest blast radius** |
| DB VM (.10) | All database passwords (local), rclone/Filen credentials, NFS write access to all media and backup shares |
| Monitoring VM (.11) | Prometheus scrape targets (read-only), Gotify app token, UDM SE read-only account |
| Media VM (.12) | NFS mount access to media datasets — no credentials beyond SSH |
| Synology (.2) | All NFS-exported data, DSM admin credentials |
| Proxmox (.3) | VM management — physical access to all VMs |
| UDM SE (.254) | DNS for entire lab — poisoning or misconfiguration affects all hostname resolution; **high impact** |
| DGX Spark (.4) | Swarm overlay access, GPU workloads — **normally off (WOL-gated), limited attack window** |

!!! warning "Highest-value targets"
    The **runner LXC (.17)** and **DB VM (.10)** are the highest-value targets. Runner compromise exposes the SOPS age key — all secrets become decryptable. DB VM compromise means all database access and offsite backup credentials.

---

## Scenario B — Service Failure (No Data Loss)

For a Swarm service that is down or unhealthy but underlying data is intact.

| Step | Action | How |
|---|---|---|
| 1 | **Check service status** | `docker service ps <service>` on Services VM (.13) — look for exit codes and error messages |
| 2 | **Check logs** | `docker service logs <service>` or query Loki in Grafana |
| 3 | **Check NFS mounts** | If the service uses NFS-backed volumes, verify the mount is accessible on the host: `mountpoint /mnt/media/images` |
| 4 | **Check DB connectivity** | If the service connects to a database, verify Postgres is reachable from the DB VM: `psql -h 127.0.0.1 -U postgres -c '\l'`; or from another node: `psql -h 172.16.20.10 -U postgres -c '\l'` (mode=host port) |
| 5 | **Restart the service** | `docker service update --force <service>` — forces Swarm to reschedule |
| 6 | **Redeploy from IaC** | `just deploy-stack <stack>` — redeploys the entire stack from the compose definition in the repo |
| 7 | **Check Synology / DNS** | If multiple services are failing, check Synology NFS status at `https://synology.blackcats.cc`; if DNS-related, check UDM SE DNS settings at `.254` |

---

## Scenario C — Storage Failure (Synology)

### C1. Degraded Volume (Drive Failure)

Synology will alert if a drive fails and the volume is degraded but still accessible.

| Step | Action |
|---|---|
| 1 | **Alert received** | Gotify alert from synology-exporter (or DSM email/push notification): volume degraded |
| 2 | **Identify failed drive** | DSM → Storage Manager → HDD/SSD — check drive status |
| 3 | **Replace drive** | Hot-swap the failed drive |
| 4 | **Start repair** | DSM → Storage Manager → Volume → Repair — select new drive. Repair begins automatically |
| 5 | **Monitor repair** | DSM Storage Manager or Synology mobile app — repair typically takes several hours |

### C2. Volume Unreadable or Total Synology Loss

All NFS mounts disappear. All stateful services stop or hang on NFS I/O. Restore data from Filen after hardware is fixed.

| Step | Action |
|---|---|
| 1 | **Fix hardware / reinstall DSM** | Reinstall DSM on the RS1219+. Assign static IP `172.16.20.2` |
| 2 | **Restore DSM config** | DSM → Control Panel → Update & Restore → Restore — use the most recent DSM config backup if available. Otherwise manually recreate shared folders (`media`, `backups`) on `/volume2` with Btrfs |
| 3 | **Restore NFS permissions** | Set `docker:labops` (UID 1002 / GID 1100) ownership and `2775` mode on all shared folders |
| 4 | **Run Ansible** | `ansible-playbook site.yml` — reconfigures NFS mount units on all VMs |
| 5 | **Restore media from Filen** | See [Restore Procedures](#restore-procedures) below |
| 6 | **Restore DB dumps from Filen** | `rclone copy filen-crypt:databases/ /mnt/backups/databases/` — then import each dump (see below) |
| 7 | **Restart services** | `docker service update --force <service>` or redeploy stacks via Ansible |
| 8 | **Verify** | Check service health checks, confirm data is visible in Immich and Paperless |

---

## Scenario D — DB VM Failure

The DB VM (.10) is down. All services that connect to a database (`172.16.20.10`) fail.

### D1. DB VM recoverable (no data loss)

| Step | Action |
|---|---|
| 1 | **Restart or rebuild VM** | `ansible-playbook site.yml --limit db` — Ansible redeploys Docker compose services on the DB VM |
| 2 | **Verify Postgres** | `psql -h 172.16.20.10 -U postgres -c '\l'` — confirm all expected databases exist |
| 3 | **Restart Swarm services** | `docker service update --force <service>` for each affected service |

### D2. DB VM disk lost (data loss)

Local ext4 DB data is gone. Restore from the most recent dump in the `backups/databases/` NFS share (written by the daily backup script) or from Filen.

| Step | Action |
|---|---|
| 1 | **Rebuild DB VM** | `tofu apply` + `ansible-playbook site.yml --limit db` — provisions a fresh VM at .10 |
| 2 | **Find latest dumps** | Check `/mnt/backups/databases/` on any VM that has the NFS mount, or pull from Filen |
| 3 | **Import databases** | See [Restore Procedures](#restore-procedures) below |
| 4 | **Restart services** | `docker service update --force <service>` for each database-dependent service |

---

## Scenario E — Total Site Loss

All hardware destroyed or unrecoverable. Starting from bare metal with only the git repo and Filen offsite backup.

**Recovery order matters.**

```
1. Physical layer (UDM SE, Synology, Proxmox)
2. DB VM (databases must be up before services connect)
3. VMs via Tofu + Ansible (compute + services)
4. Data restore from Filen
```

### E1. Restore Physical Layer

| Step | Action |
|---|---|
| 1 | **Rack and cable hardware** | Refer to `design/hardware.md` for switch port assignments and connections |
| 2 | **Restore UDM SE** | Power on UDM SE. Reconfigure VLANs, firewall rules, and DNS local overrides from `design/network.md` |
| 3 | **Install fresh DSM on Synology** | Boot RS1219+. Assign static IP `172.16.20.2`. Create `/volume2` Btrfs volume. Create `media` and `backups` shared folders. Configure NFS exports |
| 4 | **Install Proxmox** | Boot MS-A2 from Proxmox installer. Assign static IP `172.16.20.3` |
| 5 | **Configure Proxmox networking** | Replicate VLAN config from `design/network.md` |

### E2. Provision VMs

| Step | Action |
|---|---|
| 6 | **Build Packer template** | `just build-template` — creates Debian base VM template in Proxmox |
| 7 | **Run `tofu apply` fresh** | `just plan && just apply` — creates all VMs including DB VM at .10. No state restore needed |
| 8 | **Run Ansible** | `just configure` — configures OS, installs Docker, joins Swarm workers, deploys all stacks |

### E3. Restore Data from Filen

All offsite data is encrypted with rclone crypt. You need the rclone config and crypt password from offline paper backup.

| Step | Action |
|---|---|
| 9 | **Configure rclone on DB VM** | Deploy rclone config with Filen + filen-crypt remotes via Ansible, or manually from offline paper |
| 10 | **Restore media** | `rclone sync filen-crypt:media/images/ /mnt/media/images/`<br/>`rclone sync filen-crypt:media/paperless/ /mnt/media/paperless/`<br/>`rclone sync filen-crypt:media/gitea/ /mnt/media/gitea/` |
| 11 | **Restore DB dumps** | `rclone copy filen-crypt:databases/ /mnt/backups/databases/` |
| 12 | **Import databases** | See [Restore Procedures](#restore-procedures) below |
| 13 | **Restart services** | Services auto-start via Ansible deploy; force reschedule if needed: `docker service update --force <service>` |
| 14 | **Run verification scripts** | Level 1 integrity check + Level 2 test restore (see [Storage → Backup Strategy](../stack/storage.md)) |

**What is not restored from Filen:**

| Data | Reason |
|---|---|
| Video media (series, movies, downloads) | Re-downloadable — not backed up offsite |
| Grafana/Prometheus/Loki data | Ephemeral by design — monitoring rebuilds itself |
| Traefik `acme.json` | Traefik re-requests certs via ACME on first startup |
| Valkey (Redis) data | Ephemeral cache/broker — no state to restore |

---

## Restore Procedures

### Restore a Postgres Database from Dump

Dumps live in `/mnt/backups/databases/` (NFS from Synology) or inside `media/<service>/dbdump/` for Immich/Paperless/Gitea. Mirrored on Filen. File naming: `<YYYY-MM-DD>-<dbname>.sql`.

```bash
# On DB VM (.10) — Postgres is local here
# 1. Stop the affected service (via Swarm manager on .13)
ssh services "docker service scale <stack>_<service>=0"

# 2. Drop and recreate the database (if re-importing clean)
psql -h 127.0.0.1 -U postgres -c "DROP DATABASE IF EXISTS <dbname>;"
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE <dbname> OWNER <dbuser>;"

# 3. Import the dump
# For Immich:
psql -h 127.0.0.1 -U postgres immich < /mnt/media/images/dbdump/<date>-immich.sql
# For Paperless:
psql -h 127.0.0.1 -U postgres paperless < /mnt/media/paperless/dbdump/<date>-paperless.sql
# For others (zitadel, freshrss, tofu_state):
psql -h 127.0.0.1 -U postgres <dbname> < /mnt/backups/databases/<date>-<dbname>.sql

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
# On DB VM (.10) — rclone syncs directly to the NFS share
rclone sync filen-crypt:media/images/ /mnt/media/images/ --transfers 4 --progress
```

Immich uses the filesystem directly — no additional import step after files are in place. Restart Immich to trigger a library scan if needed.

### Restore Paperless Documents from Filen

```bash
rclone sync filen-crypt:media/paperless/ /mnt/media/paperless/ --transfers 4 --progress
```

Paperless re-indexes documents from the filesystem on restart.

### Roll Back to a Synology Btrfs Snapshot (Local Only)

If the issue is on the Synology and a recent snapshot exists:

1. Open DSM → **Snapshot Replication**
2. Select the shared folder (`media/images`, `media/paperless`, etc.)
3. Choose the snapshot and click **Recover** — or **Browse** to restore individual files

!!! warning "Rollback is destructive"
    Recovering to a snapshot overwrites all data written after that point. Use only when you need to undo recent changes, not as a substitute for restore from Filen.

---

## Scenario F — Key Loss (SOPS Age Key)

If the primary SOPS age key is lost (workstation destroyed, key deleted), secrets cannot be decrypted from the repo.

### F1. Offline paper copy available

Use the age key printed on paper to recover. Type it in or use a camera — the key is a single line starting with `AGE-SECRET-KEY-1...`. Store it in `~/.config/sops/age/keys.txt` with mode `0600`.

Then generate a new primary SSH key, add it as a SOPS recipient, and re-encrypt all secrets:

```bash
# Add new public key to .sops.yaml, then:
sops updatekeys infra/ansible/group_vars/all/secrets.sops.yml
sops updatekeys infra/terraform/secrets.sops.tfvars
git add -p && git commit -m "rotate: update SOPS recipients with new primary key"
```

### F2. All age keys lost

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

# Check Postgres (run on DB VM host)
psql -h 127.0.0.1 -U postgres -c '\l'

# Import Postgres database from dump (run on DB VM host)
psql -h 127.0.0.1 -U postgres <dbname> < /mnt/backups/databases/<date>-<dbname>.sql

# Pull from Filen (restore media) — run on DB VM
rclone sync filen-crypt:media/images/ /mnt/media/images/ --transfers 4 --progress

# Run Tofu fresh (no state required)
cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu apply'
```
