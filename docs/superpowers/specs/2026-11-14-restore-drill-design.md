# Restore drill — design

Date: 2026-11-14. Status: awaiting owner approval. Closes the `restore-drill` item in
`design/TODO.md`.

## Intent

No backup in this cluster has ever been restored. The drill is a weekly, automatic proof
that the backups of the critical apps restore into something that **runs**: the real app
boots on the restored data and answers correctly.

The owner's two requirements:

1. Only critical applications: **Keycloak, Gitea and Immich**.
2. Fully separate from production, so nothing live can be impacted.

## Goals

- Every week, restore the latest Keycloak database dump and Gitea snapshot from Filen, boot
  the real Keycloak and Gitea on them, and log in to Gitea through Keycloak.
- Every week, restore Immich's database dump plus a small sample of photos, boot Immich on
  them, and prove the sampled files match what the database says they are.
- Post one pass or fail per drill to Gotify, with enough of the log to triage a failure.
- Leave nothing behind after a run.

## Non-goals

- A full Immich library restore. The library PVC is 100Gi; the owner ruled out restoring it
  repeatedly. A sample plus a rotating `restic check` slice stands in for it.
- Proving the Keycloak operator, the Gitea chart or CNPG rebuild the apps. Git and Flux
  already do that; the unproven part is the data.
- Other apps (Paperless, Obsidian, Homebox, media configs, Minecraft, etcd). The design
  must make adding one later a contained change, nothing more.
- Replacing the by-hand end-to-end restore in `design/TODO.md` ("Restore-test one backup
  end to end"). That item stays open: it restores into production, the drill never does.

## Anti-goals — the drill has failed if any of these can happen

- It writes to, locks, prunes or unlocks a restic repo.
- A restored app reaches anything real: the live Postgres, the mail bridge, a webhook
  target, a push mirror, the live Keycloak.
- It mounts or reads a production PVC, or holds RBAC outside its own namespace.
- It reports a pass on an empty or stale restore.

## Approaches considered

| | A. Sealed box | B. Parallel stack | C. Outside the cluster |
|---|---|---|---|
| What runs | Plain containers of the real app images plus a throwaway Postgres, in one pod per drill | Drill copies via the Keycloak operator, the Gitea chart and a second CNPG cluster | The same box as A on a separate VM |
| Separation | Pod has no network out | Shares operators and Flux with production | Total |
| Cost | ~1 week | ~2 weeks, breaks on chart and operator upgrades | ~1 week, outside Flux, needs a VM kept alive |

**The owner picked A.** B shares controllers with production, which is the opposite of
requirement 2. C leaves GitOps for no extra proof.

## Design

### Shape

A new `restore-drill` namespace under `kubernetes/apps/restore-drill/`, holding three
CronJobs and one scratch PVC. No operator, no Helm chart, no HTTPRoute, no Service.

| When (Saturday, UTC) | CronJob | Network | What it does |
|---|---|---|---|
| 10:00 | `drill-restore` | DNS + internet 443 (Filen) + Gotify | Wipes the scratch PVC, restores from the three repos, writes a marker file |
| 12:00 | `drill-sso` | Gotify only | Boots Postgres + Keycloak + Gitea on the restored data, runs the checks, reports |
| 13:00 | `drill-immich` | Gotify only | Boots Postgres + Valkey + Immich on the restored data, runs the checks, reports, wipes the scratch PVC |

All times sit outside the nightly backup window (01:00–07:30), which matters because the
backup jobs run `restic unlock --remove-all`.

Splitting restore from boot is what makes the isolation real: a network policy applies to
a whole pod, so the pod that needs the internet (restore) is never the pod that runs a
restored app (boot). The two boot jobs are separate pods so one failing does not hide the
other's result. *(My call: two fixed-time CronJobs joined by a marker file rather than an
orchestrator that creates Jobs — it needs no RBAC at all.)*

### Isolation rules

- **Network.** `CiliumNetworkPolicy` objects in the namespace: default deny in and out for
  every pod; `drill-restore` may reach kube-dns, the internet on 443 and
  `gotify.monitoring` on 80; the boot pods may reach **only** `gotify.monitoring` on 80.
  Boot pods get no DNS — everything they talk to is on localhost. `drill-restore`
  resolves Gotify's ClusterIP and writes it into the marker file, and the boot pods post
  to that IP. *(My call, after review: opening DNS to the boot pods would let a restored
  app resolve names.)*
- **RBAC.** None. The CronJobs use a ServiceAccount with
  `automountServiceAccountToken: false`. Unlike the backup jobs, nothing here scales a
  Deployment.
- **Storage.** One `restore-drill-scratch` PVC (`nfs-client`, 30Gi, RWX so it is not tied
  to a node) carries restored files from the restore pod to the boot pods. Postgres data
  directories are `emptyDir`. No production PVC is referenced anywhere in the namespace.
