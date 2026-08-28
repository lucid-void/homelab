# Plex — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## Deployment and direct access

Runs in k8s (`media` namespace, `replicas: 1`, `lscr.io/linuxserver/plex`). Web via
HTTPRoute; direct/GDM via a `pool-b` LoadBalancer at `172.16.20.51:32400`
(`ADVERTISE_IP` set accordingly, so the IP is pinned with `lbipam.cilium.io/ips` — it
must not drift to `.52`). Transcoding is CPU-only today — no GPU device plugin wired
in yet.


## Image tag must be the full lscr tag

Pinned as `1.43.3.10896-cb3ebc72d-ls321`, not `1.43.3`. The short tag is
**mutable** — lscr re-points it at every rebuild — so pinning it means Renovate
correctly sees nothing to bump, `image-scan` never runs, and the node's
`IfNotPresent` cache serves the same digest forever (a restart does not
re-pull). That drift went unnoticed until 2026-08-28, by which point the pod had
been 6 lscr builds behind for a week.

Plex needs its own Renovate versioning rule because it is the only lscr image
here whose tag carries an upstream git hash between the build number and the
`ls` revision. Full reasoning, the shapes of the other images, and how to verify
a tag is mutable: [../docs/gitops.md](../docs/gitops.md) → "Image pinning and
mutable upstream tags".
