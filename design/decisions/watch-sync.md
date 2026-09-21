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

**Sync pair definition — the only record of this configuration.** The pair is authored
through the CrossWatch web UI and lives in `config.json` on the PVC, which is not in
git. This prose is what rebuilds it by hand if the PVC is ever lost:

- **Plex ↔ Jellyfin.** Two-way. Syncs **history and playback progress**. Watchlist sync
  **off**. Ratings and collections are out of scope.

CrossWatch assigns the configured pair an id (e.g. `pair_07c3`) only once it exists in
the UI — this has not happened yet, and no id is recorded here for that reason. Once the
pair is configured, capture its id with `cw sync list` (see Verify) and add it here.

### Jellyfin Trakt plugin

Installed, deliberately left unauthorized. See `design/decisions/jellyfin-ui.md`.

### Historical Trakt data — one-time import, not a live integration

The Trakt watch-history archive (from Hobi/Showly) was imported once from a
`trakt.tv/settings/data` export ("Export now", a ZIP of JSON files), via a separate
throwaway script outside this repo. Trakt is not connected to CrossWatch, Plex, Jellyfin,
or anything else in this cluster on an ongoing basis — the import was a one-time
backfill, not a sync leg.

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

## Verify

```bash
mise exec -- kubectl get pods -n media -l app.kubernetes.io/name=crosswatch
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync list   # confirm watchlist sync is off; record the pair id here once configured
mise exec -- kubectl get keycloakoidcclient crosswatch -n keycloak \
  -o custom-columns='NAME:.metadata.name,ERRORS:.status.conditions[?(@.type=="HasErrors")].status'
curl -sf https://crosswatch.blackcats.cc && echo
```