- **Repos are read-only by behaviour.** Filen offers no read-only login, so the script
  enforces it: every restic call uses `--no-lock`; the script contains no `backup`,
  `forget`, `prune`, `unlock` or `init`; a missing repo is a failure, never an init.
- **Belt and braces inside the boot pods**, in case the network policy is ever wrong:
  Gitea runs with mirrors, webhooks, cron and the mailer disabled; Keycloak gets no SMTP
  relay sidecar; Immich's machine-learning container is not started.

### `drill-restore`

Image: `backup-tools` at the tag the backup jobs use. Steps:

1. Empty the scratch PVC.
2. For each repo, check the latest snapshot is under 48 hours old, else fail.
3. Restore, each into its own directory:
   - `postgres-backup` repo → only `/db/keycloak.pgdump`.
   - `gitea-backup` repo → the whole snapshot (database dump + `/data`, 10Gi PVC).
   - `immich-backup` repo → `/db/immich.pgdump`, the `.immich` marker files, and a random
     sample of original files (default 200 files, capped at 5GB), chosen from
     `restic ls --json` and passed with `--include-file`. The sample is drawn only from
     the `upload/` and `library/` folders, so it holds originals, not thumbnails,
     sidecars or Immich's own database backups.
4. Run `restic check --read-data-subset=N/52` on each repo, with
   N = ((ISO week − 1) mod 52) + 1, so every repo is fully read-verified once a year
   without a full download. (The modulo matters: restic rejects 53/52, and 2026 has a
   week 53.)
5. Write `READY` with the date, the three snapshot IDs and Gotify's ClusterIP. On any failure, post a Gotify ✗
   and write no marker.

### `drill-sso` — Keycloak and Gitea

One pod. Containers share localhost:

- **Postgres** — throwaway, data on `emptyDir`.
- **Loader** (init) — refuses to continue if `READY` is missing or not from today; creates
  two databases with generated passwords and loads both dumps with
  `pg_restore --no-owner --no-acl`.
- **Keycloak** — the upstream server image, started with `kc.sh start` against the local
  database, hostname `https://sso.<domain>`, plain HTTP behind the proxy below.
- **Gitea** — the upstream image, `/data` pointed at the restored copy, with `GITEA__…`
  environment overrides for: database on localhost, cache/session/queue in memory (no
  Valkey), and the switches listed under isolation.
- **TLS proxy** — terminates HTTPS on localhost with a certificate generated at pod start,
  and routes `sso.<domain>` → Keycloak and `gitea.<domain>` → Gitea.
  `hostAliases` point both names at `127.0.0.1`, and Gitea trusts the generated CA. This
  is what lets the restored login settings (issuer URL, callback URL, client secret — all
  stored in the two databases) work without being edited.
- **Driver** — the checks, then the Gotify report. Its exit code is the Job's result.

Checks, in order:

1. Keycloak is ready; the `homelab` realm's discovery document is served.
2. Database floors: Keycloak user count and Gitea repository count are each at or above a
   configured minimum (guards against a valid-but-empty backup).
3. **SSO login**: with a cookie jar, follow Gitea's "sign in with Keycloak" link, post the
   drill user's credentials to Keycloak's form, follow the redirect back, and confirm
   Gitea's API reports the session as that user. The drill sets Gitea's OAuth
   auto-registration on by environment override, so the first login creates the account
   in the throwaway copy instead of stopping at the link-account page.
4. Gitea data: the number of repositories on disk matches the database, and
   `git fsck` passes on the most recently pushed repository, which must have commits.
5. Version drift. Keycloak and Immich record their app version in the database, so the
   loader compares it with the drill's image before booting. Gitea records only a schema
   number, so there the signal is Gitea refusing to start on a newer database. Either
   way an image older than the data is reported as **"Restore drill: … stale — bump the
   image"**. *(My call: it is a failed Job at priority 8 with its own title. The drill
   proved nothing that week, so the 9-day alert should fire if it is left unfixed.)*

### `drill-immich`

One pod: Postgres (an image carrying the vector extensions the dump needs — Immich's own
Postgres image, major version 17), Valkey, the loader, the Immich server image, a driver.

Checks:

1. Dump loads cleanly; asset count is at or above a configured minimum.
2. Immich server boots on it (migrations run if the image is newer) and answers
   `/api/server/ping`.
3. **Every sampled file's SHA-1 equals the checksum stored for that asset in the restored
   database.** This ties the file backup and the database backup to each other. A
   sampled file with no asset row is skipped and counted; the check fails if fewer than
   a configured minimum (default 150 of 200) matched.
4. Version drift, as above.

The last step of this job empties the scratch PVC, pass or fail.

### Images and versions

