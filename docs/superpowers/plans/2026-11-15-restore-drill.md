# Restore Drill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly, isolated drill that restores the Keycloak, Gitea and Immich backups from Filen, boots the real apps on them, and reports pass or fail to Gotify.

**Architecture:** A new `restore-drill` namespace with three CronJobs sharing one scratch PVC. `drill-restore` (the only pod with internet) pulls from the restic repos and writes a marker file; `drill-sso` and `drill-immich` (Gotify-only egress) each boot a throwaway Postgres plus the real app images in one pod and run checks. No RBAC, no operators, no charts.

**Tech Stack:** Kubernetes CronJobs, FluxCD Kustomization, CiliumNetworkPolicy, restic over rclone (Filen), bash + jq + curl in `ghcr.io/lucid-void/backup-tools`, upstream Keycloak / Gitea / Immich / Postgres / Valkey images, Reflector, VictoriaMetrics `VMRule`.

**Spec:** `docs/superpowers/specs/2026-11-14-restore-drill-design.md` — read it first; this plan argues from it.

## Global Constraints

- Repo hard rules in `.claude/CLAUDE.md` apply to every task. The ones that bite here: never write a raw `Secret` with values (seal it); never read or print secret material (`kubectl describe secret` for key names is fine, `-o yaml|json` is not); never a rolling image tag; never set `spec.targetNamespace` on a Kustomization whose resources span namespaces; never `kubectl apply` config — the only imperative step allowed is `kubectl create job --from=cronjob/<name>` to run a drill by hand.
- After editing any manifest: `.agents/scripts/validate-manifests.sh <file>`, one file per call. A `SKIPPED:` line means not validated.
- All k8s tooling runs as `mise exec -- kubectl|flux|kubeseal|kubeconform`.
- Restic is read-only by behaviour: every restic call carries `--no-lock`; the scripts must not contain the words `backup`, `forget`, `prune`, `unlock` or `init` as restic subcommands; a repo that cannot be opened is a failure.
- Schedules (UTC, Saturday): `drill-restore` `0 10 * * 6`, `drill-sso` `0 12 * * 6`, `drill-immich` `0 13 * * 6`.
- CronJob conventions: `concurrencyPolicy: Forbid`, `backoffLimit: 0`, `restartPolicy: Never`, `ttlSecondsAfterFinished: 86400` with the `ignore-check.kube-linter.io/job-ttl-seconds-after-finished` annotation, `activeDeadlineSeconds` set, `automountServiceAccountToken: false`.
- Gotify: post to the in-cluster Service by **IP** from the marker file, port 80, path `/message`; priority 5 on pass, 8 on fail; failure body carries `tail -10` of the log with control characters stripped.
- Titles, exact: `Restore drill: restore ✗`, `Restore drill: sso ✓`, `Restore drill: sso ✗`, `Restore drill: sso stale — bump the image`, and the same three for `immich`.
- Snapshot freshness limit: 48 hours. Immich sample: 200 files, 5 GB cap, at least 150 must match. Scratch PVC: `restore-drill-scratch`, `nfs-client`, 30Gi, RWX.
- Every drill container runs as `1000:1000` with `fsGroup: 1000` (Gitea's and Keycloak's uid, so restored files are owned by the uid that reads them), except Postgres and Valkey, which keep their image's uid on `emptyDir`.
- Docs describe the implemented state and never record a deployed version number.

## Review Focus

1. **A stale marker.** Last week's `READY` is still on the PVC because this week's restore failed before wiping → the boot jobs must fail with "no restore from today", not test week-old data. Pinned by `test_marker_is_today` (Task 2) and the wipe-first ordering (Task 3).
2. **A Filen blip mid-restore.** restic exits non-zero half way → no `READY` is written and Gotify gets `Restore drill: restore ✗`. Pinned by the `trap` test in Task 3 Step 5.
3. **A valid-but-empty backup.** The dump loads and the app boots with zero users/repos/assets → fail on the floors. Pinned by `test_floor` (Task 2).
4. **The drill user cannot log in for a non-backup reason** (password rotated, required action added, account locked) → the failure body must say which page the login stopped on, not just "exit 1". Pinned in Task 5 Step 3.
5. **Week 53 and week 1.** `check_subset` must never produce `53/52` or `0/52`. Pinned by `test_check_subset` (Task 2).

