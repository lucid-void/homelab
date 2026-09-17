# Media and manga stacks

**Read before editing:** `kubernetes/apps/media/` (excluding `plex/`, `romm/`, `minecraft*/`, which keep their own decision files)

## Current state

**Media stack.** sonarr/radarr/prowlarr/sabnzbd/seerr in the `media` namespace, all
`bjw-s/app-template`. Linuxserver images run `PUID=2202`/`PGID=2200`. Seerr has no
linuxserver image — it uses `ghcr.io/seerr-team/seerr` with pod `securityContext`
(`runAsUser`/`runAsGroup`/`fsGroup`) instead of PUID/PGID env vars. Shared `media-nfs`
RWX PVC for the Synology Media share; per-service `nfs-client` PVC for config.

**Manga stack.** Suwayomi-Server (downloader) + Kavita (reader) in `media`, both
`app-template`, writing/reading `media-nfs` subPath `Manga`. Single `app-template`
HelmRelease: controller `app` (`ghcr.io/suwayomi/suwayomi-server`, port `4567`,
embedded H2 DB — no CNPG) + controller `flaresolverr`
(`ghcr.io/flaresolverr/flaresolverr`, service `suwayomi-flaresolverr:8191`); services
are `suwayomi-app`/`suwayomi-flaresolverr` (HTTPRoute → `suwayomi-app:4567`). Config via
env: `BIND_PORT`, `DOWNLOAD_AS_CBZ=true`, `AUTH_MODE=none`,
`FLARESOLVERR_ENABLED`/`FLARESOLVERR_URL` — see `server-reference.conf` upstream. Runs
as uid 2202/gid 2200 (the image `chmod 777`s `/home/suwayomi` so arbitrary UIDs work).
Data dir `/home/suwayomi/.local/share/Tachidesk` is on the `suwayomi-config`
nfs-client PVC; downloads are a nested mount — `media-nfs` subPath `Manga` mounted at
`…/Tachidesk/downloads` so CBZs land on the Synology share for Kavita. No auth of its
own (VPN-gated like the rest of the media stack). Suwayomi ships with no extension repos
(legal reasons); `EXTENSION_REPOS` seeds the Keiyoushi repo
(`["https://github.com/keiyoushi/extensions/tree/repo"]`) so sources are installable —
installing a source and adding a manga is runtime app-state, done in the web UI, not
GitOps. For licensed English titles use a source like ComicK/Bato/WeebCentral
(Keiyoushi), not MangaDex (licensed-empty).

Kavita's first admin is created by an idempotent `kavita-bootstrap` Job
(POST `/api/account/register`; first user auto-becomes admin; creds from
`kavita-admin-secret` SealedSecret; HTTP 400 = admin already exists, treated as
success).

**Kavita OIDC.** Kavita reads OIDC creds only from `/config/appsettings.json` under key
`OpenIdConnectSettings` (`Authority`+`ClientId`+`Secret`, all three required for
`Enabled`) — it does not bind env vars and manages this file itself. Callback URI:
`https://kavita.blackcats.cc/signin-oidc` (ASP.NET middleware const). Wiring: Terraform
registers the Zitadel app and writes flat `kavita-oidc-secret`
(`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`) in `media`; an `alpine`+`jq` initContainer
(`oidc-config`) idempotently merges `{Authority,ClientId,Secret}` into
appsettings.json on each boot (`Authority` static). app-template initContainer env is
the raw k8s array schema and only allows `value`/`fieldRef`/`resourceFieldRef` (no
`secretKeyRef`), and `secretRef.optional` is stripped, so the secret is injected via
non-optional `envFrom` — the pod waits in `CreateContainerConfigError` until
`zitadel-bootstrap` writes it, then self-heals. `reloader.stakater.com/auto` restarts
Kavita on credential rotation.

OIDC account provisioning is a DB setting, not appsettings.json/env —
`provisionAccounts`/`requireVerifiedEmail`/`defaultRoles`/`defaultLibraries` default
off/empty. `kavita-bootstrap` configures them via `POST /api/settings`:
`provisionAccounts=true`, `requireVerifiedEmail=false`, `defaultRoles=["Login"]`
(non-admin reader — `Login` is the minimum to authenticate), `defaultLibraries=<all>`,
`defaultAgeRestriction=-1` (NotApplicable) + `defaultIncludeUnknowns=true`. `UpdateSettings`
writes `Authority`/`ClientId`/`Secret` back to appsettings.json and the GET masks the
secret, so the Job re-injects the real `OIDC_CLIENT_SECRET` from `kavita-oidc-secret` in
the POST.

## Rules

- **Do not deploy Tranga, Kaizoku or mangal** — Tranga was tried and removed: its
  4-connector set (WeebCentral/MangaDex/AsuraComic/Mangaworld) cannot reliably source
  licensed English titles (e.g. MangaDex is licensed-empty for some, WeebCentral's
  image-URL extraction breaks upstream). Kaizoku and mangal are archived upstream.
  Suwayomi's Tachiyomi/Mihon extension ecosystem gives far broader coverage.
- **Keep `defaultAgeRestriction=-1` and `defaultIncludeUnknowns=true` on Kavita** —
  Suwayomi's manga carry no age-rating metadata (→ `Unknown`), and Kavita's own defaults
  (`Unknown`/`includeUnknowns=false`) filter all such content out, leaving non-admin
  users an empty library. Admins bypass age restrictions regardless.
- **Always re-inject the real `OIDC_CLIENT_SECRET` when calling Kavita's
  `POST /api/settings`** — the preceding GET returns a masked secret, and posting that
  back verbatim breaks OIDC.

## Verify

```bash
mise exec -- kubectl get deployments -n media
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=kavita -o jsonpath='{.items[*].status.phase}'
```
