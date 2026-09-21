# Watch-history sync: Plex ↔ Jellyfin ↔ Trakt

**Read before editing:** `kubernetes/apps/media/crosswatch/`,
`kubernetes/apps/media/trakt-seerr-queue/`, `kubernetes/apps/keycloak/clients/app/crosswatch.yml`

## Why it exists

Two separate problems sharing a toolchain:

1. **Watch history converges** across Plex, Jellyfin and the historical Trakt archive
   (from Hobi/Showly) so watching an episode anywhere marks it watched everywhere.
2. **The Trakt watchlist becomes a work queue.** An item added to it gets requested in
   Seerr and then removed, so the list drains as it is fulfilled instead of growing
   forever and hitting Trakt's 100-item free-tier cap.

CrossWatch (`ghcr.io/cenodude/crosswatch`) does the syncing; `trakt-seerr-queue` does the
draining. They are independent workloads sharing only the Trakt account.

CrossWatch was chosen over WatchState + PlexTraktSync (both more mature) for three
reasons: its credentials are encrypted at rest in `config.json` with the key supplied
separately, so a read-only secret mount doesn't fight it the way it fights tools that
rewrite a plaintext credentials file in place; it has native OIDC
(`api/authOidcAPI.py`), so the UI can sit behind Keycloak like the rest of the stack
instead of being port-forward-only; and it's one tool covering all three services
instead of two tools with two config patterns and two seed-copy workarounds.

## Current state

### Topology — a chain, not a mesh

```
Jellyfin  ←──[CrossWatch pair A]──→  Plex  ←──[CrossWatch pair B]──→  Trakt
                                                                        │
                                                        [trakt-seerr-queue]
                                                                        ↓
                                                            Seerr → Sonarr/Radarr
                                                                        ↓
                                                    item removed from Trakt watchlist
```

This shape is not a preference. CrossWatch sync pairs are strictly one source → one
destination, so a three-way mesh cannot be expressed — a chain through Plex as the
transit point is the only shape available. It has the useful property of having no
cycle: nothing can feed its own output back to itself. The cost is that a Jellyfin
watch reaches Trakt only after two hops (pair A, then pair B) — worst case, two sync
intervals.

### CrossWatch (`kubernetes/apps/media/crosswatch/`)

`bjw-s/app-template` HelmRelease, `replicas: 1`, `strategy: Recreate` (the RWO config
PVC holds a single JSON document that two replicas would interleave writes into and
corrupt). Config on an `nfs-client` PVC (`crosswatch-config`, 2Gi). Web UI on container
port 8787, exposed at `https://crosswatch.blackcats.cc` via `HTTPRoute`, behind Keycloak
OIDC (client `crosswatch`, `user` role — see
`kubernetes/apps/keycloak/clients/app/crosswatch.yml`).

**Sync pair definitions — the only record of this configuration.** Pairs are authored
through the CrossWatch web UI and live in `config.json` on the PVC, which is not in
git. This prose is what rebuilds them by hand if the PVC is ever lost:

- **Pair A — Jellyfin ↔ Plex.** Two-way. Syncs **history and playback progress**.
  Watchlist sync **off**.
- **Pair B — Plex ↔ Trakt.** Two-way (union — an item watched on either side is
  watched on both, nothing is deleted). Syncs **history only**. Watchlist sync **off**.
  Ratings and collections are out of scope on both pairs.

CrossWatch assigns each configured pair an id (e.g. `pair_07c3`) only once it exists in
the UI — this has not happened yet, and no id is recorded here for that reason. Once
the pairs are configured, capture their ids with `cw sync list` (see Verify) and add
them here.

### `trakt-seerr-queue` (`kubernetes/apps/media/trakt-seerr-queue/`)

CronJob, `*/30 * * * *`, `concurrencyPolicy: Forbid`. No off-the-shelf tool does this —
Seerr has no Trakt integration (`seerr-team/seerr#1298`, open) and nothing removes an
item from a source list once it's been acted on.

Per run: `GET /sync/watchlist/movies` and `/shows` from Trakt; for each item carrying a
TMDb id, `POST /api/v1/request` to Seerr. On HTTP 201 (accepted) **or 409 (already
requested)**, remove the item from the Trakt watchlist — 409 counts as success because
the item is already in Seerr's pipeline, which is the condition being drained on.
Anything without a TMDb id, or any other response, is left on the watchlist and logged,
so a stuck item stays visible instead of silently vanishing.

**Removal fires on request-acceptance, not download-completion.** This is deliberate:
the watchlist drains immediately and stays well under the 100-item cap. The accepted
cost is that a request which later fails leaves nothing on the watchlist — but the item
is still in Seerr's own request list, so it's relocated, not lost.

