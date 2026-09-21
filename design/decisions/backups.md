# Backups

**Read before editing:** `kubernetes/apps/*/backup/`, `kubernetes/apps/kube-system/etcd-snapshot/`, `kubernetes/images/backup-tools/`

## Current state

Offsite target is restic over an rclone crypt remote to Filen cloud. One restic repo per
job at `rclone:filen:backups/restic/{name}`. Every job is a `CronJob` in the app's own
namespace running `ghcr.io/lucid-void/backup-tools`.

| Time | Job | Quiescing |
|---|---|---|
| 01:00 | `etcd-snapshot` (in `kube-system`) | n/a |
| 01:30 | changedetection-backup | scale to 0 |
| 02:00 | homebox-backup | scale to 0 |
| 02:30 | obsidian-backup | scale to 0 |
| 03:00 | immich-backup | scale to 0 |
| 03:30 | postgres-backup | none — reads the CNPG read replica |
| 04:00 | paperless-backup | scale to 0 |
| 05:00 | gitea-backup | scale to 0 |
| 07:00 | minecraft-backup | quiesced |

Scale-down jobs go to 0 replicas via `trap cleanup EXIT`. homebox and gitea back up
SQLite/repos; immich and paperless dump Postgres plus PVCs into a single
snapshot. Apps with their own quiesced job are excluded from `postgres-backup`'s
`databases.yml` list.

Retention is `--group-by '' --keep-daily 30 --keep-monthly 12`.

**Postgres has no PITR** — no `ScheduledBackup`/`Backup`/`barmanObjectStore`, no WAL
archiving. `postgres-backup`'s logical dump is the only database backup.

**There are no VM/PBS backups.** Recovery is `tofu apply` + talhelper + Flux
reconciliation, with data restored from Filen. **Restore procedures are in
`design/runbook.md`, not here** — per-app restic and etcd both.

`etcd-snapshot` takes `talosctl etcd snapshot` from the first reachable control plane
(`.11` → `.12` → `.13`), downloading `talosctl` at runtime. Needs three SealedSecrets:
`restic-secret`, `rclone-secret`, `talosconfig-secret`. It is a **secondary** path —
rebuilding from git is primary, so the snapshot only covers in-cluster state git never
held. Never restore-tested.

`backup-tools` bundles bash, curl, **jq** (added in image v1.1.0 — older tags lack
it), kubectl, restic, rclone and postgresql17-client — but **not** python3, and its
`find` is busybox (no `-printf`, no `-readable`).

Gotify notifications are priority 5 on success, 8 on failure. The token is in
`gotify-secret`, managed by the `gotify-bootstrap` Job (not a SealedSecret), wired
`optional: true` so jobs run before bootstrap completes.

## Rules

- **Keep `--group-by ''` on every `forget`** — restic defaults to `--group-by host,paths`
  and every Job pod has a unique hostname, so each nightly snapshot forms a *group of
  one* that `--keep-daily 30` trivially keeps: retention deletes nothing and `--prune`
  becomes a permanent no-op. The log tell: N consecutive
  `Applying Policy: keep 30 daily snapshots` / `keep 1 snapshots:` blocks.
- **Every script runs `restic unlock --remove-all` right after the repo-init block** —
  a job killed mid-run (OOM, node reboot, `activeDeadlineSeconds`) leaves a lock that
  blocks *every following night* for that repo, not just its own run. restic only
  auto-expires a lock it can prove is dead, which requires the lock's hostname to match
  the current host, and every Job pod has a unique hostname — so plain `restic unlock`
  does not clear it and `--remove-all` is the only thing that does. Any restic command
  blocked on a held lock exits **11** in ~2s. Observed 2026-08-05 on `immich-backup`
  (`Exit 11`: the snapshot was written, only `forget --prune` and `check` were skipped,
  so Gotify reported a failure against an otherwise intact repo). `--remove-all` is safe
  **here only** because each repo has exactly one job and every CronJob is
  `concurrencyPolicy: Forbid` — never copy it to a repo with concurrent writers, where
  it would delete a live writer's lock.
- **Bump `TALOS_VERSION` in the `etcd-snapshot` script alongside every Talos upgrade** —
  it is pinned there and the job downloads that exact `talosctl` at runtime.
- **`etcd-snapshot` also runs `restic check --read-data-subset=1/10`** — spot-checks
  real data, not just metadata.
- **Restore etcd with `talosctl bootstrap --recover-from <snap>` *instead of* plain
  `talosctl bootstrap`** — running both discards the snapshot. Procedure in
  `design/runbook.md`.
- **Use the official rclone binary from `downloads.rclone.org` in any image that needs
  the `filen` backend** — it arrived in rclone v1.69 and Alpine's `apk add rclone`
  installs something older that lacks it.
