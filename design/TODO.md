# TODO — homelab-k8s

Known gaps, planned work, and items that need verification.

---

## Not Yet Built

*(nothing currently — see Future Work and Service Candidates below)*

---

## Known Broken

### Gitea's valkey cluster keeps AOF on NFS — 2026-08-01

The bundled `gitea-valkey-cluster` persists to three 8Gi `nfs-client` PVCs
(`valkey-data-gitea-valkey-cluster-{0,1,2}`). This is the same NFS/fsync-locking
trap the RomM row in `.claude/CLAUDE.md` already warns about — RomM's embedded
Valkey was deliberately put on an `emptyDir` for exactly this reason.

The failover fix (`nodes: 6, replicas: 1`) addresses *availability*, not the
storage choice. Since the data is a disposable cache, the right end state is
almost certainly `emptyDir` (or `openebs-hostpath`) rather than NFS — nothing
here is worth surviving a pod restart, and NFS buys fragility for no benefit.

Moving off NFS would also remove the re-adoption trap that made the rebuild so
awkward (see `docs/storage.md`): with `emptyDir` the "start clean" path is just
deleting the pod.

Not changed yet because it is a separate decision from the failover work.

### Valkey replica placement is imperative runtime state — 2026-08-01

`valkey-cli --cluster create` assigned each primary the replica sitting on its
**own** node, so a single node loss took out a primary *and* its only replica —
the exact failure mode the 6-node change was meant to prevent. Corrected by hand
with `CLUSTER REPLICATE` so every replica follows a primary on a different node.

That mapping lives in `nodes.conf`, not in git. It survives pod restarts but
**not** a cluster rebuild, and nothing alerts if it regresses. After any valkey
recreate, verify:

```bash
kubectl exec -n gitea gitea-valkey-cluster-0 -- valkey-cli cluster nodes
kubectl get pods -n gitea -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName
```

No replica should share a node with the primary it follows. A proper fix would
drive placement declaratively rather than relying on a manual step.

### Immich is offline ~2h16m nightly — quiesced through restic maintenance — 2026-08-04

`immich-backup` scales `immich-server` to 0 at step 1 and only restores it via
`trap cleanup EXIT`, so the deployment stays down for the whole job — including
the two steps that never touch the live PVC.

```
immich-backup  2026-08-02  03:00 → 03:11   (11m)   <- pre-fix
immich-backup  2026-08-03  03:00 → 05:11  (132m)
immich-backup  2026-08-04  03:00 → 05:16  (136m)
```

**Measured phase breakdown** (reconstructed from repo object mtimes on Filen —
snapshot file = backup done, index file = prune done):

| phase | Aug 4 duration | needs immich down? |
|---|---|---|
| scale-down + `pg_dump` | 23s | yes |
| `restic backup` | 5m14s | yes |
| **`restic forget --prune`** | **125m36s** | **no** |
| `restic check --read-data-subset=1/10` | 5m24s | no |
| total | 136m37s | |

**Prune is 92% of the runtime.** The earlier assumption that `check`
re-downloading 10% of the repo was a major term is wrong — check is ~5min, and
restic parallelises it to roughly 18 MiB/s.

**Root cause — restic runs with no cache.** The job emits, every night:

```
unable to open cache: mkdir /.cache: permission denied
warning: running prune without a cache, this may be very slow!
```

`backup-tools` sets `HOME=/`, the pod runs as uid 65534, and `/` is root-owned
`0755` — so restic cannot create `/.cache/restic`. Nothing sets
`RESTIC_CACHE_DIR` and no cache volume is mounted. Prune's
`finding data that is still in use for 33 snapshots` phase then re-fetches tree
metadata from Filen with nothing reusable between runs. This is the same class of
bug as the documented `gotify-telegram` one — uid 65534 with no writable HOME.

Scale of the metadata involved (measured): the newest snapshot alone is ~60k
files and warms 32 MiB of cache; walking all 33 snapshots warms **81 MiB total**,
so the snapshots share most tree blobs — which is precisely why a cache that
survives between runs pays off and a cold one does not.

**Why it only appeared on Aug 3:** `031af82` (committed 2026-08-02 21:58 UTC,
first affected run Aug 3 03:00) made `--group-by ''` actually prune. Before that,
prune was a permanent no-op, so the cacheless penalty never materialised. The
repo is now fully converged at 33 snapshots (30 daily + 3 monthly), 57.8 GiB,
3537 packs — so this is **steady state, not one-time backlog**.

**All seven backup jobs are cacheless** (same image, same uid 65534, no cache
volume, no `RESTIC_CACHE_DIR` anywhere in the repo). The other six only survive
it because their repos are small enough to finish in 27–71s.

The job still reports success, so nothing alerts on any of this.

**Actions, in order:**

1. **Scale back up immediately after `restic backup`** rather than at process
   exit, leaving prune and check outside the downtime window. Correct regardless
   of how fast prune is, and the only fix that stays correct if the repo grows or
   the cache is ever cold: downtime becomes ~5.5m (the backup itself) and is no
   longer coupled to maintenance at all. Apply the same restructure to the shared
   script shape.
2. **Give restic a writable cache.** *Manifests written 2026-08-04, not yet
   committed or reconciled* — a 10Gi `nfs-client` PVC (`immich-backup-cache`)
   mounted at `/cache` with `RESTIC_CACHE_DIR` set, in
   `kubernetes/apps/immich/backup/app/pvc.yml`.

   **Measured:** `prune --dry-run` takes **8s with a warm cache** versus **>11min
   and still unfinished** cacheless (killed while still in `finding data that is
   still in use for 33 snapshots`).

   This alone should take the nightly run from 136m to **~11m** (23s + 5m14s
   backup + seconds of prune + 5m24s check), i.e. back to the pre-`031af82`
   baseline — so it addresses most of the availability problem even before fix 1.

   It **must be a PVC, not an `emptyDir`** — an `emptyDir` is cold every night,
   which is exactly today's behaviour. Expect the *first* run after deploy to
   still be slow while the cache populates; runs 2+ get the benefit.

   NFS rather than `openebs-hostpath` because the job has no node affinity
   (`immich-library` is RWX), so a node-local cache would only be warm when the
   pod happened to land on the same node. This is read-mostly, pack-sized I/O —
   not the fsync-heavy pattern that keeps Valkey and the Minecraft worlds off NFS.

   The other six jobs are still cacheless; they finish in 27–71s so it is not
   urgent, but the same three-line change applies if any repo grows.
3. **Stop pruning nightly** — run `forget` (cheap, metadata-only) every night and
   `--prune` weekly.

**Related risk:** `activeDeadlineSeconds: 14400` (4h) against a 136m runtime is
57% consumed. Bash's `EXIT` trap does fire on SIGTERM (verified), so a deadline
kill would still scale immich back up — but the container memory limit is 2Gi and
cacheless prune is memory-hungry; an **OOMKill is SIGKILL, where the trap does
not run and immich stays at 0 replicas indefinitely**. Not yet observed. Fix 2
shrinks this substantially (a cached prune finishes in seconds and holds far less
in memory), but only fix 1 removes the coupling entirely.

