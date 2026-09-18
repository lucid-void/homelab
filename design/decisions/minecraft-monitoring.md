# Minecraft monitoring

**Read before editing:** `kubernetes/apps/monitoring/minecraft-monitoring/`, `kubernetes/apps/media/minecraft-events/`, `kubernetes/apps/media/minecraft/app/world-size-configmap.yml`

## Current state

`monitoring/minecraft-monitoring`: **mc-monitor** (`itzg/mc-monitor`,
`export-for-prometheus`) runs in `monitoring`, pinging both Minecraft servers
cross-namespace via `EXPORT_SERVERS` and exporting `minecraft_status_healthy`,
`minecraft_status_players_online_count`, `minecraft_status_players_max_count` and
`minecraft_status_response_time_seconds` — protocol-level, not plugin-level, so it is
identical for Paper and vanilla and unaffected by MC version upgrades. It also pings
`minecraft-proxy` (the Velocity proxy, on its own dedicated pool-b address
`172.16.20.52`) as a third target, since Velocity exports no Prometheus metrics of its
own; pinging the backends directly separates a proxy fault from a backend fault
(`MinecraftServerDown` excludes the proxy target; `MinecraftProxyDown` matches only
it).

World data lives on `openebs-hostpath`, never NFS — so `kubelet_volume_stats_*` for
these claims reports the node filesystem, not the world, which is why a dedicated
**`world-size` sidecar** exists in each game pod (`world-size-configmap.yml`, a
stdlib-only Python exporter on `:9109`, scraped via the `matcha-metrics`/
`vanilla-metrics` Services), exporting `minecraft_world_size_bytes{server,world}` and
`minecraft_server_data_size_bytes{server}`. It counts `st_blocks*512` to match `du` on
sparse region files and rescans on a `300s` timer so a scrape never blocks on disk.
Alerts are `MinecraftWorldGrowingFast` (rate), `MinecraftWorldLarge` (absolute) and
`MinecraftWorldSizeExporterStale` (a dead exporter otherwise reads as "no growth"). The
sidecar is liveness-only and `publishNotReadyAddresses: true`, so it can never gate
pod readiness and disconnect players. `openebs-hostpath` enforces no quota and cannot
expand in place, so the `20Gi` in the PVC spec is advisory, not a hard ceiling.

Gatus additionally runs `tcp://` checks against the public hostnames, exercising the
full DNS → `.51` → router → backend path that the in-cluster scrape bypasses.

Player join/leave notifications are a separate component, `media/minecraft-events`: a
Deployment that streams the Velocity proxy pod's log via `kubectl logs --follow`
(RBAC scoped to `pods`/`pods/log` in one namespace) and posts to Gotify. Uses the
`backup-tools` image (already carries bash+curl+kubectl).

## Rules

- **There is no TPS metric, and no maintained exporter provides one** — a server-side
  plugin is the only possible source, so tick health is inferred instead from
  `minecraft_status_response_time_seconds` (answered on the main thread) plus
  container CPU.
- **Never source a per-PVC storage alert for these worlds from `kubelet_volume_stats`**
  — it reports the node/NAS filesystem, not the claim, because world data lives on
  `openebs-hostpath`, never NFS. Use the `world-size` sidecar's
  `minecraft_world_size_bytes` instead.
- **Watch the Velocity proxy log for join/leave events, not the two backends or a
  plugin** — tailing both backends double-counts `/server` switches and needs a
  container inside the game pod, where a non-ready container strips the pod's Service
  endpoints and drops players; a join-webhook plugin is another `MODRINTH_PROJECTS`
  entry subject to the Modrinth loader trap (verify `loaders`/`game_versions` before
  relying on a project); and `minecraft_status_players_online_count` is a 60s poll
  with no player names, where an Alertmanager alert stays firing until the last player
  leaves rather than firing once per event.
- **Pair Velocity's `[connected player]` and `[server connection]` log lines to tell
  "joined" from "switched"** — Velocity logs
  `[connected player] Name (/ip:port) has connected` (the true session boundary)
  followed by `[server connection] Name -> matcha has connected` (which backend; also
  fires on a `/server` switch), and the script holds the first until the second names
  the server. Names are matched against the MC username charset
  (`[A-Za-z0-9_]{1,16}`) before JSON interpolation, so an unexpected log line can only
  fail to match, never corrupt the body.
- **Recycle the `kubectl logs --follow` stream hourly and read a heartbeat file for
  liveness** — the API server can silently drop a follow connection, leaving the
  container Running with no output forever; `--tail=0` on reconnect replays nothing,
  and an idle server emits no log lines for hours, so output cannot be the health
  signal.
- **Read the `minecraft-events-gotify-secret` token from an optional mounted secret
  volume, re-read per event — not `envFrom`** — the house pattern (`envFrom` plus
  `reloader.stakater.com/auto`) failed silently here: env vars are fixed at pod start,
  and the Reloader annotation only works from the Deployment's own
  `metadata.annotations`. The mounted-volume approach self-heals once
  `gotify-bootstrap` runs or rotates the token, with no restart — also why this
  Kustomization deliberately does **not** `dependsOn: gotify-bootstrap`.

## Verify

```bash
mise exec -- kubectl get vmservicescrape -n monitoring minecraft-monitoring
mise exec -- kubectl get deploy minecraft-events -n media
```