- **A backup job whose data PVC is RWO *and* `nfs-client` needs `podAffinity` onto the
  app's node** (homebox) — scale-down happens inside the script, so the job
  mounts the PVC while the app pod still holds it, and an `nfs-client` PV can bind
  anywhere. immich is exempt only because `immich-library` is RWX.
- **A hostpath-backed (`openebs-hostpath`) job must NOT have `podAffinity`** (obsidian) —
  that PV already carries nodeAffinity from its first bind, so the scheduler is
  constrained anyway, while podAffinity on the app pod makes the job unschedulable
  whenever the app is already at 0 replicas — exactly the state a prior failed run
  leaves behind.
- **Exclude derived data the app writes non-world-readable** — jobs run as uid 65534
  against read-only PVC mounts, so any file the app leaves unreadable fails the whole
  run: restic exits **3** ("snapshot saved, but at least one source file could not be
  read") and `set -e` aborts *after* the snapshot is written but *before*
  `forget --prune` and `check`. The Gotify body then reads like a success
  (`snapshot … saved`) with a one-line
  `Warning: at least one source file could not be read` above it. Paperless's
  Tantivy search index writes `meta.json`/`.managed.json` mode `0600` as uid 1000, hence
  `--exclude=/data/index`; the index is derived data, rebuilt with
  `document_index reindex` (`design/runbook.md`). Do not instead loosen the backup's
  uid — that couples it to the app image.
- **Diagnose an unreadable-file failure from a pod running as the backup's uid with the
  same read-only mounts** — a `find` inside the app pod lies twice: it runs as root, and
  paperless bind-mounts the PVCs at `/data` and `/media`, not at the
  `/usr/src/paperless/*` paths.
- **Always post to the in-cluster Service `http://gotify.monitoring.svc.cluster.local/message`,
  never `https://gotify.blackcats.cc`** — the public hostname needs DNS plus egress out
  to the Gateway and back, exactly what breaks in the commonest failure mode (a node
  reboot killing egress). Applies to every job-style Gotify caller: every app backup,
  `etcd-snapshot`, `kubent` and `security-report`. Legitimate public-hostname uses: the
  HTTPRoute, the Gatus check (deliberately probing the public path) and the homepage
  link.
- **Keep the log capture and the tail in the failure body** — scripts capture everything
  with `exec > >(tee "$LOG") 2>&1` and the handler awk-JSON-escapes `tail -10 "$LOG"`
  into the Gotify message, which is what makes a failure triageable without kubectl.
  `notify_gotify` ends in `>/dev/null || true`, so a failed notification leaves no trace
  in the job log and the backup fails silently.
- **Leave `backoffLimit: 0` / `restartPolicy: Never` alone** — Job backoff is
  ~10s/20s/40s while the real-world failure (a node reboot) lasts minutes, so retries
  would not help and would only re-enter the scale-down/scale-up cycle. The daily cadence
  is the recovery mechanism.
- **Every CronJob carries `ttlSecondsAfterFinished: 86400`** — without it
  `failedJobsHistoryLimit: 3` retains a failed Job until three *more* failures evict it,
  so one bad night leaves the object there while `KubeJobFailed` re-fires roughly twice
  a day forever. Nothing is lost: each script posts its own Gotify ✗ with the log tail,
  and that message persists in Gotify's own DB (`/app/data/gotify.db`) independently of
  the Job object — so the *alert* stops being the record.
- **Pair that TTL with `ignore-check.kube-linter.io/job-ttl-seconds-after-finished` on
  top-level metadata** — kube-linter's `job-ttl-seconds-after-finished` check warns a
  jobTemplate-level TTL can conflict with the history limits. They are complementary
  here: whichever fires first removes the Job, and three runs of a daily job cannot
  accumulate inside 24h, so the TTL always wins and the limits stay a cap.
- **A jobTemplate change affects only *new* Jobs** — an existing lingering Job must be
  deleted by hand.
- **Prefer `backup-tools` plus a ConfigMap-mounted script over `apk add curl jq kubectl`
  at container start**, and move all consumers to a new image tag together — leaving some
  behind keeps two copies of a ~200MB layer on every node's `/var`, the partition that
  trips `NodeFilesystemAlmostFull`. Only `gitea-db-bootstrap` and `kavita-bootstrap`
  still do a start-time `apk add`.

## Verify

```bash
mise exec -- kubectl get cronjobs -A -o custom-columns=NAME:.metadata.name,LAST:.status.lastSuccessfulTime
mise exec -- kubectl get cronjob <name> -n <ns> -o jsonpath='{.spec.jobTemplate.spec.ttlSecondsAfterFinished}'
grep -rn "group-by" kubernetes/apps/*/backup/
```