**Verification note:** this was investigated with temporary read-only diagnostic
pods, since `k8s-cleaner` deletes succeeded job pods every 6h and the logs were
already gone. Two things to know if repeating it: killed `restic` processes
become zombies that keep holding the repo lock, so plain `restic unlock` will not
clear them (`--remove-all` does); and any restic command that fails on a held
lock exits `rc=11` in ~2s, which silently invalidates timing comparisons unless
exit codes are checked.

**That investigation broke the next night's backup** — see "A stale restic lock
blocks every subsequent backup" below. Checking `locks: 0` *before* deleting a
diagnostic pod is not sufficient; verify from a fresh pod *after* deleting it.

### A stale restic lock blocks every subsequent backup — 2026-08-05

The 2026-08-05 immich backup **failed** (`Exit 11`, job `immich-backup-29764980`,
03:05:58). The snapshot itself was written — `snapshot a7ddaf50 saved`, repo now
34 snapshots — so **no data was lost**; only `forget --prune` and `check` were
skipped. The `trap cleanup EXIT` fired correctly and immich was scaled back up at
03:06, so downtime was ~6min. All seven other backup jobs succeeded that night.

```
[backup] Pruning old snapshots...
unable to create lock in backend: repository is already locked by PID 61
  on immich-cache-verify by nobody (UID 65534, GID 65534)
lock was created at 2026-08-04 14:34:17 (12h31m38s ago)
```

Immediate cause was self-inflicted: `immich-cache-verify` was a diagnostic pod
from the investigation above, and a restic process in it re-created a lock after
the pre-deletion check reported zero. Cleared with `restic unlock --remove-all`;
repo verified at 0 locks / 34 snapshots.

**The durable lesson is the part worth keeping.** restic did *not* auto-expire a
**12.5-hour-old** lock. restic normally treats locks older than 30min as stale,
but it will only do so when it can verify the owning process is gone — which
requires the lock's hostname to match the current host. **Every Job pod has a
unique hostname**, so a lock left behind by any pod is foreign to the next run and
is never auto-expired. It blocks that repo indefinitely until someone runs
`unlock` by hand.

This is the *same* ephemeral-hostname property that broke retention in `031af82`
(`--group-by host,paths` made every snapshot its own group). It has now caused two
distinct failures; assume it will cause a third.

**Consequences to note:**
- Any job killed mid-run (OOM, node reboot, `activeDeadlineSeconds`) leaves a lock
  that silently breaks *every following night* for that repo, not just its own run.
- The failure is loud in Gotify but the job still writes its snapshot first, so
  the backup looks partly fine while maintenance silently stops. Retention then
  quietly stops converging.

**Action:** add a defensive `restic unlock` at the start of each backup script.
Note plain `unlock` may not clear a foreign-hostname lock, so `--remove-all` is
what actually works — which is safe *here* specifically because each repo has
exactly one job and every CronJob is `concurrencyPolicy: Forbid`, so a concurrent
legitimate lock cannot exist. Do not copy this pattern to a shared repo.

### Goldilocks/VPA recommendations are fabricated — no metrics-server — 2026-08-04

There is **no metrics-server** in the cluster. `pods.metrics.k8s.io` does not
resolve, so `kubectl top` fails and the VPA recommender cannot ingest anything:

```
E0804 09:02:01 cluster_feeder.go:502] "Cannot get ContainerMetricsSnapshot from MetricsClient"
  err="the server could not find the requested resource (get pods.metrics.k8s.io)"
```

The recommender still runs and still writes checkpoints, so the stack *looks*
healthy — but all 70 VPA objects emit their configured floors rather than
measurements:

| target cpu/mem | containers |
|---|---|
| `15m` / `100Mi` (= the `--pod-recommendation-min-*` flags) | 44 |
| `5m` / `34952533` (VPA hardcoded default) | 12 |
| `7m` / `50Mi` | 8 |
| `3m` / `25Mi` | 8 |
| plausibly real | 2 |

`lowerBound == target == upperBound` on every one — the signature of an empty
histogram.

**Correction, 2026-08-28: the two "plausibly real" entries are not real.** Re-ran
the analysis (now 74 VPAs / 79 container recommendations, 16 with no
recommendation at all, **77 of 79 degenerate**). The two non-degenerate entries
are `gitea` (~729Mi) and `trivy-operator` (~422Mi) — precisely the two workloads
that have been **OOMKilled**. VPA's memory recommender bumps its target on OOM
*events*, which it reads from container status, **not** the metrics API, so the
only two non-floor numbers in the stack come from the one code path that does
not need metrics-server. Their `upperBound` is `97656250000Ki` (~93 TiB), VPA's
no-confidence sentinel. Even these are wrong: trivy-operator's OOM-derived
target was 422Mi against a measured peak of **1022 MiB**.

**This already caused a real bug.** `gitea`'s memory limit was sized against
that ~422Mi figure as though it were a measurement (see the comment formerly in
`kubernetes/apps/gitea/gitea/app/helmrelease.yml`). The reasoning is circular —
the recommendation is *derived from* the OOM, so it always trails reality:
512Mi -> OOMKilled -> VPA said 422Mi -> set 1Gi -> **OOMKilled again 2026-08-28
18:51** -> VPA now says ~729Mi. Fixed by sizing from VictoriaMetrics instead
(30d, 45 pod instances: avg ~312 MiB, p99 ~595 MiB, peak 1000 MiB censored by
the limit) and raising the limit to 2Gi. gitea was the **only** manifest
justifying a limit from a VPA number — verified by grep — but that is one more
than zero, and the dashboard is still live and still lying.

Worth weighing against the "deploy metrics-server" option: 91 days of
`container_*` history already exists in VictoriaMetrics, which is strictly
better data than VPA's in-memory histogram. Goldilocks' value was the dashboard,
not the recommender, and the same sizing questions are answerable with PromQL. Concretely `goldilocks-matcha/app` recommends **5m CPU / 33MB** for a
Minecraft server deliberately limited to 9Gi. The dashboard at
`goldilocks.blackcats.cc` has been actively misleading since the stack went in.

This also blocks anything else that needs the metrics API (HPA, KEDA CPU
triggers) and undercuts the "CNPG resource requests/limits + capacity plan" item
below, which assumes usage data exists to size against.

**Action:** either deploy metrics-server (Talos needs
`--kubelet-insecure-tls=false` with proper kubelet serving certs) or delete
Goldilocks + VPA outright — three deployments and 70 CRs currently produce noise.
Decide rather than leave it half-working.

### kube-apiserver SLO rules produce no data — `KubeAPIErrorBudgetBurn` can never fire — 2026-08-04

The vm-stack chart's `metric_relabel_configs` drops the exact histogram buckets
its own bundled recording rules consume:

```yaml
regex: apiserver_request_duration_seconds_bucket|apiserver_request_sli_duration_seconds_bucket|...
action: drop
```

