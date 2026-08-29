# Obsidian Self-hosted LiveSync (CouchDB) — decisions

Do not re-litigate without reason.

## Why a central server rather than Syncthing peer-to-peer

Syncthing's selling point is "no server to run", which is not a saving here — the
cluster is already up 24/7 with Netbird as transport and a restic pipeline. Three
concrete failures decided it:

- **Convergence needs two devices awake at once.** A phone edit sits unreplicated
  until a laptop opens. The standard remedy is an always-on peer, which is a
  central server wearing a P2P hat.
- **Nothing canonical to back up.** Every other app here is snapshotted from a
  cluster PVC by a nightly CronJob. A peer mesh would mean backing up a laptop.
- **Discovery contradicts the network posture.** Syncthing global discovery and
  relays traverse third-party servers; pinning it to Netbird puts it back on
  cluster-adjacent infrastructure anyway.

iOS settles it independently: there is no first-party Syncthing client, and the
third-party wrapper cannot sync reliably in the background.

## Deployment

`obsidian` namespace, `couchdb:3.5.2` via `bjw-s/app-template`, single controller
`app` → Deployment/Service **`couchdb`**, port 5984, `obsidian.blackcats.cc`.

**`strategy: Recreate`** — the data PVC is RWO, so a RollingUpdate leaves the new
pod Pending forever on a volume the old pod still holds.

**Storage is `openebs-hostpath`, never `nfs-client`.** CouchDB fsyncs on every
write and holds long-lived locks on its `.couch` shard files — the same trap that
put RomM's valkey on `emptyDir` and is still open against gitea's valkey (see
`design/TODO.md`). Note the consequences of that class: capacity is **advisory**
(a plain directory on the node EPHEMERAL partition, no quota), the PV is pinned to
its first node permanently, and `kubelet_volume_stats_*` for it report the *node*
filesystem, not the claim.

**No `securityContext` / `runAsNonRoot`.** The image ships with no `USER` and its
entrypoint expects root: it `chown`s `/opt/couchdb` to uid 5984 and drops
privileges itself (`docker-entrypoint.sh:37-43`). That chown is what makes a
fresh, root-owned hostpath directory writable — forcing `runAsUser` skips it and
CouchDB fails on its data dir. Upstream's own chart leaves the context empty for
the same reason, and root is permitted under the cluster-default PSA `baseline`.

## The four settings that will bite

**1. `NODENAME` is load-bearing — never change it once data exists.**
CouchDB keys its shard paths by erlang node name. The image's shipped `vm.args`
deliberately carries no `-name` line, so the entrypoint appends
`-name couchdb@$NODENAME` (`docker-entrypoint.sh:62`); with `NODENAME` unset the
VM derives a name from the container hostname, which on a Deployment changes every
rollout. The failure is nasty because it is silent: CouchDB comes back healthy
under a *different* node identity and reports an **empty database** while the real
shards sit untouched on disk. Pinned to `127.0.0.1` rather than the Service FQDN —
this node never clusters, a long erlang node name must resolve at VM boot, and the
literal removes DNS from the startup path while matching upstream's stock default.

**2. `[couchdb] single_node = true` creates the system databases.**
Without it CouchDB 3.x starts in an unconfigured cluster state with no `_users`,
`_replicator` or `_global_changes`, and every request fails until someone POSTs to
`/_cluster_setup` by hand. Setting it declaratively is what removes the need for a
bootstrap Job.

**3. Health probes must be authenticated `exec`, not `httpGet`.**
`[chttpd] require_valid_user = true` gates **`/_up` as well**, so a plain
`httpGet: /_up` returns 401 and the kubelet restart-loops a perfectly healthy pod
— the same shape as the Joplin `Host`-header trap. The probes shell out to
`curl -u "$COUCHDB_USER:$COUCHDB_PASSWORD"`, reading credentials from the env so
nothing lands in the manifest. This mirrors what the official `apache/couchdb-helm`
chart does under the same setting (`statefulset.yaml:145-155`) — note upstream
chose this over CouchDB's `require_valid_user_except_for_up` exemption.