---

### Task 0: Preconditions (owner + verification, no code)

- [ ] **Step 1: Confirm the Gitea backup fix is live.** PR #328 merged, and a `gitea-backup` Job run after the merge is `Complete`.

Run: `mise exec -- kubectl -n gitea get jobs`
Expected: a `gitea-backup-*` Job younger than the merge with `Complete 1/1`. If it failed, stop — the SSO drill has nothing valid to restore.

- [ ] **Step 2 (owner): create the drill user in Keycloak.** Console → realm `homelab` → Users → Add: username `restore-drill`, an email, *Email verified* on, no required actions, a non-temporary password; Groups → join the Gitea `user` group only.

- [ ] **Step 3 (owner): seal the credentials.** Write a Secret named `drill-user` in namespace `restore-drill` with keys `username` and `password` to a temp file, seal it per the repo rule into `kubernetes/apps/restore-drill/app/drill-user-sealed.yml`, delete the temp file. The implementer never sees the plaintext.

- [ ] **Step 4: Record the production image versions** the drill must pin (Task 4 and 6 use them):

```bash
mise exec -- kubectl -n keycloak get statefulset keycloak -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
mise exec -- kubectl -n gitea get deploy gitea -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
grep -n 'tag:' kubernetes/apps/immich/immich/app/helmrelease.yml
grep -n 'imageName' kubernetes/apps/postgres/cluster/app/cluster.yml
```

### Task 1: Namespace, isolation and credentials

**Files:**
- Create: `kubernetes/apps/restore-drill/kustomization.yml`, `namespace.yml`, `ks.yml`
- Create: `kubernetes/apps/restore-drill/app/kustomization.yml`, `networkpolicy.yml`, `pvc.yml`, `serviceaccount.yml`, `mirrored-secrets.yml`
- Modify: the `restic-sealed.yml` and `rclone-sealed.yml` under `kubernetes/apps/gitea/backup/app/`, `kubernetes/apps/postgres/backup/app/`, `kubernetes/apps/immich/backup/app/` (template annotations only)
- Modify: `kubernetes/apps/monitoring/gotify-bootstrap/app/bootstrap.sh` and `rbac.yml`
- Modify: `kubernetes/apps/monitoring/coverage-audit/app/exemptions.json`

Read first: `design/docs/gitops.md` (layout), `design/decisions/gotify.md` (token bootstrap), the Reflector rule in `design/decisions/backups.md`.

**Interfaces — Produces:**
- Namespace `restore-drill`; ServiceAccount `restore-drill` with `automountServiceAccountToken: false`.
- PVC `restore-drill-scratch`.
- Secrets in `restore-drill`: `restic-gitea`, `restic-postgres`, `restic-immich` (each reflecting `<ns>/restic-secret`, keys `RESTIC_REPOSITORY` and `RESTIC_PASSWORD`), `rclone-secret` (reflecting `gitea/rclone-secret`), `gotify-secret` (key `GOTIFY_TOKEN`, written by `gotify-bootstrap`), `drill-user` (from Task 0).
- Pod label contract for the policies: `drill/role: restore` or `drill/role: boot`.

- [ ] **Step 1: Write the Flux wiring.** `ks.yml` is one Flux Kustomization `restore-drill`, `dependsOn: [sealed-secrets]`, `path: ./kubernetes/apps/restore-drill/app`, `prune: true`, **no `targetNamespace`**; every resource carries `namespace: restore-drill` explicitly. Model on `kubernetes/apps/gitea/backup/ks.yml`.

- [ ] **Step 2: Write `networkpolicy.yml`** — three `CiliumNetworkPolicy` objects:
  - `default-deny`: `endpointSelector: {}`, empty `ingress` and `egress` lists (deny both ways).
  - `restore-egress`: selects `drill/role: restore`; egress to kube-dns (UDP/TCP 53, with a `dns` rule matching `*`), to `world` on TCP 443, and to the `gotify` endpoints in `monitoring` on TCP 80.
  - `boot-egress`: selects `drill/role: boot`; egress only to the `gotify` endpoints in `monitoring` on TCP 80.

  These are the first Cilium policies in the repo (`design/decisions/cilium-gateway.md` for the Cilium version in use). Find Gotify's pod labels with `mise exec -- kubectl -n monitoring get pods --show-labels | grep gotify`.