The apiserver *does* export them (6146 lines on `/metrics`); VictoriaMetrics
retains only `_count` and `_sum`. So `kube-apiserver-burnrate.rules`,
`-histogram.rules` and `-availability.rules` all evaluate to zero samples.

Two consequences:

1. **14 `RecordingRulesNoData` alerts have been firing continuously since
   `2026-08-01T00:52Z`.** They are `severity: info` and therefore blackholed in
   the alertmanager route, so no Gotify spam — but they permanently mask any
   *genuine* recording-rule breakage.
2. **`KubeAPIErrorBudgetBurn` is structurally dead.** The alertmanager config
   carries a deliberate, non-blackholed route for it with `repeat_interval: 24h`,
   but its input rule `apiserver_request:burnrate1h` returns 0 series. API-server
   SLO alerting exists on paper and not in practice.

**Action:** either drop the three apiserver SLO rule groups (and the dedicated
alertmanager route with them), or stop dropping the buckets and accept the
cardinality. Leaving both halves in place is the worst option — it looks like
coverage.

### Trivy scan jobs fail on multi-container pods — 2026-08-04

Four `scan-vulnerabilityreport-*` Jobs hit `BackoffLimitExceeded`. Standalone
mode scans a pod's containers in parallel against one shared cache volume, so
multi-container pods deadlock on the cache lock, and one container was OOMKilled:

```
ERROR  Failed to acquire cache or database lock
FATAL  unable to initialize fs cache: cache may be in use by another process: timeout
...
"job":"security/scan-vulnerabilityreport-545495f45c","container":"zitadel-machinekey","status.reason":"OOMKilled"
```

`zitadel-setup` (3 containers) reproduces it reliably. There is also a steady
stream of `configauditreports ... already exists` reconcile errors against
`immich-db-extensions` and the `reflector` ReplicaSets.

Switching to **`ClientServer` mode** with a central Trivy server fixes the lock
contention *and* removes the per-job ~300 MB DB download — which is the same fix
already called for under "Container image accumulation — capped, not solved"
below. Doing it once addresses both.

**Action:** move trivy-operator to `ClientServer`, raise the scan-job memory
limit, and confirm the four failing reports regenerate.

**Still live as of 2026-08-28** — unchanged symptom, now on `scan-vulnerabilityreport-5dc5f95f99`
(`media/matcha`, containers `backup`/`files`/`filebrowser-init`):
`unable to initialize fs cache: cache may be in use by another process: timeout`.
Note this is the *scan job's* cache lock and is separate from the **operator**
OOMKill fixed on 2026-08-28 (limit 1Gi → 2Gi); raising the operator limit does
not touch this.

### Minecraft restic repo failed an integrity check once — 2026-08-20

`minecraft-backup` exited 1 at 07:40 on 2026-08-20. `backup` and `forget --prune`
succeeded; `restic check --read-data-subset=1/10` did not:

```
rclone: ERROR : data/be/bebe69fdb4...: Didn't finish writing GET request
  (wrote 2097152/18408552 bytes): failed to fetch chunk:
  decrypting chunk 2: open: cipher: message authentication failed
download error: circuit breaker open for file <data/bebe69fdb4>
The repository is damaged and must be repaired.
Fatal: repository contains errors
```

A pack read back from Filen failed its rclone-crypt MAC check — a genuine
integrity error, not a timeout. All 8 nightly runs since have passed, so it
looks transient (Filen returning bad bytes once), but each run only verifies
**10%** of packs, so consecutive passes are weak evidence.

This is also the failure that exposed the missing `ttlSecondsAfterFinished`:
the Job lingered 8 days and produced 18 of 27 alert notifications.

**Action:** run a full `restic check --read-data` against
`rclone:filen:backups/restic/minecraft` once, out of band (it downloads the
whole repo — do not put it in the nightly job). If packs are genuinely bad,
`restic repair packs`. Until then the minecraft repo is *probably* sound rather
than *known* sound.

### Orphaned `openebs-hostpath` PV with pre-rename node affinity — 2026-08-04

`pvc-641863e1-a4aa-49e9-9594-5568086f2369` (30Gi, was
`monitoring/vmsingle-vm-stack-victoria-metrics-k8s-stack`) is stuck `Released`
with `reclaimPolicy: Delete`, retrying deletion forever:

```
VolumeFailedDelete: Unable to get the Node with the Node Labels
  {map[kubernetes.io/hostname:k8s-cp-3]}
```

Its `nodeAffinity` names **`k8s-cp-3`** — the node naming from before the
2026-06-17 re-IP/rename. The node is now `cp-3`, so openebs cannot resolve it and
the provisioner can neither delete the PV nor reclaim the directory. The 30Gi
lives on cp-3's local disk alongside `vanilla-data`, which is node-local storage
that **cannot expand in place**; cp-3 is currently the fullest node at 51%.

**Action:** delete the PV object manually and remove the orphaned directory on
cp-3. Grep for any other resource still referencing the `k8s-cp-*` names.

### kube-proxy runs redundantly alongside Cilium — 2026-08-04

Cilium fully owns service routing, but the `kube-proxy` DaemonSet is still
installed and programming a parallel set of iptables service rules on all three
nodes:

```
KubeProxyReplacement:  True          # cilium-dbg status
kube-proxy-replacement=true          # cilium-config
kube-proxy   3/3 Running   76d       # DaemonSet, still there
```

Nothing is visibly broken — Cilium's rules win — but it is duplicated state,
wasted resources, and a genuine source of confusion when debugging service
routing (two authorities for the same iptables chains).

**Update 2026-08-29:** `cluster.proxy.disabled: true` **is now set** in
`talconfig.yaml`, so Talos will not recreate the DaemonSet — but the DaemonSet
itself is still running (3/3, object age 101d, i.e. it outlived the 72d node
rebuild because it lives in etcd, not on the nodes). Talos does not garbage
collect a bootstrap manifest it has stopped managing, and the object is not in
git either (`owning-inventory: talos-bootstrap-manifests-inventory`), so neither
Flux nor Talos will ever remove it. It is a pure orphan.

This is also why kube-proxy was deliberately **excluded** from the 2026-08-29
platform resource-request pass: sizing requests for a workload that should be
deleted would only make it look intentional.

**Action:** `kubectl delete daemonset kube-proxy -n kube-system`, then verify
service connectivity from all three nodes. This is a live datapath change with
no Flux/git rollback path (the object is not in git), so it wants a maintenance
window rather than a drive-by.

### Degoog's FreshRSS plugin fails to start — stale API password — 2026-08-13

`degoog-org-official-extensions-freshrss` throws on every boot, so the FreshRSS
widget never loads:

```
error: FreshRSS auth failed: 401
  at /app/data/plugins/degoog-org-official-extensions-freshrss/index.js:103:26
  at async start (…/index.js:434:33)
```

