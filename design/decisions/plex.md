# Plex — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## Deployment and direct access

Runs in k8s (`media` namespace, `replicas: 1`, `lscr.io/linuxserver/plex`). Web via
HTTPRoute; direct/GDM via a `pool-b` LoadBalancer at `172.16.20.51:32400`
(`ADVERTISE_IP` set accordingly, so the IP is pinned with `lbipam.cilium.io/ips` — it
must not drift to `.52`). Transcoding is CPU-only today — no GPU device plugin wired
in yet.

