# Watch-history sync: Plex ↔ Jellyfin

**Read before editing:** `kubernetes/apps/media/crosswatch/`,
`kubernetes/apps/keycloak/clients/app/crosswatch.yml`

## Why it exists

Watch history should converge across Plex and Jellyfin, so watching an episode on
either server marks it watched on both.

CrossWatch (`ghcr.io/cenodude/crosswatch`) does the syncing. It was chosen over
WatchState + PlexTraktSync (both more mature) for three reasons: its credentials are
encrypted at rest in `config.json` with the key supplied separately, so a read-only
secret mount doesn't fight it the way it fights tools that rewrite a plaintext
credentials file in place; it has native OIDC (`api/authOidcAPI.py`), so the UI can sit
behind Keycloak like the rest of the stack instead of being port-forward-only; and it
already supports the Plex/Jellyfin pair without extra tooling.

## Current state

### Topology — one pair

```
Jellyfin  ←──[CrossWatch: Plex ↔ Jellyfin]──→  Plex
```

Two-way, history and playback progress, watchlist sync off. This is the only ongoing
sync in this design — there is no Trakt leg and no chain.

### CrossWatch (`kubernetes/apps/media/crosswatch/`)

`bjw-s/app-template` HelmRelease, `replicas: 1`, `strategy: Recreate` (the RWO config
PVC holds a single JSON document that two replicas would interleave writes into and
corrupt). Config on an `nfs-client` PVC (`crosswatch-config`, 2Gi). Web UI on container
port 8787, exposed at `https://crosswatch.blackcats.cc` via `HTTPRoute`, behind Keycloak
OIDC (client `crosswatch`, `user` role — see
`kubernetes/apps/keycloak/clients/app/crosswatch.yml`).

**OIDC settings, entered in the CrossWatch UI** (Settings → Authentication) and stored
in `config.json` under `app_auth.oidc` — UI-only state, so this is its only record:

| Field | Value |
|---|---|
| Issuer | `https://sso.blackcats.cc/realms/homelab` |
| Client ID | `crosswatch` |
| Client secret | `kubectl get secret crosswatch-client-secret -n keycloak -o jsonpath='{.data.secret}' \| base64 -d` |
| Scopes | `openid profile email` |

**Keycloak is served at `sso.blackcats.cc`, not `keycloak.blackcats.cc`.** The latter has
no DNS record and resolves nowhere, so an issuer built from it fails in a way that hides
its own cause: CrossWatch *saves* the config to disk, then fails the link step with a
`NameResolutionError` behind a 502, and the UI redisplays the OIDC toggle as disabled.
That looks like "the setting won't persist" — but `app_auth.oidc.enabled` is `true` in
`config.json` the whole time. Check `kubectl logs deploy/crosswatch | grep OIDC` before
believing the toggle.

**Provider connections — the only record of this configuration.** Entered in the
CrossWatch UI under Settings → Connections, stored in `config.json` on the PVC (secrets
encrypted), which is not in git:

| Provider | Server URL | Auth |
|---|---|---|
| Plex | `https://plex.blackcats.cc:443` (as configured; `http://plex-app.media.svc.cluster.local:32400` also works) | Plex account link |
| Jellyfin | `http://jellyfin.media.svc.cluster.local:8096` | Jellyfin API key + user id |

**The Plex Service is `plex-app`, not `plex`.** `bjw-s/app-template` names the Service
after the controller, so `plex.media.svc.cluster.local` does not resolve and fails as
`Name or service not known` — which reads as a DNS or network fault rather than a wrong
name. `plex-direct` (the `pool-b` LoadBalancer at `172.16.20.51:32400`) also works but
leaves the cluster and back; prefer the ClusterIP.

**Sync pair definition — the only record of this configuration.** The pair is authored
through the CrossWatch web UI and lives in the same `config.json`. This prose is what
rebuilds it by hand if the PVC is ever lost:

- **Plex ↔ Jellyfin.** Two-way. Syncs **history and playback progress**. Watchlist sync
  **off**. Ratings and collections are out of scope.

The pair's id is **`pair_d2644b0b4e64`**. Both providers also carry
`strict_id_matching: true`, and history is `add`-only (`remove: false`) so a sync can
only ever mark something watched, never un-watch it.

Scheduled on CrossWatch's own scheduler: `mode: custom_interval`,
`custom_interval_minutes: 15`. **Do not use the "Every N hours" frequency for this** —
its field is hours, so a `15` meant as minutes becomes a 15-hour gap between syncs, which
looks like a broken sync rather than a slow one.

