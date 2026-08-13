# Minecraft — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## Servers, proxy, storage, backups, plugins

Two servers in `media`, both `app-template` and both **`TYPE=PAPER`**: **matcha** (with
plugins) and **vanilla** (plugin-free — the name is about content, not engine).
`itzg/minecraft-server` (image tag and `VERSION` pinned in each HelmRelease —
Minecraft moved to **calendar versioning**, so do not assume `1.21.x`).

**The `-javaNN` image variant must be ≥ the Java the server jar targets**: MC 26.2 is
class file 69.0 (Java 25), so `-java21` (class file 65.0) crash-looped on
`UnsupportedClassVersionError` before startup — bump the variant alongside `VERSION`.
One pod each, three containers: server + `itzg/mc-backup` + `filebrowser`.

**World data on `openebs-hostpath`, never NFS** — Anvil region files are random small
I/O in large files, and a chunk load blocking the single-threaded tick loop is directly
visible as TPS drop; the Synology is HDD-backed and shared with the media stack, and
`hard` NFS mounts turn a NAS stall into a wedged JVM. Node-pinning is free here (all 3
CPs are VMs on one Proxmox host).

**Backups are quiesced snapshots, not continuous sync** — rsyncing a live world
yields torn region files that only fail at restore; the mc-backup sidecar does RCON
`save-off`/`save-all`/`save-on` → tarball → RWX `mc-backups` PVC every 2h, and
`minecraft-backup` (07:00) restics that to Filen.

**Routing is raw TCP, not the Gateway**: **Velocity** (`minecraft-proxy`,
`itzg/mc-proxy`, Velocity pinned to a **3.x** build — *not* 4.x, which changes the
`velocity.toml` schema; bump `VELOCITY_VERSION` and `config-version` together) owns
`172.16.20.52:25565` — **its own pool-b address**, pinned with `lbipam.cilium.io/ips`
— and selects the backend from the handshake hostname via `[forced-hosts]` (replaced
`mc-router` 2026-08-02). It used to *share* `.51` with Plex via
`lbipam.cilium.io/sharing-key: pool-b-shared`; **never do that again**. Cilium's L2
announcer keeps one `Lease` per *Service* and elects each leader independently, so the
shared IP was announced by two nodes at once (mc-router→cp-3, plex-direct→cp-2) and
both answered ARP for it. With `externalTrafficPolicy: Cluster` the receiving node
SNATs the flow locally, so when a client's ARP entry flipped mid-session its packets
were re-SNATed on the other node, hit the backend pod as an unknown 4-tuple, and got
`RST` ~2s after joining. Brutal to diagnose: short HTTP bursts to the same IP succeed
(they finish before a flip), clients on the nodes' own L2 segment are fine (ARP entry
stays pinned) while anything routed in from another VLAN breaks constantly, and the
`RST` carries the *pod's* TTL so it looks like the game server did it. Server logs show
only a generic `lost connection: Disconnected`, ~7s late, because the server finds out
when a keepalive write fails — the client-side `latest.log` timestamp is the real
event time. A new server = HelmRelease + two lines in `velocity-config.yml`
(`[servers]` + `[forced-hosts]`) + a name in the proxy Service's `external-dns`
hostname list — which is why external-dns has `service` in `sources`.

**Backends run `ONLINE_MODE=FALSE`** and trust Velocity's *modern* forwarding signed
with `velocity-secret`; Paper rejects unsigned logins so they still can't be joined
directly — but that makes the secret load-bearing and the backend Services must stay
ClusterIP-only. The `proxies.velocity.*` keys are applied via itzg `PATCH_DEFINITIONS`
(a **patch**, not a mounted `paper-global.yml` — replacing the whole file would reset
every other Paper setting to defaults).

**Velocity does NOT merge chat**: `quickchatv2` on *both* servers relays
chat/`/msg`/staffchat/join-leave through `minecraft-valkey` (in-memory, `--save ""` +
`appendonly no`); install it on one server only and the bridge is one-way. Three traps,
all hit in practice: (1) **`PATCH_DEFINITIONS` pointing at a *directory* wants a bare
PatchDefinition** (`file`/`ops`/`file-format`) — the `{"patches":[…]}` *patch-set*
wrapper is only valid when it names a single file, and getting it wrong is **not** a
silent no-op: mc-image-helper fails to parse, the init script exits non-zero and the
server crash-loops *before Paper starts* (symptom: `2/3` containers ready, since the
backup and filebrowser sidecars stay up, and `paper-global.yml` still showing
`velocity.enabled: false`). (2) **QuickChat hard-depends on LuckPerms +
PlaceholderAPI**, declared in its `plugin.yml` but **not** in Modrinth's dependency
metadata — so `MODRINTH_DOWNLOAD_DEPENDENCIES: required` does not fetch them, the jar
downloads, then fails to load with `UnknownDependencyException` and the server starts
*healthy* with chat silently un-bridged. Both must be listed explicitly in
`MODRINTH_PROJECTS`. (3) Redis config lives in **`plugins/Quickchat/redis.yml`**, not
`config.yml` (whose `storage:` block only offers YAML/MYSQL); `redis.server-id` **must
be unique per server**, so it comes from `CFG_QUICKCHAT_SERVER_ID` per HelmRelease.
QuickChat publishes no source or wiki, so that schema can only be read off a running
server — which also means the patch targets a plugin-generated file and has nothing
to patch on a rebuilt PVC's first boot.

