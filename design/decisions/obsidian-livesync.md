# Obsidian LiveSync (CouchDB)

**Read before editing:** `kubernetes/apps/obsidian/`

## Current state

Central CouchDB server (`couchdb:3.5.2`, `bjw-s/app-template`, single controller `app`
→ Deployment/Service `couchdb`, port 5984, `obsidian.blackcats.cc`) is the sync backend
for the Obsidian LiveSync plugin — chosen over Syncthing P2P because the cluster is
already up 24/7 with Netbird as transport and a restic pipeline, convergence otherwise
needs two devices awake at once, there is nothing canonical to back up in a peer mesh,
and Syncthing's global discovery/relays traverse third-party servers anyway. iOS also
has no first-party Syncthing client.

`strategy: Recreate` (RWO data PVC). Storage is `openebs-hostpath`, never `nfs-client`
— CouchDB fsyncs on every write and holds long-lived locks on its `.couch` shard files.
No `securityContext`/`runAsNonRoot` — the image ships with no `USER` and its entrypoint
`chown`s `/opt/couchdb` to uid 5984 itself before dropping privileges; forcing
`runAsUser` skips that chown and CouchDB fails on its data dir (permitted under the
cluster-default PSA `baseline`).

Settings are delivered via a ConfigMap that an **initContainer copies onto an emptyDir**
that becomes `/opt/couchdb/etc/default.d` (not `local.d` — the entrypoint writes the
`[admins]` block into `local.d/docker.ini`, and `default.d` loads first, so declarative
settings load without discarding the generated admin credentials on every restart). The
initContainer uses the `couchdb` image, not `alpine`, so it also carries the image's own
stock `default.d` drop-ins alongside ours; sharing the image tag keeps the two in step
across a version bump. `COUCHDB_ERLANG_COOKIE` is pinned in the SealedSecret rather than
left to the image's random default, so it stays stable across pod recreates.

The one user-facing service not behind SSO — the LiveSync plugin authenticates with
HTTP Basic against a CouchDB admin (`couchdb-admin-secret` SealedSecret); there is no
OIDC path. Exposure is VPN-gated like the media stack.

`obsidian-backup` CronJob (daily), restic → `rclone:filen:backups/restic/obsidian`,
retention `--group-by '' --keep-daily 30 --keep-monthly 12`. Quiesced: scales `couchdb`
to 0 via `trap cleanup EXIT` before snapshotting, because a snapshot mid-write can
capture a torn `.couch` header. Deliberately **no `podAffinity`**, unlike
`homebox-backup` — `couchdb-data` is `openebs-hostpath`, so its PV already carries
nodeAffinity from first bind; adding podAffinity on the app pod would
make the job unschedulable whenever couchdb is already at 0 replicas, exactly the state
a previously failed run leaves behind.

The vault is stored as CouchDB documents, not plain markdown — a restic snapshot of
this PVC is not a directly-usable vault; restoring means standing CouchDB back up and
letting a client re-sync. The database name and the LiveSync E2EE passphrase are chosen
client-side, not in git — nothing here pins the vault's database name, so a rebuild
plus a client pointed at a different name silently starts an empty vault. The
passphrase is unrecoverable and belongs with the other recovery keys.

## Rules

- **Never mount a ConfigMap or Secret anywhere under `/opt/couchdb`, including via
  `subPath`** — the entrypoint runs
  `find /opt/couchdb \! \( -user couchdb -group couchdb \) -exec chown -f couchdb:couchdb '{}' +`
  under `set -e`; `chown -f` suppresses the error message but still returns 1 on a
  read-only mount, and `set -e` aborts before anything is logged. The pod
  CrashLoopBackOffs with **exit 1 and completely empty logs** — dropping
  `readOnly: true` does not help, since ConfigMap volumes are always read-only
  regardless. Stage config onto a writable `emptyDir` via an initContainer instead.
- **Never change `NODENAME` once data exists** — CouchDB keys its shard paths by
  erlang node name, and the image's shipped `vm.args` has no `-name` line, so the
  entrypoint appends `-name couchdb@$NODENAME`; with `NODENAME` unset the VM derives a
  name from the container hostname, which changes every Deployment rollout. The
  failure is silent: CouchDB comes back healthy under a *different* node identity and
  reports an **empty database** while the real shards sit untouched on disk. Pin it to
  `127.0.0.1` — this node never clusters, and the literal removes DNS from the
  startup path.
- **Set `[couchdb] single_node = true`** — without it CouchDB 3.x starts in an
  unconfigured cluster state with no `_users`/`_replicator`/`_global_changes`, and
  every request fails until someone POSTs to `/_cluster_setup` by hand. Setting it
  declaratively removes the need for a bootstrap Job.
- **Health probes must be authenticated `exec`, not `httpGet`** —
  `[chttpd] require_valid_user = true` gates `/_up` as well, so a plain
  `httpGet: /_up` returns 401 and the kubelet restart-loops a perfectly healthy pod.
  Probe with `curl -u "$COUCHDB_USER:$COUCHDB_PASSWORD"`, reading credentials from the
  env so nothing lands in the manifest.
- **CORS origins are per-platform, and a missing one fails on one platform only** —
  desktop Obsidian sends `app://obsidian.md`, mobile (Capacitor) sends
  `capacitor://localhost`. Omitting either breaks only that platform, surfacing as a
  generic connection failure rather than a CORS error.
- **Set `[cluster] n = 1` explicitly rather than ignoring the "wrong N" error log** —
  `Request to create N=3 DB but only 1 node(s)` logs at `[error]` on every database
  creation because `[cluster] n` is unset and CouchDB's compiled default of 3 clamps to
  what actually exists; it clamps correctly, but the log noise recurs for every new
  database.

## Verify

```bash
mise exec -- kubectl get pods -n obsidian
mise exec -- kubectl exec -n obsidian deploy/couchdb -- curl -s -u "$COUCHDB_USER:$COUCHDB_PASSWORD" http://127.0.0.1:5984/_up
```
