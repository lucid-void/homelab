# Homelab — Claude Context

## What this repo is

Infrastructure-as-Code repository for a personal homelab that doubles as a production environment. Everything from VM provisioning to service deployment is managed declaratively.

## Design specs

All design specs live in **[design/](design/)** — gitignored, never committed. Read only the file(s) relevant to your current task.

The **[docs/](docs/)** directory is committed and published to GitHub Pages (MkDocs Material site). It mirrors the design files in a user-facing format. Both must be kept in sync — see the "Keeping design and docs in sync" section. The recovery runbook lives at **[docs/operations/incident-response.md](docs/operations/incident-response.md)** (committed, accessible from GitHub even when local systems are down).

| File | Covers |
|---|---|
| [hardware.md](design/hardware.md) | Physical devices, switch ports, connections |
| [network.md](design/network.md) | VLANs, trust model, jumbo frames, DNS, NTP |
| [compute.md](design/compute.md) | Proxmox VMs, Swarm topology, node placement, overlay networks |
| [storage.md](design/storage.md) | ZFS datasets, NFS exports, backups, MinIO, DB hosting |
| [certificates.md](design/certificates.md) | ACME DNS-01, per-service cert strategy, Cloudflare tokens |
| [iac-pipeline.md](design/iac-pipeline.md) | Packer, Tofu, Ansible, Gitea, CI/CD, secrets, image management |
| [monitoring.md](design/monitoring.md) | Prometheus, Loki, Grafana, exporters, alerting |
| [docs-site.md](design/docs-site.md) | MkDocs Material site, GitHub Pages |
| [sso.md](design/sso.md) | Authentik, Authelia, OIDC, forward auth |
| [incident-response.md](design/incident-response.md) | Containment runbooks, blast radius, recovery procedures |

### Which design files to read

| If your task involves... | Read |
|---|---|
| Adding/modifying a Swarm service | `compute.md`, `storage.md` |
| Changing DNS records or NTP | `network.md` |
| Provisioning a new VM or LXC | `compute.md`, `iac-pipeline.md` |
| Adding NFS mounts or ZFS datasets | `storage.md` |
| Certificates or HTTPS setup | `certificates.md` |
| Ansible playbooks or Tofu modules | `iac-pipeline.md` |
| Dashboards, alerts, or exporters | `monitoring.md` |
| Gitea, CI runners, or pipelines | `iac-pipeline.md` |
| SSO, OIDC, or auth middleware | `sso.md` |
| Backup or recovery procedures | `storage.md`, `incident-response.md`; full recovery runbook at `docs/operations/incident-response.md` (committed, accessible from GitHub) |
| Documentation site changes | `docs-site.md` |

## Keeping design and docs in sync with implementation

Design files (`design/`) and the docs site (`docs/`) describe the **intended and implemented** state of the homelab — not just plans. Once implementation begins, reality takes precedence over the design.

**When you make any change to IaC (Ansible, Tofu, Packer, compose files, scripts):**
- If the change differs from what the relevant design file describes, update the design file to match what was actually built.
- If the change affects something documented in `docs/`, update the docs page too.
- Do not leave design files describing a plan that was superseded by implementation choices.

**Specifically:**
- Changed a VM's resource allocation, IP, or placement → update `design/compute.md` and `docs/stack/compute.md`
- Changed a ZFS dataset, NFS export, or backup script → update `design/storage.md` and `docs/stack/storage.md`
- Changed a DNS record, VLAN, or firewall rule → update `design/network.md` and `docs/stack/network.md`
- Added or removed a Swarm service → update `design/compute.md`, `docs/stack/services.md`, and relevant design files
- Changed a monitoring exporter, alert, or scrape target → update `design/monitoring.md` and `docs/operations/alerting.md`
- Changed secrets handling, CI pipeline, or SOPS config → update `design/iac-pipeline.md` and `docs/automation/`
- Changed the backup script or recovery procedure → update `design/storage.md`, `docs/stack/storage.md`, and `docs/operations/incident-response.md`

**This applies to the key decisions table too.** If a decision changes during implementation (e.g. a service is swapped, a tool is replaced, an approach is simplified), update the key decisions entry — don't leave it describing the original plan.

## Visual companion

Topology diagrams and service placement maps are generated during brainstorming sessions using a local browser companion. Saved diagrams live in `.superpowers/brainstorm/`.

To resume visual brainstorming in a new session, start the server:

```bash
/home/arcana/.claude/plugins/cache/claude-plugins-official/superpowers/5.0.6/skills/brainstorming/scripts/start-server.sh \
  --project-dir /home/arcana/repos/Homelab
```

The server returns a JSON object with `url`, `screen_dir`, and `state_dir`. Open the URL in a browser, then write HTML content fragments to `screen_dir` to display diagrams.

## TrueNAS ZFS dataset tiers (important naming convention)

