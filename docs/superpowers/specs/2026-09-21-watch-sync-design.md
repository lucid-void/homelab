# Watch-state sync: Plex ↔ Jellyfin ↔ Trakt, with a Trakt→Seerr request queue

**Date:** 2026-09-21
**Status:** design, not yet implemented
**Namespace:** `media`

## Goal

Two things, which are separate problems sharing a toolchain:

1. **Watch history converges** across Plex, Jellyfin and Trakt. Watching an episode
   anywhere marks it watched everywhere, including the historical Trakt archive from
   Hobi/Showly.
2. **The Trakt watchlist becomes a work queue.** Items on it are requested in Seerr and
   then removed from the watchlist, so the list drains as it is fulfilled rather than
   growing forever.

Goal 2 is why the watchlist never touches Plex. See "Rejected: watchlist mirroring".

## Topology

```
Jellyfin  ←──[CrossWatch pair A]──→  Plex  ←──[CrossWatch pair B]──→  Trakt
                                                                        │
                                                        [trakt-seerr-queue]
                                                                        ↓
                                                            Seerr → Sonarr/Radarr
                                                                        ↓
                                                    item removed from Trakt watchlist
```

History is a **chain, not a mesh**, with Plex as the transit point. This is not a
preference — CrossWatch sync pairs are strictly one source → one destination, so a
three-way mesh is not expressible. The chain is the only shape available, and it has the
useful property of having no cycle: nothing can feed its own output back to itself.

Propagation is therefore two-hop for Jellyfin→Trakt. A Jellyfin watch reaches Trakt after
pair A then pair B have both run — worst case two sync intervals.

## Components

### 1. CrossWatch (`kubernetes/apps/media/crosswatch/`)

`ghcr.io/cenodude/crosswatch`, `bjw-s/app-template` 3.7.3, `replicas: 1`, config on an
`nfs-client` PVC. Web UI on port 8787.

Chosen over WatchState + PlexTraktSync (two mature tools) for three reasons:

1. **Credentials are encrypted at rest in `config.json`**, with the encryption key
   supplied by env var (`CW_CONFIG_KEY`, or `CROSSWATCH_CONFIG_KEY`) from a SealedSecret.
   The volume holds ciphertext; the key never touches it. Both alternatives store
   credentials in plaintext files they rewrite in place, which a read-only secret mount
   breaks — each would have needed an initContainer seed-copy workaround.
2. **Native OIDC support** (`api/authOidcAPI.py`), so the UI can sit behind Keycloak like
   the rest of the stack rather than being port-forward-only.
3. One tool covers all three services instead of two tools with two config patterns.

It also ships a `cw sync once` CLI with real exit codes (0 success, 3 unreachable, 6
busy) and a `--fail-on-unresolved` flag.

**Correction to an earlier draft of this spec:** a `CW_CFG__<section>__<key>` environment
override syntax is described in third-party write-ups but does **not** appear anywhere in
the source. Do not design around it. Provider credentials are entered once through the
UI and persisted encrypted; only `CW_CONFIG_KEY` and `CONFIG_BASE` are confirmed env
vars.

**Maturity, stated plainly:** v0.12.3, released 2026-09-16, with eight releases in the
preceding month. This is pre-1.0 software under heavy churn. Pin the tag, never
auto-merge a Renovate bump, and read the release notes before each one.

Two pairs:

| Pair | Source ↔ Dest | Syncs | Direction |
|---|---|---|---|
| A | Plex ↔ Jellyfin | history, progress | two-way |
| B | Plex ↔ Trakt | history | two-way (union) |

**Hard rule: neither pair may sync watchlists.** Pair B writing the Trakt watchlist
would fight `trakt-seerr-queue` directly — the queue removes a fulfilled item, CrossWatch
sees it still present in Plex's watchlist and re-adds it, and the item cycles forever
while Seerr collects duplicate requests. Watchlist sync is owned by the queue job alone.

Ratings and collections are out of scope; the answer was history only.

### 2. `trakt-seerr-queue` (`kubernetes/apps/media/trakt-seerr-queue/`)

CronJob, `*/30 * * * *`. No off-the-shelf tool does this — Seerr has no Trakt integration
(`seerr-team/seerr#1298`, open), and nothing removes from a source list after a
successful request.

Per run:

1. `GET https://api.trakt.tv/sync/watchlist/movies` and `/shows` (OAuth bearer).
2. For each item with an `ids.tmdb`, `POST /api/v1/request` to
   `http://seerr.media.svc.cluster.local:5055` with `X-Api-Key`, body
   `{mediaType, mediaId: <tmdb>, seasons: "all"}` for shows.
3. On HTTP 201 (accepted) **or 409 (already requested)**, `POST
   /sync/watchlist/remove` to Trakt for that item. 409 counts as success — the item is
   already in Seerr's pipeline, which is the condition we are draining on.
