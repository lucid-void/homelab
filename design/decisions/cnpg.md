# CloudNativePG

**Read before editing:** `kubernetes/apps/postgres/`, `kubernetes/apps/*/database/`, `kubernetes/apps/kube-system/reflector/`

## Current state

One shared CNPG cluster, `postgres` in the `postgres` namespace (primary + one read
replica), chart `cloudnative-pg`. It is configured
`primaryUpdateStrategy: unsupervised` + `primaryUpdateMethod: restart`, and
`ENABLE_INSTANCE_MANAGER_IN_PLACE_UPDATES` is unset (default `false`).

**Backups are logical dumps only.** There is **no** `ScheduledBackup`/`Backup`/
`barmanObjectStore` — no WAL archiving, no PITR. The only DB backup is the
`postgres-backup` CronJob (03:30) doing `pg_dump` off the read replica. Consequence:
CNPG 1.31's removal of in-tree Barman Cloud support is a non-event here. Apps that run their
own quiesced backup job are excluded from `postgres-backup`'s `databases.yml` list.

**Passwords.** CNPG managed-role secrets (`{app}-role-secret`) in the `postgres`
namespace are the single source of truth. Reflector (in `kube-system`,
emberstack/reflector chart `7.*`) mirrors each one into the app namespace via
`reflector.v1.k8s.emberstack.com/reflection-auto-*` annotations on the SealedSecret
template, so no app needs a duplicate SealedSecret. Apps read the `password` key via
`secretKeyRef`.

Gitea is the exception: its chart offers only `extraEnvFrom` (no per-key `valueFrom`),
so a `gitea-db-bootstrap` Job remaps `password` → `GITEA__database__PASSWD` into a
separate Secret consumed through `extraEnvFrom`.

## Rules

- **Leave `enableSuperuserAccess: true` on the shared cluster** — the backup CronJob
  dumps every database as `postgres`, and the immich extension job needs superuser to
  create its extensions; turning it off breaks both silently at the next run.
- **Expect ~2–5 min of write interruption on any CNPG *minor* operator bump** — the
  bump changes the instance manager, and with in-place updates off the operator does a
  rolling update of the Postgres pods, replica first then primary. The primary restarts
  in place (phase `Primary instance is being restarted without a switchover`) rather
  than switching over.
- **Do not panic when Paperless CrashLoopBackOffs through an operator bump** — it exits
  1 and then self-recovers. Immich, gitea, freshrss, zitadel, sonarr/radarr and romm
  reconnect without restarting.
- **Confirm `kubectl get cluster postgres -n postgres` reports
  `Cluster in healthy state` with 2 ready before declaring an upgrade done** — the
  HelmRelease going Ready says nothing about the database pods.
- **Nudge the `Cluster` object after adding any new Postgres-backed app** — adding a
  service adds its `{app}-role-secret` SealedSecret (in `{app}-database`) *and* its
  managed role (in `postgres-cluster`). They reconcile near-simultaneously, and if CNPG
  evaluates the role before Sealed Secrets has decrypted the secret it records
  `cannotReconcile: failed to get password secret … not found` and **never retries** —
  the status stays frozen indefinitely. Cascade: no role → the `Database` CR fails
  `role "x" does not exist` → no DB → the app crash-loops on
  `password authentication failed` (`28P01`). Expect this on *every* new app.
- **`flux reconcile` does not fix the managed-role race** — the `Cluster` object already
  matches git, so no watch event fires. Annotate it instead:
  `kubectl annotate cluster postgres -n postgres reconcile-nudge="$(date +%s)" --overwrite`,
  then remove the annotation.
- **A replica that cannot rejoin blocks *all* image rollouts** —
  `pg_rewind: could not find common ancestor of the source and target cluster's timelines`
  means that replica's PGDATA is unrecoverable, and CNPG then refuses any
  rolling update because it will not roll pods while it cannot read every instance's
  status (`Instance Status Extraction Error: HTTP communication issue`). An image fix
  lands in `status.image` and stops there while the pods keep running the bad tag.
- **Rebuilding that replica needs BOTH a PGDATA rename AND a PVC delete** — CNPG only
  runs `pg_basebackup` from a `<cluster>-<n>-join` Job and only creates that Job when
  the PVC is **absent** (emptying PGDATA alone just makes the instance manager die on
  `stat .../pgdata`), while the rename is required because `nfs-client` re-adopts the
  same `<ns>-<pvc>` directory on recreate. Full procedure: design/runbook.md → "Rebuild
  a CNPG replica that cannot rejoin".

## Verify

```bash
mise exec -- kubectl get cluster postgres -n postgres
mise exec -- kubectl get cluster postgres -n postgres -o jsonpath='{.status.managedRolesStatus}'
mise exec -- kubectl get cluster postgres -n postgres -o jsonpath='{.status.image}'
```