### Jellyfin Trakt plugin

Installed, deliberately left unauthorized. See `design/decisions/jellyfin-ui.md`.

### Historical Trakt data — one-time import, not a live integration

The Trakt watch-history archive was imported once from a `trakt.tv/settings/data`
export ("Export now", a ZIP of JSON files) using `.agents/scripts/trakt-export-to-plex.py`
(see "Migrating Trakt history" below). Trakt is not connected to CrossWatch, Plex,
Jellyfin, or anything else in this cluster on an ongoing basis — the import was a
one-time backfill, not a sync leg.

**Durable constraint:** as of 2026-07-30, creating a Trakt API app requires a paid VIP
account, and Trakt deleted existing free-account apps. This is why there is no ongoing
Trakt sync in this design — CrossWatch, like any Trakt integration, needs a Trakt API
app (`client_id`/`client_secret`) to talk to Trakt at all, and that path is no longer
available on a free account.

### Watchlist → request automation

Handled entirely by **Seerr's own native Plex-watchlist auto-request**, a setting inside
Seerr — not code in this repo. It is Plex-account-only: it works for users who log in to
Seerr with their Plex account, and is not available to local Seerr users, since it reads
the watchlist Plex maintains for that account.

## Rules

- **No CrossWatch pair may sync watchlists.** Seerr's native Plex-watchlist
  auto-request owns the watchlist: it reads and acts on Plex's own watchlist state. A
  CrossWatch pair syncing watchlists would write to that same watchlist from the
  Jellyfin side, fighting Seerr's auto-request behind its back — items would appear or
  vanish from the Plex watchlist for reasons Seerr didn't cause. Watchlist sync must
  stay off on the Plex ↔ Jellyfin pair. Confirm with `cw sync list` (below) whenever the
  pair is touched.
- **The Jellyfin Trakt plugin stays installed but unauthorized.** There is no Trakt
  integration anywhere in this design. Authorizing the plugin would create a direct
  Jellyfin→Trakt scrobble path that nothing here accounts for or manages — an unmanaged
  sync leg outside the one supported topology (Plex ↔ Jellyfin via CrossWatch). Never
  authorize this plugin.
- **Never auto-merge a CrossWatch image bump.** Pre-1.0, eight releases in the month
  before the tag currently pinned (see `design/docs/services.md` for what that tag is),
  under heavy churn. Pin the tag exactly and read the release notes before merging —
  see `.github/renovate.json`.
- **Never copy a CrossWatch version from its GitHub release into the image tag.** The
  releases are named `vX.Y.Z`; the ghcr tags are `X.Y.Z`, with no `v`. A v-prefixed tag
  does not exist and fails as `ImagePullBackOff` — `failed to resolve reference … not
  found` — which reads as a missing or private image rather than a malformed tag. Take
  the tag from the registry, not the release page:
  `curl -s "https://ghcr.io/token?scope=repository:cenodude/crosswatch:pull&service=ghcr.io"`
  then `GET https://ghcr.io/v2/cenodude/crosswatch/tags/list?n=1000` with that bearer
  token. Note the tag list pages at 100 by default and is unsorted, so a naive read
  shows `0.9.x` as newest — follow the `Link` header and sort numerically.
- **Never write a raw Secret for CrossWatch credentials** — same repo-wide rule, sealed
  with `kubeseal --cert kubernetes/flux/pub-cert.pem`.
- **Never rely on `optional: true` on CrossWatch's `envFrom`** — chart 3.7.3 strips it
  silently; the HelmRelease carries a comment noting this rather than the flag.

## Credentials — currently incomplete

**CrossWatch provider logins** (Plex, Jellyfin) are entered once through the CrossWatch
UI and persisted encrypted in `config.json`, using `crosswatch-config-key`
(`CW_CONFIG_KEY`, an env var from a SealedSecret) as the encryption key. **Losing that
key means re-entering every provider login** — the ciphertext on the PVC is unrecoverable
without it. This has not been done yet; no provider is logged in, and the Plex ↔
Jellyfin sync pair has not been created in the UI either.

## Migrating Trakt history

`.agents/scripts/trakt-export-to-plex.py` is a **one-time** migration script, not
ongoing tooling — it marks a Trakt export's watch history as watched in Plex, once,
before the owner leaves Trakt for good. CrossWatch's Plex ↔ Jellyfin pair then carries
the marks to Jellyfin, so the script only ever talks to Plex. Python 3 stdlib only, no
dependencies.

