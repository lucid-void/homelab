# Plex

**Read before editing:** `kubernetes/apps/media/plex/`

## Current state

`media` namespace, `replicas: 1`, `lscr.io/linuxserver/plex`. Web access is via
HTTPRoute; direct/GDM access is via a dedicated `pool-b` LoadBalancer at
`172.16.20.51:32400` (`ADVERTISE_IP` set to match, IP pinned with
`lbipam.cilium.io/ips`). Transcoding is CPU-only — no GPU device plugin wired in.

## Rules

- **Never let the pool-b IP drift to `.52`** — `172.16.20.52` is the Minecraft
  Velocity proxy's own pinned pool-b address; sharing or swapping breaks direct
  connect/GDM for one of the two services.
- **Pin the full linuxserver.io tag, never the short `X.Y.Z`** — lscr re-points the
  short tag at every rebuild without the upstream version changing, so it reads as a
  version but behaves as a rolling tag: Renovate sees nothing to bump and
  `imagePullPolicy: IfNotPresent` keeps serving the cached digest through a restart.
  Plex specifically needs its own Renovate regex because its tag carries an upstream
  git hash between the build number and the `ls` revision, unlike the other lscr
  images here.

## Verify

```bash
mise exec -- kubectl get deploy plex -n media -o jsonpath='{.spec.replicas}'
mise exec -- kubectl get svc -n media -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.name}{" "}{.status.loadBalancer.ingress[0].ip}{"\n"}{end}'
```