The plugin authenticates against the **GReader API**
(`POST /api/greader.php/accounts/ClientLogin`, `Email` + `Passwd`). Probing that
endpoint from the degoog pod returns `401` with `google-bad-token: true` and a
body of `Unauthorized!` — that is `greader.php`'s own bad-credentials path, so
the request is reaching FreshRSS and **mod_auth_openidc is not the blocker**
(the API is exempt from the OIDC gate, and a globally disabled API would be
`503`). The stored credential for user `maxim` is simply wrong.

The likely cause is that FreshRSS's **API password is separate from the account
password** (Profile → API management) and, because this instance authenticates
via OIDC only, the user has no local password that could serve as one — it has
to be set explicitly, and was probably never set or was reset.

Note the credential lives in `/app/data/plugin-settings.json` on the
`degoog-data` PVC — runtime app state outside git, like Suwayomi's installed
sources. It will not survive PVC loss and cannot be reproduced from a `tofu
apply` + Flux rebuild.

**Action:** set an API password in FreshRSS (Profile → API management), re-enter
it in Degoog's FreshRSS plugin settings, restart the pod, and confirm the
exception is gone from the log.

---

## Stale / Needs Update

### Kavita missing from README service list

Kavita is **already deployed** (manga stack, `media` namespace — see `.claude/CLAUDE.md` and
`design/docs/services.md`), but the top-level README's service inventory doesn't list it, and it
lingered in the Service Candidates table below as if un-built. Removed from candidates; **add Kavita
to the README/service inventory** so docs match reality.

---

## Needs Verification

### ~190Gi of orphaned `Released` PVs on the Synology — 2026-08-04

21 PVs sit in `Released` with `reclaimPolicy: Retain`, so their NFS directories
are still consuming pool space:

| claim | count | size |
|---|---|---|
| `gitea/valkey-data-gitea-valkey-cluster-{0..5}` | 12 | 96Gi |
| `monitoring/vm-stack-grafana` | 5 | 10Gi |
| `media/plex-config` | 1 | 50Gi |
| `media/tranga-config` | 1 | 5Gi |
| `freshrss/freshrss-notify-state` | 1 | 100Mi |

The valkey ones are fallout from the 2026-08-01 rebuild. These are not merely
wasted space: `docs/storage.md` documents that `nfs-client` derives share paths
from namespace+PVC name, so a same-named PVC **re-adopts the old directory** —
which is exactly the trap that made the valkey rebuild awkward. Leaving 12 stale
valkey directories in place means the next rebuild hits it again.

**Action:** confirm nothing is needed from each, delete the PV objects, and
remove the backing directories on the Synology. Pair with the "Gitea's valkey
cluster keeps AOF on NFS" item above — moving valkey to `emptyDir` removes the
re-adoption trap permanently.

### Local `talosconfig` is empty — `talosctl` unusable from the workstation — 2026-08-04

`~/.talos/config` is 25 bytes and every command fails:

```
error constructing client: failed to determine endpoints
error constructing client: failed to resolve configuration context: talos config file is empty
```

Backups are unaffected — the `etcd-snapshot` CronJob carries its own
`talosconfig-secret` — but the documented recovery paths in `RUNBOOK.md`
(`talosctl etcd snapshot`, `talosctl bootstrap --recover-from`, `talosctl get
links` for the MTU checks, reading `/system/config/...` for admission config) all
assume a working local client. This is the kind of thing that is only discovered
during an incident.

**Action:** regenerate from `talhelper genconfig` output / the SOPS-encrypted
secrets, verify `talosctl -n 172.16.20.11 version` works, and note the
regeneration step in `RUNBOOK.md` so it is recoverable rather than tribal.

### `openebs-hostpath` PVC claims are fiction — alerts name the wrong culprit

An `openebs-hostpath` PVC is a plain directory on the node's `EPHEMERAL` partition with
**no quota**. Two consequences that have already caused confusion:

1. **The `storage:` request is advisory.** `matcha-data` and `vanilla-data` claim 20Gi
   each and `plex-config-local` claims 50Gi, but nothing enforces any of it — the real
   ceiling is node free space, shared with `/var/lib/containerd`, etcd and logs.
2. **`kubelet_volume_stats_*` on these PVCs report the node filesystem.** So
   `MinecraftWorldVolumeAlmostFull` does not mean "the Minecraft world is filling up";
   it means "cp-2 or cp-3 is 85% full", which during investigation turned out to be
   ~30 GB of unused container images and only ~1.4 GB of Minecraft. The alert fires on
   the right condition but names a misleading cause.
3. **Nothing checks that the claims fit the node** (observed 2026-08-04). cp-1 now
   carries `plex-config-local` (50Gi) + `vmsingle` (30Gi) = 80Gi of claims against
   97Gi of node filesystem shared with `/var/lib/containerd`, etcd and logs. Actual
   usage is fine (cp-1 42%, cp-2 44%, cp-3 51%), so this is latent rather than
   active — but because there is no quota, the scheduler will happily place a third
   hostpath PVC on cp-1 and the first thing to notice would be `DiskPressure`
   evictions. Worth an alert on *sum of hostpath claims per node* vs node capacity,
   independent of actual usage.

**Resolved 2026-08-29 (points 2 and 3 — point 1 stands).** Both halves of the
suggested action were done rather than either/or:

* **Real per-world metric.** A `world-size` sidecar (`kubernetes/apps/media/
  minecraft/app/world-size-configmap.yml`) walks the world tree and exports
  `minecraft_world_size_bytes{server,world}` plus
  `minecraft_server_data_size_bytes{server}`. It counts `st_blocks*512`, so it
  matches `du` rather than overstating sparse region files, and it scans on a
  300s timer in a background thread so a scrape never blocks on disk. A sidecar
  is the only option available: the `media` namespace runs under the cluster
  default PSA `baseline`, which forbids `hostPath`, so the node-exporter
  textfile-collector route is closed and nothing outside the pod can see the
  volume. `MinecraftWorldGrowingFast` / `MinecraftWorldLarge` now alert on that,
  with `MinecraftWorldSizeExporterStale` so a dead exporter cannot read as
  "no growth".
* **Node disk is now its own alert.** `NodeFilesystemAlmostFull` /
  `NodeFilesystemCriticallyFull` (80/90%) in `vm-stack/vmrules.yml`, per node,
  named for what they measure. Note this closed a real gap rather than just
  renaming one: **nothing else in the cluster watched node disk at all**, so the
  mis-attributed Minecraft alert had been the only coverage — and only for the
  two nodes that happened to hold a world. The 80% threshold sits above the
  kubelet `imageGCHighThresholdPercent: 70` so routine GC churn stays quiet.
* **The Synology share likewise.** `SynologyShareAlmostFull` (85%) reads
  `kubelet_volume_stats` on a single pinned claim, because that metric genuinely
  does report the backing share — pinned to one claim precisely because all ~34
  nfs-client PVCs report identical numbers and an unpinned selector would fire
  ~34 duplicate notifications for one condition. (`MinecraftBackupVolumeAlmostFull`
  was the old form of this and has been removed.)

