# Gotify

**Read before editing:** `kubernetes/apps/monitoring/gotify/`, `kubernetes/apps/monitoring/gotify-bootstrap/`, `kubernetes/apps/monitoring/gotify-telegram/`

## Current state

Deployed in `monitoring` at `gotify.blackcats.cc` (`gotify/server`, SQLite on an
nfs-client PVC, admin credentials in `gotify-admin-secret` SealedSecret).

**Token provisioning** is the `gotify-bootstrap` Job: creates app/client tokens through
the Gotify REST API and writes plain k8s Secrets into each namespace (`gotify-secret`
with key `GOTIFY_TOKEN`); the client token lands in `monitoring/gotify-client-secret`.
Gotify 3 discloses tokens only at creation/rotation — `GET /application` and
`GET /client` blank the `token` field — so the Job treats the destination **Secret as
the source of truth**: reuse the stored token when the app still exists, else
`POST /application` (missing app) or `PUT /application/{id}/security`
`{"regenerateToken":true}` (missing Secret). Rotation needs an elevated session, and
HTTP Basic auth satisfies elevation (`auth.RequireElevatedClient`), so no
`/client/{id}/elevate` step is needed. There is no client-token rotation endpoint
(`PUT /client/{id}` returns only the public prefix) — a lost client token means DELETE +
re-POST; created with `expiresAfterInactivitySeconds: 0` to opt out of v3's automatic
inactive-client cleanup. v2 env vars are unchanged
(`GOTIFY_SERVER_PORT`, `GOTIFY_DATABASE_*`, `GOTIFY_DEFAULTUSER_*`) — only list/map
values changed syntax in v3, and none are used here. Idempotent, so it can be re-run
after a DB reset to refresh all tokens.

It re-runs itself roughly hourly (`ttlSecondsAfterFinished: 3600` + 30m Kustomization
interval: the TTL deletes the finished Job, Flux recreates it), so a deleted Secret or a
wiped Gotify DB self-heals within the hour. The script lives in `app/bootstrap.sh`,
delivered by `configMapGenerator` (hash-suffixed name, deliberately **not**
`options.immutable` — the hash already prevents in-place revision edits, and the flag
would only block an emergency in-place patch), and runs on `backup-tools` (no
start-time `apk`), non-root with a read-only rootfs.

**Duplicate detection.** Two applications with one name means one of them is an orphan
holding a token nothing provisions or reads. The script cannot repair it — Gotify blanks
the token on GET, so nothing can tell which of the two the Secret belongs to — so it
counts the name matches, reports them as `DUPE:` and notifies at priority 8. Reported on
*every* run kind, unlike drift: adding an entry to the token list explains a created
app, never two apps with one name. The cause was `id=$(curl ... ) || true` around the
`GET /application`: a swallowed listing failure read as "app does not exist", so the run
POSTed a second application and overwrote the Secret with its token, orphaning the
first — and every later run then matched the orphan by name and "reused" the stored
token, so it stayed invisible. Both listings now fail the Job instead.

**Drift reporting.** The script hashes itself and stores that hash in the
`monitoring/gotify-bootstrap-state` ConfigMap: a `created`/`rotated` outcome on an
*unchanged* hash is real drift, logged as `DRIFT:` and pushed to Gotify at priority 8
via its own `gotify-bootstrap` app token in `monitoring/gotify-bootstrap-secret`; the
same outcome after the token list changed prints as expected. The Gotify message is the
durable record — the TTL deletes the pod's logs an hour later.
`activeDeadlineSeconds: 600` bounds the otherwise-unbounded `until curl .../health`
wait.

**gotify-telegram bridge**, in `monitoring/gotify-telegram`: a Python WebSocket bridge
that consumes `/stream?token=CLIENT_TOKEN` and forwards to the Telegram Bot API. Pip
deps are installed with `pip install --target /tmp/pylib` +
`PYTHONPATH=/tmp/pylib`, because uid `65534` (nobody) cannot write to `/.local`.

## Rules

- **Every consumer but one reads its token from a Secret; Jellyfin cannot.** The
  `jellyfin` token is provisioned into `media/jellyfin-gotify-secret` like any other, but
  its consumer is a *plugin*, and the Webhook plugin has no Secret indirection — the
  token is pasted into its config on Jellyfin's config PVC. So a rotation never reaches
  it: Jellyfin keeps posting a dead token and Gotify answers `401`, silently. Rotation
  only fires when that Secret loses its key, which is exactly what the drift report
  names — so **a `DRIFT:` line mentioning `jellyfin` means "go re-paste the token into
  the plugin"**, not "already repaired" as it does for every other entry.
- **Never let a Gotify listing call fail soft** — `curl ... || true` around
  `GET /application` or `GET /client` turns a transient blip into a permanent duplicate
  application, because an empty id is indistinguishable from "does not exist" and the
  create branch runs.
- **Never re-add a "look up the token each run" path** — Gotify 3 blanks tokens on GET,
  so that pattern creates a duplicate application every run; the destination Secret must
  stay the source of truth.
- **Run `gotify-telegram` with `python -u`** — otherwise its output is buffered and
  never appears in `kubectl logs`.

## Verify

```bash
mise exec -- kubectl get job gotify-bootstrap -n monitoring
mise exec -- kubectl get configmap gotify-bootstrap-state -n monitoring -o yaml  # lastRunDupes should be 0
mise exec -- kubectl logs -n monitoring deploy/gotify-telegram
```