**Never upload jars**: `TYPE`+`VERSION` fetch the server,
`MODRINTH_PROJECTS`/`SPIGET_RESOURCES`/`PLUGINS` declare plugins in git so they survive
PVC loss.

**`server.properties` is unmanaged PVC state read once at boot** — hand-editing it
via filebrowser appears to do nothing until the pod cycles (a cleared `resource-pack`
stayed live for 10h), and it silently drifts (a bad `level-name` pointed at a
half-migrated stub and crash-looped the server). Drive every property from itzg env
instead — `LEVEL`, `RESOURCE_PACK`/`RESOURCE_PACK_SHA1`/`RESOURCE_PACK_ENFORCE`,
`DATAPACKS`/`REMOVE_OLD_DATAPACKS`, `OPS`, `WHITELIST` — so itzg rewrites the file
each boot and Flux rolls the pod when the value changes.

**`RESOURCE_PACK_SHA1` must match the file byte-for-byte**: a wrong value (the URL
pasted in) fails the `server_resource_pack` configuration task and disconnects *every*
joining client with "Unexpected error during configuration" — the server still starts
and looks healthy, and mc-monitor still reports it up, because status pings never reach
the configuration phase. `RESOURCE_PACK_ID` is optional (server derives a stable v3
UUID from the URL). Note a single zip can be **both** datapack and resource pack
(`data/` + `assets/`), regardless of how Modrinth labels its `loaders`.

**`terminationGracePeriodSeconds: 120` + `STOP_DURATION: 90`**: the 30s default
SIGKILLs the JVM mid-save. filebrowser (`{server}-files.blackcats.cc`) is for
datapacks/world imports/config edits only; local auth from `minecraft-secret`, **not**
Zitadel.

**The two Services select the same pod and pod readiness is all-or-nothing, so both
directions need decoupling** (learned the hard way): the `files` container has **no
readiness probe** (else a file-manager hiccup drops the game Service endpoint and kicks
players), *and* the `{server}-files` Service sets **`publishNotReadyAddresses: true`**
(else a crash-looping game server strips the filebrowser endpoints — taking the file
manager offline exactly when it's needed to fix the bad config that caused the crash).
Fixing only one direction leaves the other live. `strategy: Recreate` is mandatory (RWO
volume + two JVMs on one world = corruption).

**Container memory limit must exceed `MEMORY` (heap) by ~3Gi**, not the ~1.5Gi
originally assumed: itzg sets **`-Xms` as well as `-Xmx`** from `MEMORY`, so the heap
commits to its full size and never returns it, and measured non-heap overhead
(metaspace/GC/direct buffers) is ~1.2Gi. At `MEMORY=6G` on an 8Gi limit that puts
*steady-state* RSS at ~7.2Gi = **90% of the limit by construction**, so
`MinecraftMemoryNearLimit` (threshold 0.9) fires permanently once the heap fills rather
than warning of anything — vanilla tripped it 2026-08-04 with matcha at 86% on the
same trajectory. Both are 6G/**9Gi**. The metric is RSS, not reclaimable page cache, so
the headroom is real. Keep the two servers identical so one leads the other into the
alert.

**Plugin ports share the pod network namespace**: squaremap's internal webserver binds
`0.0.0.0:8080` from inside the server JVM, so filebrowser was moved to **8081** — any
new plugin that opens a port must be checked against the sidecars. Each server exposes
three Services (`{name}-app` 25565, `{name}-files` 8081, `{name}-map` 8080).

**Worldgen datapacks only affect chunks generated after they load** — Terralith on
vanilla's existing `world` leaves a hard seam against pre-existing terrain; regenerate
the world for a clean result, and always run Chunky pregeneration *after* the datapack,
never before.

**Modrinth loader trap:** a slug resolving on Modrinth does *not* mean a Paper build
exists — `spark` publishes only fabric/forge/neoforge/quilt and was removed after
being added by mistake; always check `loaders` (curl
`api.modrinth.com/v2/project/<slug>`) includes `paper` **and** `game_versions` includes
the pinned `VERSION`, since an unresolvable project fails startup.

**Monitoring** lives in `monitoring/minecraft-monitoring` (see the Monitoring stack
row).