**Still open (point 1):** the `storage:` requests remain fiction — `matcha-data`
and `vanilla-data` still claim 20Gi and `plex-config-local` 50Gi with nothing
enforcing any of it, and there is still no check that the sum of hostpath claims
fits the node. The new alerts measure reality instead of the claims, which makes
this less dangerous, but it does not make the numbers honest.

**Action:** annotate the hostpath claims as advisory (or correct them), and add
the per-node "sum of hostpath claims vs node capacity" alert described in
point 3.

### Container image accumulation — capped, not solved

`/var/lib/containerd` reached ~30 GB on cp-2 and ~28 GB on cp-3 (vs ~1.4 GB of actual
application data) because kubelet's default GC thresholds (high 85 / low 80) never
fired — the nodes sat at 72–76%. `talconfig.yaml` now sets `imageGCHighThresholdPercent:
70` / `imageGCLowThresholdPercent: 55`, and the node disks were grown 60 → 100 GB.

That is a **ceiling, not a cleanup**: usage dropped to 44–47% purely because the
denominator grew, so GC will not run and the ~30 GB is still there. It is now bounded,
not reclaimed.

**Action:** confirm GC actually engages the first time a node crosses 70% (it has never
run here). Separately, attack the source — `trivy-operator` runs in `standalone` mode
where **every scan job downloads a ~300 MB vulnerability DB**; switching to
`ClientServer` mode would cut a recurring chunk of image churn. See the Trivy row in
`.claude/CLAUDE.md`.

### CNPG WAL archiving

CNPG currently does base backups only — WAL archiving is not configured. Without WAL archiving, point-in-time recovery is not possible; recovery is limited to the last daily snapshot.

**Consider:** adding `backup.barmanObjectStore` to the CNPG Cluster spec for WAL archiving to Synology or Filen.

### Backup restore actually works

Nine backup CronJobs exist (immich, paperless, gitea, homebox, postgres, joplin, obsidian, minecraft, etcd-snapshot) writing restic snapshots to `rclone:filen:backups/restic/`. None have been restore-tested end-to-end. "Backup created" ≠ "data restorable."

`obsidian` is the one whose restore is not self-evidently a file copy: the snapshot holds CouchDB shard files keyed by erlang node name, so it only restores into a CouchDB running the same `NODENAME`, and the vault itself only reappears once a client re-syncs. Worth including in the first drill for that reason.

**Action:** Pick one app (Immich is highest-value) and run a restore drill into a clean PVC + fresh CNPG database. Document the procedure in `RUNBOOK.md` once it works.

The etcd snapshot is the same story: `RUNBOOK.md` → "Restore etcd from a snapshot" documents the intended `talosctl bootstrap --recover-from` flow, but it has never been exercised. Worth a drill against a throwaway cluster, since a bad assumption there only surfaces during a full wipe.

### TLS certificate expiry alerting

cert-manager renews `shared-tls` automatically via Cloudflare DNS-01. If renewal fails (Cloudflare API token rotation, ACME rate limit, network issue), there's no documented alert path — users will see browser warnings before the operator notices.

**Action:** Confirm whether Gatus or cert-manager metrics scrape catches a Certificate object stuck in `Ready: False` and routes to Gotify. If not, add a Gotify webhook tied to cert-manager events.

### Backup failure notification path

All backup CronJobs reference `gotify-secret` with `optional: true`. If a backup fails AND Gotify is down or its token is invalid (e.g. after a Gotify SQLite reset before `gotify-bootstrap` re-runs), the failure is silent.

**Action:** Add a secondary alert path (email via SMTP sidecar, or a separate webhook) so silent-Gotify doesn't hide silent-backups.

### Falco → Gotify → Telegram bridge

Falco events route to Gotify, then a Python WebSocket bridge forwards to Telegram. Bridge reconnect behavior on Telegram API rate-limits / network drops is not proven. No alert if the bridge pod itself crashes silently.

**Action:** Verify the bridge has a liveness probe and that bridge pod restarts are themselves notified.

---

## Future Work

### Obsidian LiveSync shares one CouchDB admin credential across every device

`obsidian/couchdb` authenticates every client as the **server admin** (`couchdb-admin-secret`).
That credential is entered on each device, cannot be revoked per-device, and carries rights
far beyond the vault — it can read or drop any database and rewrite server config.

OIDC is not the fix and was ruled out deliberately (see `design/decisions/obsidian-livesync.md`):
CouchDB has no OIDC flow, cannot consume a JWKS, and the plugin's JWT mode mints its own
tokens from a local signing key rather than acting as a relying party.

Two things would actually help, in order of value:

1. **Enable LiveSync's E2EE passphrase.** This matters most — it stops the server being able
   to read the vault at all, which makes the shared credential far less valuable. It is a
   client-side setting, so it is not captured in this repo; it has to be turned on per device
   and the passphrase stored alongside the other recovery keys.
2. **Per-device non-admin CouchDB users.** CouchDB has per-database `_security` with
   members/admins, so each device could get an individually revocable credential scoped to the
   vault DB only. Fits the existing idempotent bootstrap-Job pattern (`kavita-bootstrap`).

Neither is built. Note also that the restic snapshot restores to CouchDB documents rather than
plain markdown, so there is currently **no plain-text copy of the vault** anywhere in the backup
chain — a periodic git commit or file-level export would close that independently.

### Replace filebrowser before 2026-09-01 (upstream archival) — dated

`filebrowser/filebrowser:v2.63.23` runs as a sidecar in both Minecraft pods (`matcha`, `vanilla`),
serving the server directory at `{server}-files.blackcats.cc` for datapacks, world imports, and
in-browser config edits. **The project announces its own wind-down in the container's startup log:**

```
NOTICE: File Browser is being wound down.
NOTICE: The project is archived on 2026-09-01, after which there will be no
NOTICE: further releases and no security fixes. Known unfixed issues are at
NOTICE: https://github.com/filebrowser/filebrowser/security/advisories
```

As of 2026-07-31 there are 8+ open advisories, several **high** severity — including out-of-scope
file deletion via symlink-following in TUS upload-cache eviction, recursive copy/rename/delete
ignoring deny rules, and access-rule bypass via case-variant paths. All require an authenticated
session, and the service is VPN-gated behind a single local admin account (it is **not** in the
Zitadel SSO set), so present exposure is low. The problem is the deadline: after 2026-09-01 this is
a component with write access to a filesystem that will never receive another fix.