The drill pins its own copies of the app images (Keycloak, Gitea, Immich server, Immich
Postgres, Valkey). Production's Keycloak version comes from the operator and Gitea's from
the chart, so nothing in git ties them together; Renovate keeps the drill's pins moving,
and check 5 catches the case where it has fallen behind. A newer drill image than the
data is fine — the app migrates the throwaway copy.

### Reporting

- Each boot job posts `Restore drill: sso ✓/✗` or `Restore drill: immich ✓/✗` to Gotify
  (priority 5 / 8), using the existing script conventions: in-cluster Gotify URL, log tail
  in the failure body, control characters stripped. The ✓ body lists the snapshot dates
  and the counts it found.
- The Gotify token comes from the `gotify-bootstrap` Job, like the other job tokens.
- A `VMRule` alerts if either boot job has not succeeded in 9 days, so a drill that
  silently stops running is noticed.
- CronJob conventions from `design/decisions/backups.md` apply: `backoffLimit: 0`,
  `restartPolicy: Never`, `concurrencyPolicy: Forbid`, `ttlSecondsAfterFinished: 86400`
  with the kube-linter annotation, plus an `activeDeadlineSeconds`.

## Prerequisite: `gitea-backup` cannot read Gitea's config directory

Found during review, verified on the live volume. `gitea-backup` runs as uid 65534, and
`/data/gitea` is mode `0700` owned by uid 1000 (123 of 2198 entries under `/data` are not
world-readable). The snapshot therefore will not contain `/data/gitea/conf/app.ini`,
which holds the keys Gitea encrypts its secrets with, and by the rule in
`design/decisions/backups.md` the job is expected to exit 3 nightly now that it mounts
the real volume. This is a production backup gap on its own, not only a drill problem.

Fix, as a separate change that lands first: run `gitea-backup` as uid and gid 1000 (the
changedetection precedent — the unreadable set is a whole directory tree, not one derived
path). The SSO drill depends on a snapshot taken after that fix.

## What changes in production

Besides the prerequisite above, the drill needs exactly these touches outside its own
namespace. Everything else is new files under `kubernetes/apps/restore-drill/`.

1. **A `restore-drill` user in the Keycloak `homelab` realm** — needs the owner. Created
   in the console: email set and verified, no required actions, member of the Gitea `user`
   group only. Its password is sealed into the `restore-drill` namespace. It has to exist
   in production so that it is inside the backup. It never signs in to production Gitea;
   in the drill it is auto-registered in the throwaway copy.
2. **Reflector annotations on the `restic-secret` and `rclone-secret` SealedSecret
   templates** in `gitea`, `postgres` and `immich`, allowing reflection into
   `restore-drill`. Metadata only, no re-seal. Because all three are named
   `restic-secret`, the drill namespace declares three differently named Secrets that
   each carry a `reflects:` annotation, instead of using auto-mirroring.
3. One entry in `gotify-bootstrap` for the drill's token.
4. One entry in `coverage-audit`'s `exemptions.json` for the scratch PVC, and the new
   namespace wired into `kubernetes/apps/kustomization.yml`.

## Docs

`design/decisions/backups.md` gains a restore-drill section (state and rules);
`design/runbook.md` gains "what to do when the drill fails" and loses its "untested"
notes for these three apps once the first drill passes; `design/TODO.md` drops the
`restore-drill` item; the routing list in `.claude/CLAUDE.md` gets `restore drill` added
to the backups line.

## Left to the builder

- Flux layout inside `kubernetes/apps/restore-drill/` (follow `design/docs/gitops.md`).
- Which TLS proxy, and how it binds 443 as non-root (or use a high port if the stored
  URLs allow it — they do not carry a port, so 443 is expected).
- Exact Keycloak start flags and Gitea environment overrides; whether Gitea's restored
  `app.ini` needs `INSTALL_LOCK`/secret keys left as they are (they must be: 2FA and
  token secrets are encrypted with them).
- Whether a Flux-applied Secret with a `reflects:` annotation is filled reliably by
  Reflector without Flux reverting it; fall back to separately sealed copies if not.
- The sample-picking logic for Immich within busybox/jq limits (`backup-tools` has no
  python3).
- Resource requests and limits, from the production values of the same apps.
- The minimum-count floors, set from the first real run.
- Confirm the OIDC discovery URL stored in Gitea's database is the public
  `https://sso.<domain>` one (the `hostAliases` approach depends on it).
- Build order: the `gitea-backup` fix and one good nightly snapshot; then `drill-restore`, run by hand; then `drill-sso`; then
  `drill-immich`. Each is run once by hand and its log read before the schedule is
  trusted.

## Known limits

- The scratch PVC lives on the Synology, like the data it is checking. That is fine for a
  drill: the source of the restore is Filen, not the Synology.
- The drill proves the latest snapshot. It does not prove older retention points.
- A pass on Immich proves the database and a sample, not every photo. Over a year the
  `restic check` slices read the whole repo once.
- The drill's read access to the repos is by convention, not enforced by Filen.
