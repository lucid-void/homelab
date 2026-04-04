---
tags:
  - stack
  - services
  - swarm
---

# Services

What runs where — every service pinned to its host via Swarm placement constraints (`node.hostname == <name>`).

## Service Placement

=== "Services VM (.13)"

    Traefik, Paperless, paperless-broker (Valkey), Gotenberg, Tika, Immich, Immich ML (CPU), immich-valkey, reactive-resume, reactive-resume-browserless, Homebox, IT-Tools, Authentik, Authentik-worker, authentik-valkey, Authelia, authelia-valkey

=== "Media VM (.12)"

    Plex, Sabnzbd, Sonarr, Radarr, Prowlarr, qBittorrent, FlareSolverr

=== "DGX Spark (.4)"

    Ollama, OpenWebUI, Langfuse, Qdrant, SearXNG, Cortex stack

=== "Monitoring VM (.16)"

    Prometheus, Loki, Grafana, cAdvisor (global), pve_exporter, truenas-exporter, unifi-poller, Gotify, Uptime Kuma

=== "Game VM (.14)"

    Satisfactory server

=== "DNS nodes (.1, .11)"

    Technitium DNS, chrony NTP

!!! note "Netbird and ZeroTier on Game VM"
    These require `--network host` and kernel-level capabilities incompatible with Swarm's ingress mesh. They run as plain `docker compose` stacks outside Swarm.

!!! note "Valkey (Redis replacement)"
    Valkey is used in place of Redis for all cache and broker instances. Valkey instances are co-located with their service on the Services VM and hold no persistent data.

## Overlay Networks

Services are isolated by function — each logical group gets its own encrypted overlay (`--opt encrypted`). Services that need Traefik routing join the `traefik` overlay in addition to their group overlay.

| Overlay | Services | Purpose |
|---|---|---|
| `traefik` | Traefik + every routed service | HTTP routing |
| `media` | Plex, Sonarr, Radarr, Prowlarr, Sabnzbd, qBittorrent, FlareSolverr | *arr interconnect |
| `paperless` | Paperless, paperless-broker (Valkey), Gotenberg, Tika | Document pipeline |
| `immich` | Immich server, ML, immich-valkey | Photo pipeline |
| `reactive-resume` | reactive-resume, browserless | Resume builder |
| `llm` | Ollama, vLLM, OpenWebUI, Langfuse, Qdrant, SearXNG, Cortex stack | AI/ML interconnect |
| `auth` | Authentik, authentik-worker, authentik-valkey, Authelia, authelia-valkey | SSO components |
| `monitoring` | Prometheus, Loki, Grafana, Gotify, Uptime Kuma | PLG stack |

**Standalone services** (homebox, it-tools, freshrss, Gitea) join only the `traefik` overlay for HTTP routing. Database access goes over TCP to TrueNAS on the host network.
