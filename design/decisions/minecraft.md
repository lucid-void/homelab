# Minecraft

**Read before editing:** `kubernetes/apps/media/minecraft/`, `kubernetes/apps/media/minecraft-proxy/`, `kubernetes/apps/media/minecraft-backup/`

## Current state

Two Paper servers in `media`, both `app-template`, both `TYPE=PAPER`: **matcha** (with
plugins) and **vanilla** (plugin-free). Image `itzg/minecraft-server`, tag and
`VERSION` pinned per HelmRelease — Minecraft uses **calendar versioning**, not
`1.21.x`. One pod each, three containers: server + `itzg/mc-backup` +
`filebrowser`. World data lives on `openebs-hostpath`, never NFS.

Backups are quiesced snapshots: the `mc-backup` sidecar does RCON
`save-off`/`save-all`/`save-on` → tarball → the RWX `mc-backups` PVC every 2h, and the
`minecraft-backup` CronJob (07:00) restics that to Filen.

Routing is raw TCP, not the Gateway: **Velocity** (Service `minecraft-proxy`, image
`itzg/mc-proxy`, pinned to a **3.x** build) owns `172.16.20.52:25565` on its own
pool-b address (`lbipam.cilium.io/ips`) and selects the backend from the handshake
hostname via `[forced-hosts]`. Backends run `ONLINE_MODE=FALSE`, trusting Velocity's
signed modern forwarding (`velocity-secret`); Services stay ClusterIP-only.
`proxies.velocity.*` is applied via itzg `PATCH_DEFINITIONS` as a patch against
`paper-global.yml`, not a mounted replacement.

Chat is bridged across servers, not merged by Velocity: `quickchatv2` relays
chat/`/msg`/staffchat/join-leave through `minecraft-valkey` (`emptyDir`, `--save ""` +
`appendonly no`), required on **both** servers.

`filebrowser` (`{server}-files.blackcats.cc`) is for datapacks/world imports/config
edits, local auth from `minecraft-secret` — not SSO. Each server exposes
three Services: `{name}-app` 25565, `{name}-files` 8081, `{name}-map` 8080 (squaremap
binds `0.0.0.0:8080` from inside the JVM, so filebrowser moved to 8081).

Sizing: `MEMORY=6G`, container memory limit `9Gi`. `terminationGracePeriodSeconds: 120`
+ `STOP_DURATION: 90` (the 30s default SIGKILLs the JVM mid-save).

## Rules

- **Bump the `-javaNN` image variant alongside `VERSION`** — it must be ≥ the jar's
  target Java. MC 26.2 is class file 69.0 (Java 25); `-java21` (class file 65.0)
  crash-loops on `UnsupportedClassVersionError` before startup.
- **Keep world data on `openebs-hostpath`, never NFS** — Anvil region files are random
  small I/O, and a blocked tick loop shows as TPS drop; `hard` NFS mounts (required to
  avoid silent corruption) turn a NAS stall into a wedged JVM. Node-pinning costs
  nothing — all 3 CPs are VMs on one Proxmox host.
- **Never rsync a live world** — a live rsync yields torn region files that only fail
  at restore time; quiesce with RCON `save-off`/`save-all`/`save-on` first — what
  `mc-backup` does.
- **Never share a pool-b LoadBalancer IP across two Services via
  `lbipam.cilium.io/sharing-key`** — Velocity shared `172.16.20.51` with Plex this way
  before its own `172.16.20.52`. Cilium's L2 announcer holds one `Lease` per
  Service and elects leaders independently, so a shared IP is announced by both
  nodes; with `externalTrafficPolicy: Cluster` the receiving node SNATs locally; an
  ARP flip mid-session re-SNATs on the other, and the backend sends `RST` on an
  unknown 4-tuple. Tell: cross-VLAN clients break constantly, same-segment ones are
  fine; `RST` carries the *pod's* TTL (mimics the game server); the log shows only a
  generic `lost connection: Disconnected`, seconds late.
- **A new server needs a HelmRelease, two `velocity-config.yml` lines
  (`[servers]` + `[forced-hosts]`), and an `external-dns` hostname-list entry on the
  proxy Service** — the last is why external-dns has `service` in `sources`.
- **Pin Velocity to a `3.x` build, not `4.x`** — 4.x changes the `velocity.toml`
  schema; bump `VELOCITY_VERSION` and `config-version` together.