- [ ] **Step 3: Write `pvc.yml`, `serviceaccount.yml`, `mirrored-secrets.yml`.** Each mirrored Secret is declared empty with only the annotation `reflector.v1.k8s.emberstack.com/reflects: "<ns>/<name>"`. Add `kustomize.toolkit.fluxcd.io/ssa: Merge` so Flux does not strip the data Reflector fills in.

- [ ] **Step 4: Allow reflection at the sources.** On the six SealedSecret templates, add `reflection-allowed: "true"` and `reflection-allowed-namespaces: "restore-drill"` under `spec.template.metadata.annotations` (append to the existing list where one exists). No re-seal: template metadata is not encrypted.

- [ ] **Step 5: Gotify token and coverage exemption.** Add `ensure_app_token "restore-drill" "Restore drill results" restore-drill gotify-secret GOTIFY_TOKEN` to `bootstrap.sh` and the matching Role/RoleBinding for namespace `restore-drill` in `rbac.yml` (copy an existing namespace's pair). Add `"restore-drill/restore-drill-scratch": "scratch space for the restore drill, wiped every run"` to the `backup` block of `exemptions.json`.

- [ ] **Step 6: Validate every touched manifest** with `validate-manifests.sh`, one file per call. Expected: exit 0, no `SKIPPED:`.

- [ ] **Step 7: Commit, merge via PR, then verify on the cluster.**

```bash
mise exec -- flux get kustomization restore-drill
mise exec -- kubectl -n restore-drill get pvc,cnp
mise exec -- kubectl -n restore-drill describe secret restic-gitea restic-postgres restic-immich rclone-secret gotify-secret drill-user | grep -E '^Name:|bytes'
```

Expected: Kustomization `Ready True`; PVC `Bound`; three policies; every Secret lists its keys with non-zero byte counts. If a mirrored Secret stays empty, replace it with a separately sealed copy (needs the owner) and note it in `design/decisions/backups.md`.

### Task 2: Shared script library with tests

**Files:**
- Create: `kubernetes/apps/restore-drill/app/scripts/lib.sh`
- Test: `kubernetes/apps/restore-drill/tests/test_lib.sh` (plain bash, exits non-zero on the first failed assertion; run as `bash kubernetes/apps/restore-drill/tests/test_lib.sh`)

Scripts are real files mounted through a kustomize `configMapGenerator` named `drill-scripts` (precedent: `kubernetes/apps/monitoring/gotify-bootstrap/app/`). `backup-tools` has bash, curl, jq, busybox `find`/`sha1sum`, no python3.

**Interfaces — Produces** (all in `lib.sh`, sourced by every drill script):
- `check_subset <iso_week>` → prints `N/52`, `N = ((week − 1) mod 52) + 1`.
- `snapshot_fresh <snapshot_time_iso8601> <now_epoch>` → exit 0 if under 48 h old, else 1.
- `marker_is_today <marker_file> <today YYYY-MM-DD>` → exit 0 only if the file exists and its `DRILL_DATE` equals today.
- `floor <label> <actual> <minimum>` → exit 0 if `actual >= minimum`; otherwise prints `<label>: <actual> is below the floor of <minimum>` and exits 1.
- `pick_sample <count> <max_bytes>` → reads `restic ls --json` lines on stdin, prints up to `count` paths of type `file` under `/library/upload/` or `/library/library/`, shuffled, stopping before the running size total exceeds `max_bytes`.
- `version_older <image_version> <data_version>` → exit 0 if the image is older than the data (compare dotted numeric versions, ignoring a leading `v`).
- `notify_gotify <ip> <title> <message> <priority>` and `fail_body <logfile>` (the escaped `tail -10`, with `gsub(/[[:cntrl:]]/, " ")`) — copy the behaviour from `kubernetes/apps/gitea/backup/app/script-configmap.yml`.
- Marker file format, `/scratch/READY`, shell-sourceable: `DRILL_DATE`, `GOTIFY_IP`, `SNAP_KEYCLOAK`, `SNAP_GITEA`, `SNAP_IMMICH` (snapshot IDs), `SNAPTIME_KEYCLOAK`, `SNAPTIME_GITEA`, `SNAPTIME_IMMICH`.

- [ ] **Step 1: Write the failing tests** in `test_lib.sh`:

```bash
# test_check_subset
[[ $(check_subset 1)  == "1/52"  ]]
[[ $(check_subset 52) == "52/52" ]]
[[ $(check_subset 53) == "1/52"  ]]
# test_snapshot_fresh  (now = 2026-11-21T10:00:00Z = 1795255200)
snapshot_fresh "2026-11-20T05:00:10Z" 1795255200
! snapshot_fresh "2026-11-19T09:59:00Z" 1795255200
# test_marker_is_today
printf 'DRILL_DATE=2026-11-21\n' > "$T/READY"
marker_is_today "$T/READY" 2026-11-21
! marker_is_today "$T/READY" 2026-11-28
! marker_is_today "$T/missing" 2026-11-21
# test_floor
floor users 12 5
! floor users 0 1
[[ $(floor repos 0 3 || true) == "repos: 0 is below the floor of 3" ]]
# test_pick_sample: 3 files of 10 bytes under /library/upload, 1 under /library/thumbs, 1 dir
[[ $(pick_sample 200 25 < "$T/ls.json" | wc -l) -eq 2 ]]          # size cap
! pick_sample 200 1000 < "$T/ls.json" | grep -q thumbs             # folder filter
# test_version_older
version_older 26.3.1 26.4.0
! version_older v3.3.5 3.3.5
! version_older 26.4.0 26.3.9
# test_fail_body: a log line containing a tab and a double quote yields valid JSON
printf 'a\tb "c"\n' > "$T/log"; jq -e . <<<"{\"m\":\"$(fail_body "$T/log")\"}" >/dev/null
```

- [ ] **Step 2: Run it.** Expected: FAIL, `lib.sh: No such file` / `command not found`.
- [ ] **Step 3: Implement the functions in `lib.sh`.**
- [ ] **Step 4: Run it.** Expected: exit 0, prints `ok` per test.
- [ ] **Step 5: Commit.**

### Task 3: `drill-restore`

**Files:**
- Create: `kubernetes/apps/restore-drill/app/scripts/restore.sh`, `kubernetes/apps/restore-drill/app/cronjob-restore.yml`
- Modify: `kubernetes/apps/restore-drill/app/kustomization.yml` (resources + `configMapGenerator`)
- Test: `kubernetes/apps/restore-drill/tests/test_restore.sh`

**Interfaces:**
- Consumes: `lib.sh`; Secrets from Task 1, mounted as files at `/secrets/gitea`, `/secrets/postgres`, `/secrets/immich` (use `RESTIC_REPOSITORY_FILE` and `RESTIC_PASSWORD_FILE`), `rclone-secret` at `/rclone-config`.
- Produces on the PVC (mounted at `/scratch`):
  - `/scratch/keycloak/db/keycloak.pgdump`
  - `/scratch/gitea/db/gitea.pgdump` and `/scratch/gitea/data/…`
  - `/scratch/immich/db/immich.pgdump`, `/scratch/immich/library/…` (sampled originals plus every `.immich` marker file), `/scratch/immich/sample.txt` (one snapshot path per line)
  - `/scratch/READY` in the Task 2 format.

Pod: label `drill/role: restore`, image `backup-tools` at the tag the backup CronJobs use, `HOME=/tmp`, `activeDeadlineSeconds: 7200`.

- [ ] **Step 1: Write the failing tests** in `test_restore.sh`. They run `restore.sh` with `PATH` prefixed by a directory of stub `restic`, `getent` and `curl` scripts that log their arguments:
  - `test_readonly`: after a full stubbed run, every logged restic call contains `--no-lock`, and no call's subcommand is `backup`, `forget`, `prune`, `unlock` or `init`.
  - `test_wipes_first`: a pre-existing `/scratch/READY` is gone before the first restic call is logged.
  - `test_stale_snapshot_fails`: the stub reports a snapshot 3 days old → exit non-zero, no `READY`, curl stub received title `Restore drill: restore ✗` with priority 8.
  - `test_restic_failure_writes_no_marker`: the stub exits 1 on the second `restore` → exit non-zero, no `READY`, ✗ sent.
  - `test_marker_contents`: on success `READY` holds all eight keys and `GOTIFY_IP` is the address the `getent` stub returned.
- [ ] **Step 2: Run it.** Expected: FAIL (no `restore.sh`).
- [ ] **Step 3: Implement `restore.sh`** in the order the spec gives: wipe → resolve `gotify.monitoring.svc.cluster.local` to an IP → per repo `restic snapshots --latest 1 --json` and `snapshot_fresh` → restores (`--include /db/keycloak.pgdump`; whole Gitea snapshot; Immich dump, `--include '**/.immich'`, and `pick_sample 200 5368709120` fed to `--include-file`) → `restic check --read-data-subset=$(check_subset $(date +%V))` per repo → write `READY` last. `trap` on EXIT sends ✗ with `fail_body` on any non-zero exit. No ✓ message — the boot jobs report success.
- [ ] **Step 4: Run the tests.** Expected: exit 0.
- [ ] **Step 5: Write `cronjob-restore.yml`, validate, commit, merge via PR.**
- [ ] **Step 6: Run it once by hand and read the log.**

```bash
mise exec -- kubectl -n restore-drill create job --from=cronjob/drill-restore drill-restore-manual-1
mise exec -- kubectl -n restore-drill logs -f job/drill-restore-manual-1
```

Expected: `Complete`; the log shows three snapshot dates under 48 h old, three restores, three `check` passes, `READY written`. Confirm with a second look that the snapshot count in each repo is unchanged (`restic snapshots` in the next nightly backup log). If a repo fails to open with `rclone-secret` mirrored from `gitea`, mirror that namespace's own `rclone-secret` under a distinct name instead.

### Task 4: `drill-sso` boot stack

**Files:**
- Create: `kubernetes/apps/restore-drill/app/cronjob-sso.yml`, `scripts/load-sso.sh`, `scripts/proxy-sso.Caddyfile` (or the equivalent for the proxy chosen)
- Modify: `kubernetes/apps/restore-drill/app/kustomization.yml`

**Interfaces:**
- Consumes: the PVC layout and `READY` from Task 3; `marker_is_today`, `version_older` from Task 2.
- Produces, inside the pod: Postgres on `127.0.0.1:5432` with databases `keycloak` and `gitea`; Keycloak answering `https://sso.example.com`; Gitea answering `https://gitea.example.com`; the generated CA at `/tls/ca.crt`; a shared `emptyDir` `/run/drill` in which the loader writes `db.env` (generated passwords) and may write `STALE` (one line: which app, image version, data version).

Pod spec decisions:
- Label `drill/role: boot`; `hostAliases: 127.0.0.1 → sso.example.com, gitea.example.com`; pod sysctl `net.ipv4.ip_unprivileged_port_start=0` so the proxy binds 443 as non-root; `activeDeadlineSeconds: 2700`.
- Everything except the driver is a **native sidecar** (`initContainers` with `restartPolicy: Always`) so the Job ends when the driver exits, in this start order with a `startupProbe` each: `postgres` → `load-sso` (ordinary init container) → `proxy` → `keycloak` → `gitea`.
- `postgres`: an image of the same Postgres major as production, pinned to a full tag, data on `emptyDir`.
- `load-sso.sh`: `marker_is_today /scratch/READY "$(date -u +%F)"` or exit 1; create both databases; `pg_restore --no-owner --no-acl`; compare `SELECT version FROM migration_model ORDER BY update_time DESC LIMIT 1` with `KEYCLOAK_IMAGE_VERSION` via `version_older` and write `STALE` if older.
- `keycloak`: the image recorded in Task 0, `kc.sh start` with `--db=postgres`, `--db-url=jdbc:postgresql://127.0.0.1:5432/keycloak`, `--hostname=https://sso.example.com`, `--http-enabled=true`, `--proxy-headers=xforwarded`, `--health-enabled=true`. No SMTP sidecar, no truststore.
- `gitea`: the image recorded in Task 0; the restored tree mounted so the image sees it at its data path; `SSL_CERT_FILE` pointing at a bundle that includes `/tls/ca.crt`; overrides `GITEA__database__HOST=127.0.0.1:5432`, `GITEA__database__PASSWD` from `db.env`, `GITEA__cache__ADAPTER=memory`, `GITEA__session__PROVIDER=memory`, `GITEA__queue__TYPE=level`, `GITEA__mailer__ENABLED=false`, `GITEA__mirror__ENABLED=false`, `GITEA__webhook__ALLOWED_HOST_LIST=loopback`, `GITEA__cron__ENABLED=false`, `GITEA__oauth2_client__ENABLE_AUTO_REGISTRATION=true`. Do **not** override `SECRET_KEY`, `INTERNAL_TOKEN` or the JWT secrets — they come from the restored `app.ini`.

- [ ] **Step 1: Write the manifest and the loader**, with the driver container as a placeholder that only waits for both readiness URLs and exits 0:

```bash
curl -fsS --cacert /tls/ca.crt https://sso.example.com/realms/homelab/.well-known/openid-configuration | jq -e '.issuer == "https://sso.example.com/realms/homelab"'
curl -fsS --cacert /tls/ca.crt https://gitea.example.com/api/healthz | jq -e '.status == "pass"'
```

- [ ] **Step 2: Validate, commit, merge via PR.**
- [ ] **Step 3: Run by hand after a fresh `drill-restore` run** (`kubectl create job --from=cronjob/drill-sso drill-sso-manual-1`) and read every container's log. Expected: Job `Complete`; both `jq -e` checks print `true`.
- [ ] **Step 4: Prove the isolation on that same pod's logs.** Expected: no successful connection to `postgres-rw.postgres`, the mail bridge or Valkey in any container log, and Gitea's log shows the database host as `127.0.0.1`. Then confirm the login URL the restored Gitea uses is the public one:

Run (while a manual pod is up): `mise exec -- kubectl -n restore-drill exec <pod> -c postgres -- psql -U postgres gitea -Atc "select name, cfg::json->>'OpenIDConnectAutoDiscoveryURL' from login_source"`
Expected: `https://sso.example.com/realms/homelab/.well-known/openid-configuration`. If it is an in-cluster URL instead, add that hostname to `hostAliases` and the proxy.

### Task 5: `drill-sso` checks and report

**Files:**
- Create: `kubernetes/apps/restore-drill/app/scripts/drive-sso.sh`
- Modify: `kubernetes/apps/restore-drill/app/cronjob-sso.yml` (driver container, env `MIN_KEYCLOAK_USERS=1`, `MIN_GITEA_REPOS=1`)
- Test: `kubernetes/apps/restore-drill/tests/test_drive_sso.sh`

**Interfaces — Consumes:** Task 4's endpoints, `/run/drill/db.env`, `/run/drill/STALE`, `drill-user` Secret as files at `/secrets/drill-user/{username,password}`, `gotify-secret`, `GOTIFY_IP` from `READY`, `floor`/`notify_gotify`/`fail_body` from Task 2.

Check order in `drive-sso.sh` (the spec's five checks): stale short-circuit → Keycloak discovery → floors → SSO login → Gitea data.
- Stale: if `/run/drill/STALE` exists, or Gitea's log contains its "database version is newer" refusal, send `Restore drill: sso stale — bump the image` at priority 8 and exit 3.
- Floors: `SELECT count(*) FROM user_entity u JOIN realm r ON r.id = u.realm_id WHERE r.name = 'homelab'` against `MIN_KEYCLOAK_USERS`; `SELECT count(*) FROM repository` against `MIN_GITEA_REPOS`.
- Login, with one curl cookie jar and `--cacert /tls/ca.crt`: GET `https://gitea.example.com/user/oauth2/Keycloak` following redirects to the Keycloak form → extract the form's `action` URL → POST `username`/`password` to it following redirects → GET `https://gitea.example.com/api/v1/user` and require HTTP 200 with a non-empty `.login`.
- Gitea data: the number of repositories on disk equals the `repository` row count, and `git fsck` passes on a recently used repository that has commits. `backup-tools` has no `git`, so the git half runs in an ordinary init container `fsck` that uses the Gitea image and needs no database: it counts the `*.git` directories (excluding `*.wiki.git`), picks the most recently modified one that has refs, runs `git fsck` and `git rev-list --count --all` on it, and writes `REPOS_ON_DISK`, `FSCK_REPO`, `FSCK_RC` and `FSCK_COMMITS` to `/run/drill/fsck.env`. The driver then requires `REPOS_ON_DISK` to equal `SELECT count(*) FROM repository`, `FSCK_RC=0` and `FSCK_COMMITS >= 1`. Add the `fsck` init container to `cronjob-sso.yml` in this task.
- Pass body: the three snapshot times that apply, user count, repo count, the repository fsck ran on.

- [ ] **Step 1: Write the failing tests** with a stub `curl` and `psql` on `PATH`:
  - `test_stale_wins`: `STALE` present → exit 3, title `Restore drill: sso stale — bump the image`, no login attempted.
  - `test_floor_fails`: psql stub returns `0` users → exit non-zero, body contains `is below the floor`.
  - `test_login_failure_names_the_page`: the POST stub returns Keycloak's login page again → exit non-zero and the body contains `login stopped at:` followed by the final URL and the page's `<title>` (Review Focus 4).
  - `test_pass_message`: all stubs succeed → exit 0, title `Restore drill: sso ✓`, priority 5, body contains both counts.
- [ ] **Step 2: Run.** Expected: FAIL (no `drive-sso.sh`).
- [ ] **Step 3: Implement `drive-sso.sh`.**
- [ ] **Step 4: Run the tests.** Expected: exit 0.
- [ ] **Step 5: Validate, commit, merge via PR. Run restore then sso by hand.** Expected: Gotify shows `Restore drill: sso ✓` with real counts. Then set `MIN_KEYCLOAK_USERS` and `MIN_GITEA_REPOS` to about half the counts just reported and commit.
- [ ] **Step 6: Prove it can fail.** Run `drill-sso` by hand on a day with no fresh restore (or after the scratch wipe). Expected: Job `Failed`, Gotify `Restore drill: sso ✗` mentioning the marker.

### Task 6: `drill-immich`

**Files:**
- Create: `kubernetes/apps/restore-drill/app/cronjob-immich.yml`, `scripts/load-immich.sh`, `scripts/drive-immich.sh`
- Modify: `kubernetes/apps/restore-drill/app/kustomization.yml`
- Test: `kubernetes/apps/restore-drill/tests/test_drive_immich.sh`

Read first: `design/decisions/immich.md` (extensions, schema pinning notes).

**Interfaces — Consumes:** `/scratch/immich/…` and `READY` from Task 3; Task 2 functions.

Pod: label `drill/role: boot`, `activeDeadlineSeconds: 2700`, native sidecars in order `postgres` → `load-immich` (init) → `valkey` → `immich-server`; main container `driver`.
- `postgres`: Immich's own Postgres image, major 17, carrying the vector extension version production runs (check with `SELECT extname, extversion FROM pg_extension` on the production read replica), full tag pinned, the same `shared_preload_libraries` as `kubernetes/apps/postgres/cluster/app/cluster.yml`.
- `load-immich.sh`: marker check; create database `immich`; create the extensions the dump expects before `pg_restore --no-owner --no-acl`; write `STALE` if `version_older "$IMMICH_IMAGE_VERSION" "$(SELECT version FROM version_history ORDER BY "createdAt" DESC LIMIT 1)"`.
- `immich-server`: the tag from `kubernetes/apps/immich/immich/app/helmrelease.yml`, `DB_HOSTNAME=127.0.0.1`, `REDIS_HOSTNAME=127.0.0.1`, the restored `/scratch/immich/library` mounted at the path production mounts `immich-library`, `IMMICH_MACHINE_LEARNING_ENABLED=false`, no `IMMICH_CONFIG_FILE`. No machine-learning container.
- Before writing the driver, confirm on the loaded database the asset table's name, that `checksum` is the SHA-1 as `bytea`, and the prefix of `"originalPath"`, so the snapshot path `/library/<x>` maps to the right `originalPath`.

`drive-immich.sh` checks: stale short-circuit → asset count against `MIN_IMMICH_ASSETS=1` → `curl -fsS http://127.0.0.1:2283/api/server/ping` returns `{"res":"pong"}` → for each line of `sample.txt`, `sha1sum` the restored file and compare with `encode(checksum, 'hex')` of the row whose `originalPath` matches; a file with no row counts as skipped; a mismatch fails immediately; fewer than `MIN_SAMPLE_MATCHED=150` matches fails. **Last, pass or fail (in the EXIT trap): empty `/scratch`.**

- [ ] **Step 1: Write the failing tests** (stub `psql`, `curl`, `sha1sum`):
  - `test_mismatch_fails`: one sampled file's digest differs → exit non-zero, body names the file.
  - `test_too_few_matched`: 200 sampled, 120 have rows → exit non-zero, body contains `120 of 200 matched, need 150`.
  - `test_skips_unmatched`: 200 sampled, 160 have rows and match → exit 0, body contains `160 of 200 matched`.
  - `test_wipes_scratch_on_failure`: after `test_mismatch_fails`, the scratch directory is empty.
  - `test_stale`: `STALE` present → exit 3, title `Restore drill: immich stale — bump the image`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement both scripts and the manifest.**
- [ ] **Step 4: Run the tests.** Expected: exit 0.
- [ ] **Step 5: Validate, commit, merge via PR. Run restore → sso → immich by hand.** Expected: Gotify `Restore drill: immich ✓` with the asset count and `N of 200 matched`; afterwards `mise exec -- kubectl -n restore-drill get pods` shows nothing running, and the next `drill-restore` log starts from an empty volume. Raise `MIN_IMMICH_ASSETS` to about half the reported count and commit.

### Task 7: Alert, Renovate and docs

**Files:**
- Create: `kubernetes/apps/restore-drill/app/vmrule.yml` (namespace per `design/decisions/monitoring.md`)
- Modify: `.github/renovate.json`
- Modify: `design/decisions/backups.md`, `design/runbook.md`, `design/TODO.md`, `.claude/CLAUDE.md`

- [ ] **Step 1: Write the `VMRule`** `RestoreDrillNotPassing`: `time() - kube_cronjob_status_last_successful_time{namespace="restore-drill", cronjob=~"drill-(sso|immich)"} > 9 * 86400`, severity and labels matching the existing rules in `kubernetes/apps/monitoring/vm-stack/app/vmrules.yml`. Validate.

Run after merge: query the expression without the `> …` in VictoriaMetrics. Expected: two series with values under 604800.

- [ ] **Step 2: Renovate.** Make sure the five pinned drill images are picked up (read `design/decisions/images.md` for the regex managers), and add a `packageRules` group so the drill's Immich server tag moves in the same PR as `kubernetes/apps/immich/immich/app/helmrelease.yml`. Keycloak and Gitea drill pins get the label used for manual-review bumps.

Run: `npx --yes renovate-config-validator .github/renovate.json`. Expected: `Config validated successfully`.

- [ ] **Step 3: Docs.**
  - `design/decisions/backups.md`: a "Restore drill" part under *Current state* (three jobs, schedule, what each proves, what it does not) and rules: read-only restic by behaviour; boot pods have no DNS and get Gotify's IP from the marker; "stale" means bump the drill image, not a bad backup; never give the namespace RBAC or a production PVC; the floors and where to raise them.
  - `design/runbook.md`: a "The restore drill failed" section keyed by Gotify title (restore ✗ / sso ✗ / immich ✗ / stale), each with the first command to run; remove the "Untested" notes for these three apps only.
  - `design/TODO.md`: delete the `restore-drill` item; in "Restore-test one backup end to end" note that the drill now covers restore-to-scratch for Keycloak, Gitea and Immich, and that an in-place restore and etcd are still untested.
  - `.claude/CLAUDE.md`: add `restore drill` to the backups line of the routing list.
- [ ] **Step 4: Commit, merge via PR.**
- [ ] **Step 5: Let the first scheduled Saturday run happen and read all three results.** Expected: two ✓ messages in Gotify shortly after 12:00 and 13:00 UTC, no ✗.
