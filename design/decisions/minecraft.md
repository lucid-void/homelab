# Minecraft

**Read before editing:** `kubernetes/apps/media/minecraft*/`

## Current state

Two Paper servers in `media`, both `app-template`, both `TYPE=PAPER`: **matcha** (with
plugins) and **vanilla** (plugin-free). Image `itzg/minecraft-server`, tag and
`VERSION` pinned per HelmRelease — Minecraft uses **calendar versioning**, so do not
assume `1.21.x`. One pod each, three containers: server + `itzg/mc-backup` +
`filebrowser`.

World data lives on `openebs-hostpath`, never NFS. Backups are quiesced snapshots: the
`mc-backup` sidecar does RCON `save-off`/`save-all`/`save-on` → tarball → the RWX
`mc-backups` PVC every 2h, and the `minecraft-backup` CronJob (07:00) restics that to
Filen.

Routing is raw TCP, not the Gateway: **Velocity** (Service `minecraft-proxy`, image
`itzg/mc-proxy`, pinned to a **3.x** build) owns `172.16.20.52:25565` on its own
pool-b address (`lbipam.cilium.io/ips`) and selects the backend from the handshake
hostname via `[forced-hosts]`. Backends run `ONLINE_MODE=FALSE` and trust Velocity's
signed modern forwarding (`velocity-secret`); backend Services stay ClusterIP-only.
`proxies.velocity.*` is applied via itzg `PATCH_DEFINITIONS` as a **patch** against
`paper-global.yml`, not a mounted replacement of the file.

Chat is bridged across servers, not merged by Velocity itself: `quickchatv2` relays
chat/`/msg`/staffchat/join-leave through `minecraft-valkey` (`emptyDir`, `--save ""` +
`appendonly no`) and must be installed on **both** servers — on only one, the bridge is
one-way.

`filebrowser` (`{server}-files.blackcats.cc`) is for datapacks/world imports/config
edits only, with local auth from `minecraft-secret` — not Zitadel. Each server exposes
three Services: `{name}-app` 25565, `{name}-files` 8081, `{name}-map` 8080 (squaremap's
internal webserver binds `0.0.0.0:8080` from inside the JVM, so filebrowser was moved
off it to 8081).

Sizing: `MEMORY=6G`, container memory limit `9Gi`. `terminationGracePeriodSeconds: 120`
+ `STOP_DURATION: 90` (the 30s default SIGKILLs the JVM mid-save).

Player join/leave notifications are a separate component, `media/minecraft-events`: a
Deployment that streams the Velocity pod's log via `kubectl logs --follow` and posts to
Gotify.

## Rules

- **Bump the `-javaNN` image variant alongside `VERSION`** — it must be ≥ the Java the
  server jar targets. MC 26.2 is class file 69.0 (Java 25); `-java21` (class file 65.0)
  crash-loops on `UnsupportedClassVersionError` before startup.
- **Keep world data on `openebs-hostpath`, never NFS** — Anvil region files are random
  small I/O in large files, and a chunk load blocking the single-threaded tick loop
  shows up directly as TPS drop; `hard` NFS mounts (required to avoid silent
  corruption) turn a NAS stall into a wedged JVM. Node-pinning costs nothing here — all
  3 CPs are VMs on one Proxmox host.
- **Never rsync a live world** — a live rsync yields torn region files that only fail
  at restore time; quiesce with RCON `save-off`/`save-all`/`save-on` first, which is
  what `mc-backup` does.
- **Never share a pool-b LoadBalancer IP across two Services via
  `lbipam.cilium.io/sharing-key`** — Cilium's L2 announcer holds one `Lease` per
  Service and elects each leader independently, so a shared IP gets announced by two
  nodes at once. With `externalTrafficPolicy: Cluster` the receiving node SNATs
  locally, so an ARP flip mid-session re-SNATs on the other node and the backend sees
  an unknown 4-tuple and sends `RST`. Tell: cross-VLAN clients break constantly while
  same-segment clients are fine, the `RST` carries the *pod's* TTL (looks like the
  game server did it), and the server log only shows a generic
  `lost connection: Disconnected` several seconds late.
- **A new server needs a HelmRelease, two `velocity-config.yml` lines
  (`[servers]` + `[forced-hosts]`), and a name in the proxy Service's `external-dns`
  hostname list, together** — the last is why external-dns has `service` in `sources`.