**Get the export:** `trakt.tv/settings/data` → "Export now" → download the ZIP.

**Safety:** Plex's config PVC has no backup CronJob (`design/docs/storage.md`), so a bad
write here has no way back. Dry-run is the default — nothing is written to Plex unless
`--apply` is passed. Matching is strictly by id (Plex's own metadata guid, then
imdb/tmdb/tvdb); there is no title fallback, since a wrong title match would silently
corrupt watch state.

```bash
# 1. Inspect the export first — no Plex contact, confirms the file shapes match what
#    the script expects. Works directly on the ZIP.
python3 .agents/scripts/trakt-export-to-plex.py --inspect ~/Downloads/trakt-export.zip

# 2. Dry run against Plex — prints matched/unmatched counts and samples, writes nothing.
export PLEX_TOKEN=...
python3 .agents/scripts/trakt-export-to-plex.py ~/Downloads/trakt-export.zip

# 3. Only once the dry-run summary looks right:
python3 .agents/scripts/trakt-export-to-plex.py ~/Downloads/trakt-export.zip --apply
```

Only `watched-history-*.json` (one entry per play, movies and episodes) is used for
matching — it's the only export file with per-episode detail. The deduplicated
`watched-movies.json` / `watched-shows-*.json` summaries and the watchlist files are
parsed and shown by `--inspect` but never written from.

### The run that happened

Run once against Plex on 2026-09-22. The export held 10,007 plays, deduplicating to 165
unique movies and 8,925 unique episodes. **3,711 items were marked watched (27 movies,
3,684 episodes), 0 failed.**

The remainder were not failures. The library holds 120 movies against 165 watched, so
most unmatched films were simply never owned. Every id type was checked independently
against Plex afterwards and each resolved the same 3,684 episodes — `tmdb` alone reaches
the same total as all four combined, so no id type recovers anything the others miss.
3,711 is the ceiling this library allows, not a matching shortfall.

Two things worth knowing if this is ever re-run:

- **Trakt's `ids.plex.guid` matches Plex's `guid` attribute, not its `Guid[]` array.**
  `Guid[]` carries only `imdb`/`tmdb`/`tvdb`, so the script's plex-id path never matches
  and its summary always reports `plex: 0`. Harmless — those items match on tmdb anyway,
  and correcting it would add exactly zero — but the zero is expected, not a symptom.
- **Scrobbling an already-watched item increments its `viewCount`.** Items watched in
  Plex before the import now read `viewCount: 2`. Watched state is correct; only play
  counts on the overlap are inflated by one. Re-running would inflate them further.

## The Trakt watchlist was not migrated

The Trakt export also carried a 302-item watchlist. It was **deliberately not migrated**,
and the seeding script was removed rather than kept around.

Ten items were seeded into the Plex watchlist before that decision; the rest were left
behind. Two reasons, both worth remembering before anyone rebuilds this:

- **Seerr's watchlist auto-request is armed** (`watchlistSyncMovies` and
  `watchlistSyncTv` are both set for the account). Every item added to the Plex
  watchlist becomes a request, and each TV entry is a whole series — so a bulk seed is a
  bulk download, not a bookmark import.
- **The list had gone stale.** Entries dated back to 2022, and included unreleased
  titles that Sonarr/Radarr would hold indefinitely.

New watchlist entries are made directly in Plex from now on, where Seerr picks them up
natively. There is no Trakt watchlist path in this design.

## Verify

```bash
mise exec -- kubectl get pods -n media -l app.kubernetes.io/name=crosswatch
# `cw` needs its own API token once app auth is on, and will refuse with
# "Unauthorized (GET /api/pairs)" until `cw --local auth token create` is run in
# the container. Reading config.json is the lower-friction check and needs no token:
mise exec -- kubectl exec -n media deploy/crosswatch -- sh -c 'cat /config/config.json' \
  | python3 -c 'import sys,json; p=json.load(sys.stdin)["pairs"][0]; \
print(p["id"], p["mode"], {k:v["enable"] for k,v in p["features"].items()})'
mise exec -- kubectl get keycloakoidcclient crosswatch -n keycloak \
  -o custom-columns='NAME:.metadata.name,ERRORS:.status.conditions[?(@.type=="HasErrors")].status'
curl -sf https://crosswatch.blackcats.cc && echo
```