Runs `python:3.13-alpine` with a stdlib-only script (`urllib.request`, `json`) mounted
from a plain ConfigMap — no `apk add`, so it needs neither the `timeout 300` rule nor
the run-as-root rule, and the pod runs non-root with a read-only root filesystem.

**Token persistence deviates from the design spec.** The spec called for an
initContainer to seed `token.json` from the sealed secret into the `trakt-queue-state`
PVC. What's actually implemented has no initContainer: `queue.py` itself tries
`/state/token.json` first and falls back to `TRAKT_ACCESS_TOKEN` /
`TRAKT_REFRESH_TOKEN` from the env when that file doesn't exist yet, writing the
rotated pair back to the PVC on every refresh. Net effect is the same — the sealed seed
is only ever read once, into an empty volume, and goes stale (correctly) after the
first rotation — but there's no initContainer in the manifest to look for.

### Jellyfin Trakt plugin

Installed, deliberately left unauthorized. See `design/decisions/jellyfin-ui.md`.

## Rules

- **No CrossWatch pair may sync watchlists.** A watchlist pair would fight
  `trakt-seerr-queue` directly: the queue removes a fulfilled item, CrossWatch sees it's
  still present on the other side's watchlist and re-adds it, and the item cycles
  forever while Seerr collects duplicate requests. Watchlist sync is owned by the queue
  job alone, on both Pair A and Pair B. Confirm with `cw sync list` (below) whenever a
  pair is touched.
- **The Jellyfin Trakt plugin stays installed but unauthorized.** Authorizing it closes
  the cycle Jellyfin→Trakt→Plex→Jellyfin: pair B already carries Jellyfin's watch
  history to Trakt via Plex, so a direct Jellyfin→Trakt scrobble is a second route for
  the same play, and Trakt ends up with duplicate scrobbles and inflated rewatch counts.
  Jellyfin history reaches Trakt through the chain instead — never authorize this
  plugin.
- **Never auto-merge a CrossWatch image bump.** Pre-1.0 (`v0.12.3`, eight releases in
  the month before it), under heavy churn. Pin the tag exactly and read the release
  notes before merging — see `.github/renovate.json`.
- **Never write a raw Secret for CrossWatch or queue credentials** — same repo-wide
  rule, sealed with `kubeseal --cert kubernetes/flux/pub-cert.pem`.
- **Never rely on `optional: true` on either workload's `envFrom`** — chart 3.7.3 strips
  it silently; both HelmRelease/CronJob manifests carry a comment noting this rather
  than the flag.

## Credentials — currently incomplete

Two credential stores exist and both are still placeholders as of this writing:

1. **CrossWatch provider logins** (Plex, Jellyfin, Trakt) are entered once through the
   CrossWatch UI and persisted encrypted in `config.json`, using
   `crosswatch-config-key` (`CW_CONFIG_KEY`, an env var from a SealedSecret) as the
   encryption key. **Losing that key means re-entering every provider login** — the
   ciphertext on the PVC is unrecoverable without it. This has not been done yet; no
   provider is logged in.
2. **`trakt-seerr-queue`'s secret** —
   `kubernetes/apps/media/trakt-seerr-queue/app/secrets-sealed.yml` — currently seals
   `REPLACE_ME_*` placeholder values (Trakt client id/secret, Trakt access/refresh
   token, Seerr API key). **The CronJob cannot succeed until this is re-sealed with real
   values.** Once real Trakt OAuth tokens (reuse the Trakt app created during setup) and
   a Seerr API key are in hand, edit
   `kubernetes/apps/media/trakt-seerr-queue/app/trakt-queue-secret.yml` in place and
   re-seal:

   ```bash
   mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
     < kubernetes/apps/media/trakt-seerr-queue/app/trakt-queue-secret.yml \
     > kubernetes/apps/media/trakt-seerr-queue/app/secrets-sealed.yml
   ```

   Then commit `secrets-sealed.yml`.

Until both of the above are done, neither CrossWatch pair can actually sync and the
queue CronJob will fail every run.

## Verify

```bash
mise exec -- kubectl get pods -n media -l app.kubernetes.io/name=crosswatch
mise exec -- kubectl get cronjob -n media trakt-seerr-queue
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync list   # confirm no pair has watchlist sync on; record pair ids here once configured
mise exec -- kubectl get keycloakoidcclient crosswatch -n keycloak \
  -o custom-columns='NAME:.metadata.name,ERRORS:.status.conditions[?(@.type=="HasErrors")].status'
curl -sf https://crosswatch.blackcats.cc && echo
```