- **Pin Velocity to a `3.x` build, not `4.x`** — 4.x changes the `velocity.toml`
  schema; bump `VELOCITY_VERSION` and `config-version` together.
- **A `PATCH_DEFINITIONS` entry pointed at a *directory* wants a bare
  `PatchDefinition` (`file`/`ops`/`file-format`), not the `{"patches":[…]}` patch-set
  wrapper** (only valid naming a single file) — wrong shape is not a silent no-op:
  mc-image-helper fails to parse and the server crash-loops before Paper starts
  (symptom: `2/3` containers ready, since the sidecars stay up).
- **List `LuckPerms` and `PlaceholderAPI` explicitly in `MODRINTH_PROJECTS`** —
  QuickChat hard-depends on both in its `plugin.yml`, but not in Modrinth's dependency
  metadata, so `MODRINTH_DOWNLOAD_DEPENDENCIES: required` does not fetch them: the jar
  loads and fails with `UnknownDependencyException`, and the server starts *healthy*
  with chat silently un-bridged.
- **QuickChat's Redis config lives in `plugins/Quickchat/redis.yml`, not
  `config.yml`** (whose `storage:` block only offers YAML/MYSQL); `redis.server-id`
  must be unique per server, set via `CFG_QUICKCHAT_SERVER_ID` per HelmRelease.
  QuickChat has no public source or wiki, so this schema is only readable off a
  running server — and it has nothing to patch on a rebuilt PVC's first boot.
- **Never upload jars by hand** — `TYPE`+`VERSION` fetch the server, and
  `MODRINTH_PROJECTS`/`SPIGET_RESOURCES`/`PLUGINS` declare plugins in git so they
  survive PVC loss.
- **Never hand-edit `server.properties` via filebrowser** — it is unmanaged PVC state
  read once at boot, so an edit does nothing until the pod cycles, and it drifts
  silently otherwise. Drive every property from itzg env instead: `LEVEL`,
  `RESOURCE_PACK`/`RESOURCE_PACK_SHA1`/`RESOURCE_PACK_ENFORCE`,
  `DATAPACKS`/`REMOVE_OLD_DATAPACKS`, `OPS`, `WHITELIST` — itzg rewrites the file each
  boot and Flux rolls the pod when the value changes.
- **`RESOURCE_PACK_SHA1` must match the file byte-for-byte** — a wrong value fails the
  `server_resource_pack` configuration task and disconnects *every* joining client with
  "Unexpected error during configuration," while the server and mc-monitor both still
  report healthy, since status pings never reach the configuration phase.
  `RESOURCE_PACK_ID` is optional (derived as a stable v3 UUID from the URL); a single
  zip can be both datapack and resource pack (`data/` + `assets/`) regardless of what
  Modrinth's `loaders` label says.
- **Decouple readiness in both directions between the game and filebrowser
  Services** — the `files` container carries no readiness probe (else a file-manager
  hiccup drops the game Service endpoint and kicks players), and `{server}-files` sets
  `publishNotReadyAddresses: true` (else a crash-looping game server strips the
  filebrowser endpoints, taking it offline exactly when needed to fix the crash).
  Fixing only one direction leaves the other broken.
- **`strategy: Recreate` is mandatory** — an RWO volume plus two JVMs on one world
  means corruption under a rolling update.
- **Size the container memory limit at `MEMORY` (heap) plus ~3Gi, not ~1.5Gi** — itzg
  sets `-Xms` as well as `-Xmx` from `MEMORY`, so the heap commits its full size and
  never returns it, and non-heap overhead (metaspace/GC/direct buffers) runs ~1.2Gi.
  Undersizing puts steady-state RSS at ~90% of the limit by construction, so
  `MinecraftMemoryNearLimit` (threshold 0.9) fires permanently rather than warning of
  anything — the metric is RSS, not reclaimable page cache, so the headroom is real.
- **Check any new plugin's port against the sidecars before installing it** — plugins
  share the pod network namespace, which is how squaremap's `0.0.0.0:8080` collided
  with filebrowser's default port.
- **Regenerate the world after adding a worldgen datapack, and run Chunky
  pregeneration after the datapack, never before** — a datapack only affects chunks
  generated after it loads, so adding one to existing terrain leaves a hard seam.