4. Anything without a TMDb id, or any other status, is left on the watchlist and logged.
   A stuck item is visible rather than silently dropped.

**Removal trigger is request-acceptance, not download-completion.** Chosen deliberately:
the watchlist drains immediately and stays far below Trakt's 100-item free-tier cap. The
accepted cost is that a request which later fails leaves nothing on the watchlist — but
it remains in Seerr's own request list, so it is not lost, only relocated.

Implementation notes that follow repo hard rules:

- **Image `python:3.13-alpine`, script uses only `urllib.request` and `json` from the
  stdlib.** No `apk add` at all, which sidesteps both the `timeout 300` rule and the
  run-as-root rule. The pod can run non-root with a read-only root filesystem.
- Script lives in a plain `ConfigMap` (`queue.py: |`) with the body **indented four
  spaces**, matching `kromgo-badge-push-script` — Python at 0-indent inside a block
  scalar breaks the kustomize parser.
- `concurrencyPolicy: Forbid`, `backoffLimit: 0`, `activeDeadlineSeconds: 300`,
  `ttlSecondsAfterFinished: 3600`, history limits 1/3 — same shape as the kromgo CronJob.

### 3. Jellyfin Trakt plugin — installed, intentionally left unauthorized

The plugin is already installed (`design/decisions/jellyfin-ui.md`). Authorizing it
closes the cycle Jellyfin→Trakt→Plex→Jellyfin. Watched status would still converge, since
it is effectively monotonic, but Trakt would accumulate duplicate scrobbles and inflated
rewatch counts from the same play arriving by two routes.

Jellyfin history reaches Trakt via the chain instead. This must be recorded in
`jellyfin-ui.md`, because "installed but deliberately not configured" is indistinguishable
from "forgot to configure it" six months from now.

## Config and secrets

`SealedSecret` per the repo rule (`kubeseal --cert kubernetes/flux/pub-cert.pem`), never a
raw `Secret`.

| Secret | Holds | Consumed by |
|---|---|---|
| `crosswatch-secrets` | Plex token, Jellyfin API key, Trakt client id/secret | CrossWatch, via `envFrom` |
| `trakt-queue-secrets` | Trakt client id/secret, Seerr API key | queue CronJob, via `envFrom` |

**Do not rely on `optional: true` on an app-template `envFrom`** — chart 3.7.3 strips it
silently, so a missing secret surfaces as a confusing runtime failure rather than a Flux
error.

### The Trakt OAuth refresh problem

Trakt access tokens expire (~3 months) and refresh rotates the refresh token. A sealed
secret is immutable, so the queue job cannot persist a rotated token back into it.

Resolution: a small `nfs-client` PVC `trakt-queue-state` (100Mi) holds
`token.json`. An initContainer copies the sealed seed in only if the file is absent
(`cp -n`); the script rewrites it on refresh. The seed goes stale after the first
rotation, which is correct and expected — it is only ever read into an empty volume.

Reusing CrossWatch's own stored Trakt token was considered and rejected: it couples the
queue job to CrossWatch's internal config format, which is not a stable interface.

### HTTPRoute behind Keycloak

CrossWatch has native OIDC (`api/authOidcAPI.py`, issuer configured in Settings), so the
UI gets an `HTTPRoute` at `crosswatch.blackcats.cc` with `parentRefs: [{name: shared,
namespace: gateway}]` and a Keycloak client, matching the rest of the stack. Never an
`Ingress`.

OIDC is enabled through the UI, not an env var — so the sequence is: deploy, port-forward,
configure OIDC, then add the route. The route must not land before OIDC is on, because
the UI holds live Plex and Trakt credentials.

Consequence, stated plainly: **sync pair definitions, provider logins and the OIDC
settings are all authored in that UI and live in `config.json` on the PVC — unowned
state, not git.** Credentials within it are encrypted, but the pair topology is not
reconstructible from the repo. This is a genuine concession against the repo's
declarative premise. Precedent exists (Jellyfin plugin settings, Suwayomi extension
repos, Kavita library settings), but it must be recorded as such, and every pair
definition written out in prose in the decision doc so it can be rebuilt by hand.

## Data safety: the first union sync

**This is the highest-risk step in the whole design.**

`plex-config-local` is an `openebs-hostpath` PVC on cp-1 with **no backup CronJob**
(`design/decisions/jellyfin.md`). The first Plex ↔ Trakt sync merges an eight-year Trakt
archive from Hobi/Showly into Plex. If that archive marks a show complete that was
actually abandoned, the error propagates into Plex and Jellyfin and there is no restore
path.

Required order:

1. Snapshot the Plex config PVC by hand. Not optional, and not recoverable if skipped.
2. Run pair B once with CrossWatch's dry-run/preview, and read the diff. Count how many
   items it wants to mark watched in Plex, and spot-check a handful against memory.
3. Only then enable the pair on a schedule.