| Tier | Path | Purpose |
|---|---|---|
| NFS-export tier | `tank/media/` | Data mounted by Swarm VMs over NFS — not just video, includes Immich, Paperless, Gitea data |
| Local-to-TrueNAS | `tank/services/` | Data for services running directly on TrueNAS (Postgres, MariaDB, pgadmin, etc.) — never NFS-exported |
| S3 | `tank/s3/` | MinIO data |
| Backups | `tank/backups/` | DB dumps, PBS chunk store, offsite sync staging |
| Repos | `tank/repos/` | Git repos, exported to Linux desktop |

When adding a new NFS-mounted dataset for a Swarm service, it goes under `tank/media/<service>`, not `tank/services/`.

## Key decisions (do not re-litigate without reason)

| Topic | Decision |
|---|---|
| SeaweedFS | Eliminated — reactive_resume also removed from stack; no S3 consumer remains |
| DB backups | Daily coordinated script only (stop → dump → ZFS snapshot → start); Databasus was evaluated and scrapped |
| DB backup scope | Databases in scope: immich, paperless, gitea, zitadel, freshrss, tofu_state — all on TrueNAS Postgres; 30-day retention in tank/backups/databases/ |
| Backup timeout | 4-hour total timeout on backup script; emits heartbeat timestamp on success; Prometheus alerts if older than 25 hours |
| Offsite backups | rclone crypt remote → Filen cloud; credentials in tank/backups/keys (ZFS-encrypted) |
| Database hosting | Postgres, MariaDB, pgadmin, adminer run as Docker apps on TrueNAS (.2) — engines co-located with data, no NFS for DB volumes |
| Docker volumes | Config + ephemeral = local host; Immich/Paperless data = TrueNAS NFS; DB data dirs = local bind mounts on TrueNAS |
| Certificates | Each service manages its own — see `design/certificates.md` |
| Cloudflare API tokens | One token per consumer (Traefik, Proxmox, PBS, TrueNAS), Zone→DNS→Edit on blackcats.cc only; isolated for independent revocation |
| Traefik certs | Per-service ACME DNS-01; acme.json in local named volume on Services VM (.13) |
| Proxmox / PBS certs | Built-in ACME client (`pvenode` / `proxmox-backup-manager`), Cloudflare plugin, provisioned by Ansible |
| TrueNAS cert | Built-in ACME via REST API, provisioned by Ansible |
| Traefik Swarm labels | Must be under `deploy: labels:` — top-level `labels:` are invisible to the Swarm API |
| Traefik Swarm mode | `providers.docker.swarmMode = true` required; `dnsChallenge.resolvers` must point to 1.1.1.1, not Technitium |
| DNS | Technitium on Pi (.1) primary + DNS VM (.11) secondary via config sync (not traditional zone transfer — no SOA serial monitoring needed) |
| NTP | chrony on both DNS nodes |
| Redis replacement | Valkey for all cache/broker instances |
| Immich ML | Runs on CPU on Services VM (.13) — DGX is WOL-gated |
| PBS placement | Standalone VM at .10, NFS datastore on TrueNAS |
| Swarm manager | Single manager at Services VM (.13) |
| Netbird / ZeroTier | Plain docker compose on Game VM (.14) with --network host, outside Swarm |
| Monitoring stack | Dedicated VM at .16 (Swarm worker); Prometheus + Loki + Grafana; all storage local/ephemeral |
| Monitoring alerting | Grafana built-in alerting → Gotify via Swarm overlay DNS (`http://gotify:80/message`, NOT the domain — avoids circular dependency if .13 is down); no Alertmanager |
| Service health / cert expiry | Uptime Kuma on Monitoring VM (.16) — HTTP/HTTPS checks + cert expiry; replaces blackbox_exporter for this use case |
| Grafana dashboards | All dashboards, data sources, and alert rules provisioned by Ansible; nothing created manually in UI (ephemeral) |
| DGX Spark alerts | WOL-managed (off by default) — no `up == 0` alerts; disk/GPU/resource alerts active when host is up; label `wol_managed: "true"` excludes from down alerts |
| Log shipping | Promtail as Ansible systemd unit on every host (including Pi, TrueNAS, Proxmox) |
| Container metrics | cAdvisor as Swarm global service |
| GPU metrics | dcgm-exporter on DGX Spark (.4), scraped by Prometheus |
| Network metrics | unifi-poller on monitoring VM, polls UDM SE local API |
| TrueNAS metrics | truenas-exporter on monitoring VM, polls TrueNAS REST API (not Netdata) |
| Proxmox metrics | pve_exporter (API, on monitoring VM) + node_exporter with hwmon (on Proxmox host) |
| Gitea | Swarm service on Services VM (.13); data on `tank/media/gitea` NFS; shared Postgres; scheduled mirror from GitHub every 10 min; Gitea Actions CI |
| Gitea runner | Dedicated Debian LXC on Proxmox at 172.16.20.17; act_runner; SOPS age key deployed by Ansible |
| Tofu apply | Always manual — CI never auto-applies |
| SSO provider | Zitadel — single user store, manages all credentials and 2FA; Go binary backed by Postgres only |
| SSO forward auth | Authelia as Traefik middleware; authenticates against Zitadel via OIDC; no own user DB |
| SSO native OIDC | Apps with native support connect directly to Zitadel; determined per-service at deployment |
| SSO placement | Zitadel + Authelia both on Services VM (.13); dedicated Valkey for Authelia only (Zitadel needs none) |
| Internet exposure | Cloudflare DNS used only for valid TLS certs (DNS-01); all A records → internal IPs; no port forwarding on UDM SE; access requires local network, Netbird (main VPN), or ZeroTier (gaming); no Cloudflare proxy |
| Netbird vs ZeroTier | Netbird = primary remote access VPN; ZeroTier = gaming with friends only |
| Backup script | Lives in IaC repo, deployed by Ansible as cron job to TrueNAS (.2); runs on TrueNAS (local ZFS + DB access); includes daily TrueNAS config export (`GET /api/v2.0/config/save` → tank/backups/services/truenas/) |
| Tofu state | Stored in PostgreSQL on TrueNAS (`tofu_state` database); backed up daily by pg_dump alongside other databases; if lost, run `tofu apply` fresh |
| Recovery path | Primary: `tofu apply` + `ansible-playbook`; PBS restore = secondary/point-in-time fallback only |
| NFS / Postgres traffic | Cleartext on internal VLAN — accepted risk; private network, VPN-gated, mitigated by planned nftables |
| SOPS keys | Single age key for all secrets (no per-function scoping — ineffective against runner compromise anyway); backup recovery key in tank/backups/keys/ + offline paper copy |
| Docker image pinning | Minor semver (e.g. `traefik:v3.1`) — no rolling major tags, no digests |
| Swarm restart policy | `condition: any` — valid values: none, on-failure, any; `unless-stopped` is invalid in Swarm |
| Drift detection | Weekly scheduled CI: `tofu plan` + `ansible-playbook --check --diff` → report to Gotify; read-only, never auto-applies |
| Pi OS | DietPi (low-write, SD card longevity); Technitium query logging disabled/in-memory; SD failure → rebuild via Ansible (no persistent data) |
| Host firewall | Per-host nftables planned (default deny inbound, SSH/node_exporter/Promtail allowlist, per-host service overrides); not yet implemented |
| SSO rate limiting | Traefik rateLimit middleware on Zitadel/Authelia login endpoints; Zitadel built-in lockout; Authelia regulation (max_retries, ban_time) |
| UniFi backup | Not backed up — VLAN/firewall rules reconfigured manually after reset; config is simple enough to accept this |
| Swarm viability | Staying on Docker Swarm; migration trigger: >50 services, need for CRDs/operators, or Swarm dropped from Docker Engine |