**4. CORS origins are per-platform, and a miss fails on one platform only.**
Obsidian is not a normal web origin: desktop sends `app://obsidian.md`, mobile
(Capacitor) sends `capacitor://localhost`. Omit one and only that platform breaks,
surfacing in the plugin as a generic connection failure rather than a CORS error.

## Config delivery

Settings live in a ConfigMap whose contents an **initContainer copies onto an emptyDir**
that becomes `/opt/couchdb/etc/default.d`. Values match the plugin's own provisioner
(`utils/couchdb/provision.ts` upstream), which otherwise applies them imperatively
through the `_node/_local/_config` API — the drop-in makes them declarative.

**The initContainer is not tidiness; it is required.** The first attempt mounted the
ConfigMap directly at `/opt/couchdb/etc/local.d/10-livesync.ini` via `subPath`, and the
pod CrashLoopBackOffed with **exit 1 and completely empty logs** (`startedAt` ==
`finishedAt`). Cause: the entrypoint runs

```
find /opt/couchdb \! \( -user couchdb -group couchdb \) -exec chown -f couchdb:couchdb '{}' +
```

under `set -e` (`docker-entrypoint.sh:14` and `:43`). `chown -f` suppresses the error
*message* but still **returns 1** on a read-only mount, `find -exec … +` propagates that,
and `set -e` aborts before anything is logged. So any ConfigMap or Secret mounted
**anywhere under `/opt/couchdb`** kills the container silently. Note that dropping
`readOnly: true` does **not** help — ConfigMap volumes are always read-only in
Kubernetes — so the file genuinely has to be copied onto a writable volume. Upstream's
chart does the same thing for the same reason.

Two further details, both deliberate:

- **`default.d`, not `local.d`.** The entrypoint writes the `[admins]` block into
  `local.d/docker.ini`, and CouchDB reads `default.d` *before* `local.d`. Staging into
  `default.d` means our declarative settings load first and the generated admin
  credentials still win. Shadowing `local.d` with the emptyDir would instead discard the
  admin file — and any runtime config change — on every restart.
- **The initContainer uses the `couchdb` image, not `alpine`.** The emptyDir shadows the
  image's own `default.d`, so the initContainer copies that version's stock drop-ins
  (currently `10-docker-default.ini`, which sets `[chttpd] bind_address = any`) in
  alongside ours. Sharing the image tag keeps the two in step across a version bump.

`COUCHDB_ERLANG_COOKIE` is pinned in the SealedSecret rather than left to the
image's random default, so it stays stable across pod recreates.

## Auth

**The one user-facing service not behind Zitadel.** The LiveSync plugin
authenticates with HTTP Basic against a CouchDB admin; there is no OIDC path.
Credentials are in the `couchdb-admin-secret` SealedSecret, and exposure is
VPN-gated like the media stack. A dedicated non-admin CouchDB user per device
would be tighter than sharing the admin credential — worth doing if the device
count grows.

## Backup

`obsidian-backup` CronJob, daily **02:30** (a free slot between homebox 02:00 and
immich 03:00), restic → `rclone:filen:backups/restic/obsidian`, retention
`--group-by '' --keep-daily 30 --keep-monthly 12`.

Quiesced: scales `couchdb` to 0 via `trap cleanup EXIT` before snapshotting. The
`.couch` files are append-only, but a snapshot taken mid-write can capture a torn
header and a compaction rewrites shards wholesale — and the vault is small enough
that stopping costs seconds and is cheaper than reasoning about crash consistency.

**Deliberately NO `podAffinity`, unlike `joplin-backup` and `homebox-backup`.**
Those mount `nfs-client` PVCs, which can bind anywhere, so they must be hand-pinned
to the app's node. `couchdb-data` is `openebs-hostpath`: its PV carries nodeAffinity
from first bind, so the scheduler is already constrained. Adding podAffinity on the
app pod would make the job **unschedulable whenever couchdb is already at 0
replicas** — exactly the state a previously failed run leaves behind.

## Caveat

The vault is stored as CouchDB documents, not plain markdown, so a restic snapshot
of this PVC is **not** a directly-usable vault — restoring means standing CouchDB
back up and letting a client re-sync. If plain-text durability matters
independently, add a second path (a periodic git commit or a file-level export);
that was not built here.