Pair A (Plex ↔ Jellyfin) is lower-risk — both sides are already your own viewing — but
the snapshot covers it too, so do it first regardless.

## Phasing

Each phase is independently useful and independently verifiable. Stopping after any one
leaves a working system.

| Phase | Delivers | Est. |
|---|---|---|
| 1 | CrossWatch deployed, pair A live: Plex ↔ Jellyfin direct history | ~1h |
| 2 | Pair B live: Trakt archive merged, union both ways | ~1.5h |
| 3 | `trakt-seerr-queue`: watchlist → Seerr → auto-remove | ~2h |

## Verification

```bash
# Phase 1-2
mise exec -- kubectl get pods -n media -l app.kubernetes.io/name=crosswatch
mise exec -- kubectl logs -n media deploy/crosswatch --tail=50
# pick an episode watched on Jellyfin only, confirm it flips in Plex within one interval

# Phase 3
mise exec -- kubectl get cronjob -n media trakt-seerr-queue
mise exec -- kubectl create job -n media --from=cronjob/trakt-seerr-queue queue-manual
mise exec -- kubectl logs -n media job/queue-manual
# confirm: item requested in Seerr AND gone from the Trakt watchlist
```

Run `.agents/scripts/validate-manifests.sh kubernetes/apps/media/crosswatch` and the same
for `trakt-seerr-queue` before pushing — a schema violation reaching `main` wedges the
whole Flux Kustomization, not just the offending file.

## Flux wiring

Two new `ks.yml` files following the `seerr` pattern (`targetNamespace: media`,
`dependsOn: shared-gateway` only if a route is added — the CrossWatch one needs no such
dependency without an HTTPRoute), registered in
`kubernetes/apps/media/kustomization.yml`.

Image tags pinned to an exact release, never `latest`. CrossWatch is not a linuxserver.io
image, so the full-tag rule does not apply, but a Renovate entry is still wanted.

## Rejected alternatives

**Watchlist mirroring Trakt → Plex.** PlexTraktSync's one-way watchlist sync *deletes*
destination items absent from the source (`Taxel/PlexTraktSync#1872`), so it would have
wiped the existing Plex watchlist. It also fed Seerr only via Seerr's Plex-watchlist
auto-request, which is Plex-account-only and unavailable to local users. Going
Trakt → Seerr directly removes the deletion hazard, removes the dependence on Plex
accounts, and makes the 100-item cap a non-issue.

**WatchState + PlexTraktSync.** More mature (2,707 and 4,657 commits vs CrossWatch's
2,207), but two workloads, two credential stores, and two seed-copy workarounds for the
credential-rewrite problem CrossWatch solves natively.

**Full mesh, all three pairs.** Not expressible in CrossWatch, and invites conflicting
writes with no defined winner.

**Trakt VIP for an uncapped watchlist.** Unnecessary once the watchlist drains.

## Risks

| Risk | Mitigation |
|---|---|
| Bad historical Trakt data propagates into Plex | PVC snapshot + dry-run diff before enabling pair B |
| CrossWatch is the least-proven tool in the media stack | Additive-only sync; phase 1 is reversible; snapshot exists |
| Pair config is unowned UI state | Document the pair definitions in prose in the decision doc |
| Trakt token rotation | PVC-backed `token.json`, sealed seed used only when absent |
| CrossWatch watchlist sync switched on by accident | Recorded as a hard rule; it causes a remove/re-add loop |

## Docs to update on implementation

- New `design/decisions/watch-sync.md` — the implemented state, the two hard rules
  (no watchlist sync in CrossWatch; Jellyfin Trakt plugin stays unauthorized), and the
  pair definitions in prose.
- Row in `.claude/CLAUDE.md` routing table: "watch history sync, Trakt, the Seerr
  request queue" → `design/decisions/watch-sync.md`.
- `design/docs/services.md` — inventory entry for CrossWatch.
- `design/decisions/jellyfin-ui.md` — the Trakt plugin is installed *and deliberately
  unauthorized*.
- `design/TODO.md` — remove once done.

## Scheduling

Syncs run on CrossWatch's **own internal scheduler**, configured in the UI. `cw sync once`
is a client that talks to a running instance (exit code 3 is "cannot reach CrossWatch"),
so it is a manual/debug tool here, not the scheduling mechanism. No sync CronJob is
needed; the only CronJob in this design is `trakt-seerr-queue`.

## Open items

- Pin `ghcr.io/cenodude/crosswatch:v0.12.3` unless a newer tag exists at implementation
  time. Add a Renovate entry, but **never auto-merge** — 0.x, eight releases last month.
- Whether a pair can express "history only" with watchlist off, and whether that is a
  per-pair or global toggle. Verify in the UI **before the first run**, given the
  remove/re-add loop a watchlist pair causes.
- Whether CrossWatch offers a dry-run/preview of a pair before the first live run. If it
  does not, the Plex PVC snapshot becomes the only safety net for phase 2 and must not
  be skipped.