## IP map (quick reference)

```
172.16.20.1   Raspberry Pi          — physical, Swarm worker, primary DNS/NTP
172.16.20.2   TrueNAS DXP4800       — physical, storage + database host (Postgres, MariaDB, pgadmin, adminer)
172.16.20.3   Proxmox MS-A2         — physical, hypervisor
172.16.20.4   DGX Spark             — physical, Swarm worker (GPU), WOL
172.16.20.5–9 reserved              — future physical devices
172.16.20.10  PBS VM                — NOT in Swarm, NFS datastore → TrueNAS
172.16.20.11  DNS/NTP VM            — Swarm worker, secondary Technitium
172.16.20.12  Media VM              — Swarm worker, Plex/*arr/download stack
172.16.20.13  Services VM           — Swarm MANAGER, Traefik/Paperless/Immich/Gitea/Zitadel/Authelia/etc.
172.16.20.14  Game VM               — Swarm worker, Satisfactory/Netbird/ZeroTier
172.16.20.15  Lab VM                — Swarm worker, ephemeral/testing
172.16.20.16  Monitoring VM         — Swarm worker, Prometheus/Loki/Grafana/exporters
172.16.20.17  Gitea runner LXC      — Proxmox LXC, act_runner, not a Swarm member
172.16.20.18+ future VMs/LXCs
```

## IaC stack

- **Packer** — Debian base VM template stored in Proxmox
- **OpenTofu** — VM provisioning + DNS records; state in TrueNAS PostgreSQL (`tofu_state` database)
- **Ansible** — OS config, Docker, Swarm join, stack deployment, certs; hybrid inventory (static physical + dynamic Proxmox API)
- **Secrets** — SOPS + age using SSH key as recipient; encrypted files committed to git
- **Task runner** — `justfile`
- **CI/CD** — Gitea Actions on self-hosted LXC runner at .17; GitHub Actions used only for docs (GitHub Pages)
