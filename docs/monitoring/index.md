---
tags:
  - monitoring
  - prometheus
  - loki
  - grafana
---

# Monitoring Overview

A dedicated Monitoring VM at `172.16.20.16` runs the full PLG stack (Prometheus, Loki, Grafana) as a Swarm worker. Keeping monitoring on a separate failure domain means the stack survives a Services VM outage — the most important property for an observability system. All storage is local to the Monitoring VM; log data is ephemeral.

## Monitoring VM

| Property | Value |
|---|---|
| IP | `172.16.20.16` |
| Hostname | `monitoring` |
| Swarm role | Worker |
| vCPU | 2 |
| RAM | 4 GB |
| Local disk | 40 GB |
| Provisioned by | Packer → OpenTofu → Ansible (same pipeline as all other VMs) |

The VM is added to the Swarm as a worker. Grafana is exposed via Traefik on Services VM (`.13`) over the Swarm overlay network — no second Traefik instance required.

**Domain:** `grafana.blackcats.cc`

## Stack Components

All four components run as a single Swarm stack pinned to the Monitoring VM via placement constraints.

| Component | Purpose | Storage |
|---|---|---|
| Prometheus | Metrics scrape + time-series DB | Local named volume — 15-day retention |
| Loki | Log aggregation | Local named volume — 30-day retention (ephemeral) |
| Grafana | Dashboards + alerting rules | Local named volume |
| cAdvisor | Container metrics | No storage — Swarm global service, one per node |

### Volumes

| Volume | Mount path | Approximate size |
|---|---|---|
| `prometheus-data` | `/prometheus` | ~10 GB at homelab scale |
| `loki-data` | `/loki` | ~20 GB headroom |
| `grafana-data` | `/var/lib/grafana` | Dashboards, alert rules, data source config |

All volumes are local to the Monitoring VM — no TrueNAS NFS mounts. Log data is intentionally ephemeral.