- **Install `jeirecipefix` on both servers for any recipe viewer (JEI/REI/EMI) to
  work** — since **MC 1.21.2** the server sends recipe-book *displays* only, for
  already-unlocked recipes, not full recipe data (a vanilla protocol change, not a
  Paper bug; JEI's own "install server-side" advice is impossible because JEI is a mod
  and Paper takes plugins). Matters most on matcha, whose custom recipes are datapack
  recipes and never reach the client — the client-side `client-recipe-fix` workaround
  only restores vanilla recipes. **Do not substitute `jei-recipe-bridge`** — it stops
  at `26.1.2` and hits the loader trap below on later versions.
- **Verify a Modrinth project's `loaders` includes `paper` and `game_versions`
  includes the pinned `VERSION` before relying on it**
  (`api.modrinth.com/v2/project/<slug>`) — a slug resolving on Modrinth does not mean
  a Paper build exists; an unresolvable project fails startup.
- **Watch the Velocity proxy log for join/leave events, not the two backends or a
  plugin** — tailing both backends double-counts `/server` switches and needs a
  container inside the game pod (stripping pod readiness and dropping players, the
  same trap that forces the `files` sidecar to have no readiness probe); a
  join-webhook plugin is another `MODRINTH_PROJECTS` entry subject to the loader trap
  above; and `minecraft_status_players_online_count` is a 60s poll with no player
  names, where an Alertmanager alert stays firing until the last player leaves rather
  than firing once per event. Pair Velocity's
  `[connected player] Name (/ip:port) has connected` (the true session boundary) with
  the following `[server connection] Name -> matcha has connected` (which backend;
  also fires on a `/server` switch) to tell "joined" from "switched" — the script
  holds the first until the second names the server. Names are matched against the MC
  username charset (`[A-Za-z0-9_]{1,16}`) before JSON interpolation, so an unexpected
  log line can only fail to match, never corrupt the body.
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
  Kustomization deliberately does **not** `dependsOn: gotify-bootstrap`. Uses the
  `backup-tools` image (already carries bash+curl+kubectl).

## Monitoring

`monitoring/minecraft-monitoring`: **mc-monitor** (`itzg/mc-monitor`,
`export-for-prometheus`) runs in `monitoring`, pinging both servers cross-namespace via
`EXPORT_SERVERS` and exporting `minecraft_status_healthy`,
`minecraft_status_players_online_count`, `minecraft_status_players_max_count` and
`minecraft_status_response_time_seconds` — protocol-level, not plugin-level, so it is
identical for Paper and vanilla and unaffected by MC version upgrades. It also pings
`minecraft-proxy` as a third target, since Velocity exports no Prometheus metrics of
its own; pinging the backends directly separates a proxy fault from a backend fault
(`MinecraftServerDown` excludes the proxy target; `MinecraftProxyDown` matches only
it).

**There is no TPS metric, and no maintained exporter provides one** — a server-side
plugin is the only possible source, so tick health is inferred instead from
`minecraft_status_response_time_seconds` (answered on the main thread) plus container
CPU.

World size comes from a **`world-size` sidecar** in each game pod
(`world-size-configmap.yml`, a stdlib-only Python exporter on `:9109`, scraped via the
`matcha-metrics`/`vanilla-metrics` Services), exporting
`minecraft_world_size_bytes{server,world}` and
`minecraft_server_data_size_bytes{server}`. It counts `st_blocks*512` to match `du` on
sparse region files and rescans on a `300s` timer so a scrape never blocks on disk.
Alerts are `MinecraftWorldGrowingFast` (rate), `MinecraftWorldLarge` (absolute) and
`MinecraftWorldSizeExporterStale` (a dead exporter otherwise reads as "no growth"). The
sidecar is liveness-only and `publishNotReadyAddresses: true`, so it can never gate
pod readiness and disconnect players.

- **Never source a per-PVC storage alert for these worlds from `kubelet_volume_stats`**
  — it reports the node/NAS filesystem, not the claim, and `openebs-hostpath` enforces
  no quota and cannot expand in place, so the `20Gi` in the PVC spec is advisory. Use
  the `world-size` sidecar's `minecraft_world_size_bytes` instead.

Gatus additionally runs `tcp://` checks against the public hostnames, exercising the
full DNS → `.51` → router → backend path that the in-cluster scrape bypasses.

## Verify

```bash
mise exec -- kubectl get pods -n media -l app.kubernetes.io/instance=matcha
mise exec -- kubectl get svc -n media minecraft-proxy -o jsonpath='{.status.loadBalancer}'
mise exec -- kubectl get vmservicescrape -n monitoring minecraft-monitoring
```