- **A `PATCH_DEFINITIONS` entry pointed at a *directory* wants a bare
  `PatchDefinition` (`file`/`ops`/`file-format`), not the `{"patches":[…]}` patch-set**
  (valid only naming a single file) — wrongly shaped, mc-image-helper fails to parse
  and the server crash-loops before Paper starts (symptom: `2/3` containers ready,
  sidecars stay up).
- **List `LuckPerms` and `PlaceholderAPI` explicitly in `MODRINTH_PROJECTS`** —
  QuickChat hard-depends on both in its `plugin.yml`, but not in Modrinth's metadata,
  so `MODRINTH_DOWNLOAD_DEPENDENCIES: required` skips them: the jar loads
  and fails with `UnknownDependencyException`, server *healthy*, chat un-bridged.
- **QuickChat's Redis config lives in `plugins/Quickchat/redis.yml`, not
  `config.yml`** (`storage:` there only offers YAML/MYSQL); `redis.server-id` must be
  unique per server, set via `CFG_QUICKCHAT_SERVER_ID`. No public source or wiki —
  schema only readable off a running server, nothing to patch on a rebuilt PVC's
  first boot.
- **Never upload jars by hand** — `TYPE`+`VERSION` fetch the server, and
  `MODRINTH_PROJECTS`/`SPIGET_RESOURCES`/`PLUGINS` declare plugins in git so they
  survive PVC loss.
- **Never hand-edit `server.properties` via filebrowser** — unmanaged PVC state read
  once at boot: an edit does nothing until the pod cycles, and drifts silently
  otherwise. Drive every property via itzg env (`LEVEL`,
  `RESOURCE_PACK`/`RESOURCE_PACK_SHA1`/`RESOURCE_PACK_ENFORCE`,
  `DATAPACKS`/`REMOVE_OLD_DATAPACKS`, `OPS`, `WHITELIST`) — itzg rewrites the file
  each boot and Flux rolls the pod when the value changes.
- **`RESOURCE_PACK_SHA1` must match the file byte-for-byte** — a wrong value fails
  `server_resource_pack` and disconnects *every* joining client with "Unexpected
  error during configuration," while the server and mc-monitor report healthy
  (status pings never reach the configuration phase). `RESOURCE_PACK_ID` is optional
  (a stable v3 UUID derived from the URL); a zip can be both datapack and resource
  pack (`data/` + `assets/`) regardless of the Modrinth `loaders` label.
- **Decouple readiness both ways between the game and filebrowser Services** — the
  `files` container has no readiness probe (else a filebrowser hiccup drops the game
  endpoint and kicks players), and `{server}-files` sets
  `publishNotReadyAddresses: true` (else a crash-looping server strips the
  filebrowser endpoints exactly when needed to fix it).
- **`strategy: Recreate` is mandatory** — an RWO volume plus two JVMs on one world
  means corruption under a rolling update.
- **Size the container memory limit at `MEMORY` (heap) plus ~3Gi, not ~1.5Gi** — itzg
  sets `-Xms` as well as `-Xmx`: the heap commits fully and never returns it; non-heap
  overhead runs ~1.2Gi. Undersizing puts steady-state RSS at ~90% of the
  limit by construction, so `MinecraftMemoryNearLimit` (0.9) fires permanently instead
  of warning — the metric is RSS, not reclaimable page cache.
- **Check any new plugin's port against the sidecars before installing it** — plugins
  share the pod network namespace — how squaremap's `0.0.0.0:8080` collided with
  filebrowser's default port.
- **Regenerate the world after adding a worldgen datapack, and run Chunky
  pregeneration after it, never before** — a datapack only affects chunks generated
  after it loads, so adding one to existing terrain leaves a seam.
- **Install `jeirecipefix` on both servers for JEI/REI/EMI to work** — since
  **MC 1.21.2** the server sends recipe-book *displays* only, for
  already-unlocked recipes (JEI is a client mod; Paper takes plugins, so server-side
  install is impossible). Matters most on matcha — datapack recipes never reach the
  client; `client-recipe-fix` restores only vanilla ones. **Do not substitute
  `jei-recipe-bridge`** — it stops at `26.1.2`, hitting the loader trap below.
- **Verify a Modrinth project's `loaders` includes `paper` and `game_versions`
  includes the pinned `VERSION` before relying on it**
  (`api.modrinth.com/v2/project/<slug>`) — a resolving slug doesn't guarantee a Paper
  build; an unresolvable one fails startup.

## Verify

```bash
mise exec -- kubectl get pods -n media -l app.kubernetes.io/instance=matcha
mise exec -- kubectl get svc -n media minecraft-proxy -o jsonpath='{.status.loadBalancer}'
```