**Options, in rough preference order:**
1. **Migrate to [gtsteffaniak/filebrowser](https://github.com/gtsteffaniak/filebrowser)**
   ("FileBrowser Quantum") — the actively maintained fork (~7.6k stars, pushed 2026-07-31). Different
   config schema, so the `filebrowser-init` initContainer that seeds the admin account from
   `minecraft-secret` needs reworking and re-testing.
2. **Drop it entirely** — `TYPE`/`VERSION` already fetch the server jar and
   `MODRINTH_PROJECTS`/`SPIGET_RESOURCES` already declare plugins, so the residual use case is
   datapacks and world imports, which `kubectl cp` covers. Smallest attack surface, worst ergonomics.
3. **Freeze and accept** — document it in `ARCHITECTURE.md` as an accepted risk. Only defensible
   while it stays VPN-gated and single-user.

**Action:** pick one before 2026-09-01. If option 1, note that the `publishNotReadyAddresses: true`
on the `files` Service must be preserved — it is what keeps the file manager reachable while the game
server is crash-looping, which is exactly when it's needed.

### Migrate per-app backup CronJobs to VolSync

The seven backup CronJobs (immich, paperless, gitea, homebox, postgres, joplin, minecraft) work, but they're imperative
scripts wearing GitOps clothes — seven copies of similar logic, each a custom image. **VolSync**'s
restic mover gives the same restic→rclone-compatible result as a declarative `ReplicationSource`
per PVC, with built-in scheduling, pruning, and a `ReplicationDestination` CRD that makes *restores*
declarative too. That directly addresses the "Backup restore actually works" item above: restore
becomes a manifest you can rehearse, not a runbook you improvise.

(Why VolSync over Velero: Velero's main value is cluster-*resource* backup, which Git already covers
in this setup. For PVC data the declarative restic flow is the better fit — so VolSync is the better
first move; Velero stays a "maybe later" for full-cluster DR.)

**Action:** Pilot VolSync on one PVC matching the existing restic repo layout
(`rclone:filen:backups/restic/{name}`), prove a restore via `ReplicationDestination`, then migrate
the rest. Pair with the Immich restore drill.

### Dedicated CNPG cluster for Zitadel (blast-radius isolation)

Zitadel — the single OIDC provider gating Immich, Paperless, Gitea, FreshRSS, Goldilocks, Gatus —
currently shares the one CNPG cluster with six other app databases. Seven DBs in one Postgres is
fine for FreshRSS and Homebox; it's questionable for the thing that gates *everything*. A CNPG
failover hiccup or a major-version upgrade gone wrong takes down auth for every service
simultaneously — including the dashboards you'd use to debug it. A dedicated **single-instance CNPG
cluster for Zitadel** (with its own `barmanObjectStore` once MinIO exists) isolates the blast radius
and decouples Zitadel from the shared-cluster Postgres 16→17 upgrade problem (see "Postgres major
version upgrade plan").

**Action:** Stand up a separate CNPG `Cluster` for Zitadel; migrate the `zitadel` database via
logical dump+restore; repoint Zitadel; give it an independent WAL-archiving target once MinIO lands.

### Per-namespace NetworkPolicies

Cilium supports L7 NetworkPolicies. Verified 2026-08-04: exactly **one**
`NetworkPolicy` exists cluster-wide (`gitea/gitea-valkey-cluster`, shipped by the
chart, not authored here) and **zero** `CiliumNetworkPolicy` /
`CiliumClusterwideNetworkPolicy`. Effectively all pods can reach all other pods.
Adding default-deny + per-namespace allow rules would mirror the Swarm overlay
isolation model.

**Priority targets** (highest blast-radius first):
1. `postgres` namespace — allow ingress only from declared app namespaces; combined with cleartext intra-cluster Postgres traffic, any pod RCE currently = full DB access
2. `auth` namespace (Zitadel) — allow ingress only from gateway + OIDC clients
3. `cert-manager`, `flux-system`, `kube-system` — restrict egress and cross-namespace ingress

**Prerequisite — observe before enforcing:** writing default-deny policies blind is how Zitadel
breaks at 11pm. Enable **Hubble UI** first (effectively free — Cilium is already running), watch
actual flows for ~a week, then derive `CiliumNetworkPolicy` for the `postgres` and `auth`
namespaces from *observed* traffic instead of guesswork. (See Service Candidates ordering — Hubble
UI is sequenced specifically as the gate to this work.)

### Cilium runs VXLAN + legacy host routing on a single L2 segment — 2026-08-04

All three nodes are VMs on one Proxmox host sharing one L2 segment
(`172.16.20.0/24`), yet Cilium encapsulates everything:

```
routing-mode=tunnel
tunnel-protocol=vxlan
Routing:        Network: Tunnel [vxlan]   Host: Legacy
Masquerading:   IPTables [IPv4: Enabled]
```

Three separate efficiency losses stack here: VXLAN encapsulation overhead on
every pod-to-pod packet that never leaves the bridge, legacy (iptables) host
routing instead of BPF host routing, and iptables rather than BPF masquerading.
The MTU 9000 work already done makes the encapsulation cheaper but does not
remove it.

`routingMode: native` + `autoDirectNodeRoutes: true` is the natural fit for a
flat single-subnet topology, and `bpf.masquerade: true` plus BPF host routing
follow from `kubeProxyReplacement` already being on.

**Prerequisite:** this is a data-path change on the cluster's only network — do
it deliberately, not opportunistically. Note it interacts with the Netbird `wt0`
guards (`AI_CONTEXT.md`) and with the MTU pinning, both of which exist precisely
because Cilium auto-detection picked the wrong interface once already.

**Action:** evaluate native routing + BPF masquerade + BPF host routing together;
verify pod-to-pod, pod-to-service, and the `pool-b` LoadBalancer paths (Plex
`.51`, Velocity `.52`) after each step. Roll back on any ARP/L2-announcement
weirdness — see the Minecraft shared-IP incident in `.claude/CLAUDE.md`.

### Low-severity cluster hygiene — 2026-08-04

Small items found during a cluster sweep; none are urgent, all are noise that
makes real problems harder to spot.

- **`vpa-recommender` runs with `--v=4`** — debug-level logging in steady state,
  for a component whose output is currently meaningless anyway (see "Goldilocks/VPA
  recommendations are fabricated" above).
- **`observability` namespace is completely empty** — no resources at all. Either
  a leftover or an intent that never landed; delete it or document what it is for.
- **CouchDB logs `Request to create N=3 DB but only 1 node(s)` at `[error]` on every
  database creation** — `[cluster] n` is unset in `obsidian/couchdb`, so CouchDB uses
  its compiled default of 3 and clamps down. It clamps *correctly* (the live `notes`
  DB is `{"q":2,"n":1,"w":1,"r":1}`), so this is cosmetic — but it is an `[error]`-level
  line that will recur for every new database and invites a wild goose chase. Fix is one
  line, `[cluster] n = 1`, in `couchdb/app/config-configmap.yml`; needs a pod restart.
- **FreshRSS liveness probe hits an OIDC-redirecting path** — the probe gets a 302
  to Zitadel and logs a continuous `ProbeWarning` with the pod IP embedded as
  `redirect_uri` (`http://10.244.5.55:80/i/oidc/`). The app is healthy; the probe
  target is wrong. Point it at a path that does not require auth.
- **64 single-replica Deployments, 2 PDBs, memory limits at 84–90% of allocatable.**
  `postgres-primary` sits at `ALLOWED DISRUPTIONS 0`, so a Talos rolling upgrade
  will block on it and take most services down as it proceeds. This is a
  reasonable homelab trade — but confirm `RUNBOOK.md`'s upgrade section states it
  explicitly rather than letting it be discovered mid-upgrade.

### nftables host firewall on k8s nodes

Same as the broader homelab plan: default-deny inbound, SSH/node_exporter/Promtail allowlist, per-host service overrides. Not yet implemented on k8s nodes.

### Zitadel break-glass / account recovery runbook

Zitadel is the single identity provider for Immich, Paperless, Gitea, FreshRSS, Goldilocks, Gatus (OIDC) and Joplin (SAML). If the admin is locked out (lost TOTP, recovery codes gone, bootstrap secret broken) there is no documented recovery path. Joplin is the only one with an app-level fallback — `LOCAL_AUTH_ENABLED=true` keeps `admin@localhost` usable; every other app is fully gated on Zitadel.

**Action:** Add a "Zitadel admin recovery" section to `RUNBOOK.md` covering: (1) recovery code regeneration, (2) emergency admin reset via `kubectl exec` into the Zitadel pod, (3) restoring from the `zitadel-bootstrap` Job + CNPG `zitadel-role-secret`.

### Document SOPS age key protection

The single SOPS age key decrypts: Talos secrets, the SealedSecrets controller key backup at `/volume2/backups/keys/sealed-secrets-key.sops.yaml`, and any other SOPS blob. `design/docs/secrets.md` does not specify where the age key itself lives, whether it has a passphrase, or whether an off-Synology copy exists. If the age key is on the same Synology volume it protects, the chain is single-link.

**Action:** Document age key location, protection (passphrase?), and require at least one off-Synology copy (paper, hardware token, second offsite). Mention in `secrets.md` and `RUNBOOK.md` recovery section.

### Disaster recovery runbook — rebuild from Filen

`RUNBOOK.md` covers single-CP loss, all-three-CP loss, and etcd quorum recovery, but does not cover restoring application data from Filen into a freshly-rebuilt cluster. Without this, even with valid backups, restore is improvisational.

**Action:** Document the steps to: (1) re-seed CNPG databases from restic snapshots, (2) restore PVC contents (Immich library, Paperless docs, Gitea repos, Homebox SQLite), (3) re-run Zitadel bootstrap with restored data, (4) verify OIDC re-linking. Run this end-to-end during the Immich restore drill above.

### CNPG resource requests/limits + capacity plan

Shared CNPG cluster hosts seven application databases (immich, paperless, gitea, zitadel, freshrss, homebox, and any future). No documented resource requests/limits, no capacity ceiling. A memory-leaky app on the same node can starve Postgres; Goldilocks is in recommender-only mode so nothing enforces.

**Action:** Set explicit requests/limits on the CNPG cluster spec; document target headroom; add a Gatus or Prometheus alert when CNPG approaches limits.

### Postgres major version upgrade plan

Sharing one CNPG cluster across seven apps means a Postgres major upgrade (e.g. 16 → 17) must be schema-compatible with all seven simultaneously. No tested procedure, no rollback plan.

**Action:** Document the upgrade approach (in-place via CNPG image bump vs. logical dump+restore), schedule a dry-run on a test cluster, identify per-app schema compatibility checks before any future upgrade.

### Immich v2.7.5 upgrade plan

Immich is pinned to v2.7.5 with kysely migrations (see memory: `project_immich_migration`). Major version bumps require migration testing; the `oauthId` re-linking story is known-fragile after Zitadel migration.

**Action:** Define the upgrade criteria (when to bump), the rollback path (PVC snapshot + DB dump before bump), and the validation checklist (mobile sync, OIDC re-link, asset/album/person FK integrity).

### Storage capacity monitoring + quotas

Immich library and the static `/volume2/Media` share both grow uncapped. No quota, no alert before Synology pool fills, no tiering plan. A full pool stops all DB writes cluster-wide.

**Partially done 2026-08-29:** `SynologyShareAlmostFull` (85%) now alerts on
/volume2 filling, via `kubelet_volume_stats` on a pinned nfs-client claim — that
metric reports the backing share, which is the wrong number per-PVC but the right
one here. It is a single threshold off a proxy metric, not real Synology
telemetry: there is no node-exporter on the NAS, so pool health, per-share usage
and disk SMART state are all still invisible.

**Action:** Ship real Synology metrics (SNMP or node-exporter on the NAS) for
pool/per-share/SMART visibility, add the 90% critical tier, and consider
per-namespace `ResourceQuota` for PVC storage.

### Immutable / second-offsite backup tier

Restic on Filen with 30-day retention is not immutable. A cluster compromise (or a runaway delete script) can wipe recent backups before they age out. Single offsite provider = single account-compromise risk.

**Action:** Either enable restic append-only mode on a separate Filen account, or add a second offsite target (B2/MinIO/Storj) for the highest-value snapshots (Zitadel + SOPS age key + Sealed Secrets master key + Immich/Paperless).

### Centralized log retention in cluster

The in-cluster VictoriaMetrics stack handles metrics, but **VictoriaLogs is not enabled** (`vlogs` is commented out in the vm-stack HelmRelease). k8s container logs live ephemerally in `/var/log/pods/` on each node and are editable by anyone who roots a node. No forensic trail for Falco events beyond the real-time Gotify push.

**Action:** Enable VictoriaLogs (`vlogs`) in the vm-stack HelmRelease and ship node/pod logs to it for durable, queryable retention.

### Image digest pinning / signature verification

Current policy is minor-semver tags (`sonarr:4.0.*`, etc.). Tags can be re-pushed; Renovate auto-PRs accept new minor versions without provenance checks. Custom images (`ghcr.io/lucid-void/*`) are also tag-only.

**Action:** Decide explicitly: (1) accept the risk and document it in `ARCHITECTURE.md` key decisions, or (2) move to digest pinning for the custom images at minimum and consider Sigstore/cosign verification via Kyverno admission policy. Either outcome is fine — leaving it undecided is the issue.

### UDM SE DNS single point of failure

UDM SE serves gateway + DHCP + DNS resolver + ad blocking. UDM reboot or failure → all `*.blackcats.cc` unresolvable cluster-wide. No secondary DNS resolver, no fallback path.

**Consider:** A secondary resolver (Technitium on a Pi, or k8s CoreDNS exposed on the VLAN) configured as the second nameserver on DHCP. Low-priority if UDM uptime has been acceptable.

### Synology NFS — failover / recovery story

All `nfs-client` PVCs and the static `/volume2/Media` PV depend on a single Synology. Disk pool failure or controller crash = cluster-wide PVC unavailability. The design doesn't document a recovery procedure (rebuild Synology, re-export shares, re-mount PVs, restore from Filen).

**Action:** Document the Synology-loss recovery path in `RUNBOOK.md`. Pair with the Filen restore drill above.

### Secret rotation procedure (documentation only)

Sealed Secrets key rotation is intentionally disabled (`ARCHITECTURE.md` decision: "single stable key simplifies backup/restore"). If a future incident forces rotation, no procedure exists.

**Action:** Add a "Key rotation (if forced by compromise)" section to `RUNBOOK.md` documenting: (1) generate new key, (2) re-seal every committed SealedSecret with the new public cert, (3) restore new private key into the controller, (4) restart pods consuming rotated secrets. Document only — don't perform unless forced.

---

## Service Candidates

### Recommended implementation order

Dependency-aware ordering rather than the raw lists below:

1. **MinIO first** — it unlocks the CNPG fix. WAL archiving / PITR is the scariest single gap (one
   shared Postgres backing seven apps incl. Zitadel, currently dump-only). MinIO on OpenEBS hostpath
   (or Synology iSCSI) gives an S3 target for `backup.barmanObjectStore`, taking the Postgres story
   from "yesterday's dump" to point-in-time recovery. It *also* unblocks Velero/VolSync and
   VictoriaLogs object storage later. **Deploy it for CNPG, not as an abstract building block.**
2. **Hubble UI second** — the prerequisite to the NetworkPolicies work (see Future Work). Effectively
   free since Cilium is already running. Enable it, watch real flows for a week, then write
   `CiliumNetworkPolicy` for `postgres`/`auth` from observed traffic instead of guessing.
3. **Vaultwarden third** — the most glaring *functional* gap in a degoog stack that already covers
   identity, photos, docs, and RSS. Fits existing patterns exactly: CNPG database, Sealed Secret,
   HTTPRoute, restic CronJob. **Caveat:** make it the first app you run a full restore drill on —
   even before the Immich drill above — because a password vault you can't restore is worse than no
   vault.
4. **Kyverno fourth, scoped narrowly** — deploy it to *resolve the image-provenance decision* (see
   "Image digest pinning / signature verification"), not as a general policy engine. A single
   `verifyImages` policy for `ghcr.io/lucid-void/*` (cosign-sign the two custom images in the
   existing GitHub Actions workflows) plus a registry allowlist gets ~90% of the value with minimal
   admission-webhook blast radius. **Exclude `kube-system` and `flux-system` from enforcement** so a
   Kyverno outage can't brick reconciliation.

**Deprioritized (with reasons):**
- **Harbor** — Spegel already provides pull-through caching and registry-outage resilience; Harbor
  adds a stateful service to babysit for marginal gain.
- **Tempo / OpenTelemetry Collector** — no instrumented apps emit traces today; it'd be a backend
  with nothing to ingest. Revisit only once apps emit spans.
- **Headlamp** — fine, but k9s via mise costs zero cluster resources for the same day-to-day
  inspection.

**Lowest-friction frontend wins:** **Navidrome** and **Audiobookshelf** — the `media` namespace, NFS
PV, and Gateway patterns already exist, so these are near-drop-in.

### Backend / Infrastructure

| Service | What it adds |
|---|---|
| **MinIO** | **① Do first.** On-prem S3-compatible object store; unlocks CNPG WAL archiving / PITR (`barmanObjectStore`), plus Velero/VolSync and VictoriaLogs object storage. Deploy *for CNPG* first |
| **Hubble UI** | **② Do second.** Cilium already running — real-time network flow visualization + service maps at no extra cost; the prerequisite for writing NetworkPolicies from observed traffic |
| **Kyverno** | **④ Scoped only.** Policy-as-code admission controller; use it narrowly to enforce image provenance (`verifyImages` for `ghcr.io/lucid-void/*` + registry allowlist), **excluding `kube-system`/`flux-system`** |
| **VolSync** | Declarative restic backup/restore per PVC (`ReplicationSource`/`ReplicationDestination`); replaces the seven imperative per-app backup CronJobs with GitOps-native, restore-rehearsable manifests — see Future Work |
| **Velero** | Kubernetes-native PVC snapshot + resource backup; cluster-level DR. _Lower priority — cluster resources are already in Git; prefer VolSync for PVC data_ |
| **Grafana Tempo** | _Deprioritized — no instrumented apps emit traces yet, so it'd be a backend with nothing to ingest._ Distributed tracing backend; revisit once apps emit spans |
| **OpenTelemetry Collector** | _Deprioritized (same reason as Tempo)._ Unified pipeline to collect/route traces, metrics, and logs |
| **Harbor** | _Deprioritized — Spegel already gives pull-through caching + registry-outage resilience; Harbor is a stateful service to babysit for marginal gain._ Private OCI registry with proxy cache + Trivy |
| **KEDA** | Event-driven autoscaling; scale jobs based on queue depth rather than CPU (useful for media transcoding or backup queues) |
| **Headlamp** | _Deprioritized — k9s via mise gives the same day-to-day inspection at zero cluster cost._ Lightweight web-based Kubernetes dashboard |

### Frontend / User Applications

| Service | What it replaces / adds |
|---|---|
| **Vaultwarden** | **③ Do third.** Bitwarden-compatible password manager — the most obvious functional gap in the degoog stack. Fits existing patterns (CNPG + Sealed Secret + HTTPRoute + restic CronJob). **Run its full restore drill before Immich's** |
| **Navidrome** | _Lowest-friction win — `media` ns + NFS PV + Gateway patterns already exist._ Self-hosted music streaming with Subsonic API; every mobile client just works |
| **Audiobookshelf** | _Lowest-friction win (same reason as Navidrome)._ Audiobooks + podcasts in one app; self-hosted Audible + Pocket Casts replacement |
| **Stirling PDF** | Browser-based PDF tools (merge, split, OCR, compress); replaces half a dozen disposable web tools |
| **Mealie** | Recipe manager with meal planning and grocery lists |
| **Actual Budget** | Local-first personal finance; YNAB-style zero-based budgeting with no cloud sync required |
| **Hoarder** | Bookmark manager with automatic AI tagging and full-page snapshots; degoog for browser bookmarks |
| **Vikunja** | Self-hosted task/project manager; Todoist/TickTick replacement with CalDAV sync |
| **Syncthing** | P2P file sync across devices; complements Immich for non-photo files and replaces Google Drive sync on desktops |
| **Pterodactyl** / **Pelican** | _Declined — superseded by the Minecraft stack in `media` (see `docs/services.md`)._ Both drive game servers through **Wings**, which orchestrates Docker containers and has no Kubernetes backend ([pelican-dev/panel#933](https://github.com/pelican-dev/panel/discussions/933) is still a proposal). On Talos there is no Docker at all, so the only path is a privileged Docker-in-Docker sidecar plus node-pinned image storage — and the panel's servers/allocations become runtime state outside git. If a panel UI is ever wanted, run it on a VM outside the cluster (like the ZeroTier VM) rather than fighting this in-cluster |
| **playit.gg** | _Not needed._ Tunnel service for exposing game servers without port forwarding — but remote access is already Netbird-gated by design, and the Velocity proxy publishes the servers internally at `172.16.20.52:25565`. Would only apply if servers were ever opened to non-VPN players |
| **changedetection.io** | Web page change monitoring and alerting; self-hosted alternative to Visualping/Wachete |
