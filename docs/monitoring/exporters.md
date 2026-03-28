---
tags:
  - monitoring
  - exporters
  - promtail
  - loki
---

# Exporters & Log Shipping

## Exporters — All Hosts

Deployed as systemd units by the Ansible `common` role on every host (Pi, TrueNAS, Proxmox, all VMs, DGX Spark):

| Exporter | What it covers |
|---|---|
| `node_exporter` | CPU, RAM, disk, network, hardware temperatures (`--collector.hwmon`) |
| `promtail` | Ships logs to Loki |

=== "DGX Spark only"

    | Exporter | What it covers |
    |---|---|
    | `dcgm-exporter` | GPU temperature, power draw, memory bandwidth, utilisation per GPU |

    `dcgm-exporter` requires NVIDIA driver access and runs directly on the DGX Spark host. Prometheus scrapes it remotely.

=== "Swarm global service"

    | Service | What it covers |
    |---|---|
    | `cAdvisor` | Per-container CPU, memory, and network — mounts Docker socket and cgroups |

=== "Remote API exporters"

    These exporters run as containers pinned to the Monitoring VM. They poll remote APIs and expose Prometheus metrics locally.

    | Exporter | Target | Key metrics |
    |---|---|---|
    | `pve_exporter` | Proxmox API | Per-VM CPU/RAM/disk, host resource usage, storage pool status |
    | `truenas-exporter` | TrueNAS REST API | Pool health, vdev status, dataset usage, ZFS ARC stats, SMART data |
    | `unifi-poller` | UDM SE local API | WAN throughput, switch port traffic and errors, PoE budgets per port, connected client count, AP radio stats, device temperatures |

    !!! note "unifi-poller credentials"
        Requires a read-only local account on the UDM SE. Credentials stored in SOPS-encrypted Ansible secrets.

## Log Shipping — Promtail

Promtail is deployed as a systemd service on every host via Ansible. It tails:

- `/var/log/` — system logs (syslog, auth, kernel)
- Docker container logs via `/var/run/docker.sock` — primary source for all Swarm service logs
- `/var/log/pve/` — Proxmox cluster and task logs (Proxmox host only, labelled `job=pve`)

### Labels Applied to All Log Streams

| Label | Source | Example |
|---|---|---|
| `host` | Hostname | `services`, `proxmox`, `pi` |
| `job` | Log source type | `docker`, `syslog`, `pve` |
| `container_name` | Docker container name | `traefik`, `immich_server` |

Loki retention: 30 days configured via Loki's `retention_period`. Logs are ephemeral — losing the Monitoring VM loses recent logs, which is acceptable.

## Prometheus Scrape Targets

Static scrape targets are defined in `prometheus.yml`, templated by Ansible. No service discovery needed at this scale.

| Job | Targets | Port |
|---|---|---|
| `node` | All hosts (static list) | `9100` |
| `cadvisor` | All Swarm nodes (static list) | `8080` |
| `dcgm` | DGX Spark (`.4`) | `9400` |
| `pve` | pve_exporter on Monitoring VM | `9221` |
| `truenas` | truenas-exporter on Monitoring VM | `9108` |
| `unifi` | unifi-poller on Monitoring VM | `9130` |
