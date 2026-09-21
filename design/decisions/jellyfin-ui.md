# Jellyfin — plugins and theme

**Read before editing:** nothing in git. Everything here is applied through the Jellyfin
web UI and lives only on the config PVC, so this file is the rebuild script. It was first
run on 2026-09-21. The server deployment is `design/decisions/jellyfin.md`.

**Installed:** SSO Authentication, File Transformation, Plugin Pages, Home Screen
Sections, Media Bar, Jellyfin Enhanced, Intro Skipper, Trakt. **Not yet: Webhook**, and
no theme — Custom CSS is unset.

## Plugins

Almost nothing here is from the official catalogue — the same class of unowned state as
Suwayomi's extension repos or Kavita's library settings.

### Repositories to add

Four URLs under *Dashboard → Plugins → Repositories* cover every plugin below:

| Repository | Serves |
|---|---|
| `https://www.iamparadox.dev/jellyfin/plugins/manifest.json` | File Transformation, Plugin Pages, Home Screen Sections, Media Bar |
| `https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/manifest.json` | Jellyfin Enhanced |
| `https://raw.githubusercontent.com/Flowfin/jellyfin-plugin-sso/manifest-beta/manifest.json` | SSO Authentication |
| `https://manifest.intro-skipper.org/manifest.json` | Intro Skipper |

The intro-skipper URL serves the manifest **only** to a caller sending a
`User-Agent: Jellyfin-Server/<version>` header, redirecting everything else to the org's
GitHub page — opening it in a browser looks like a dead link, and is not. It serves a
different manifest per server line (10.x vs 12.x), so there is no URL to pin.

### Install order matters

Most of these rewrite `jellyfin-web` at serve time, routed through one plugin.
**Install File Transformation first.** Home Screen Sections and Media Bar installed
ahead of their dependencies do not error — they install, report healthy and render
nothing, which reads as "the plugin is broken" rather than "a dependency is missing".

1. **File Transformation** — rewrites served web content without touching files on disk.
   Media Bar 3.x requires **File Transformation 3.x specifically**.
2. **Plugin Pages** — required by Home Screen Sections.

That is the whole dependency set. **JavaScript Injector** and **Custom Tabs** appear in
most guides for this stack, but were only ever needed by KefinTweaks and are not installed
— see the rejection note below.

### The plugins

| Plugin | Why | Notes |
|---|---|---|
| **SSO Authentication** | Keycloak OIDC | Required. A fork — see below. |
| **Jellyfin Enhanced** | Keyboard shortcuts, subtitle styling, TMDB/user reviews, Seerr integration, media calendar, Sonarr/Radarr download monitoring | From v11 it supports **10.11+ and 12.x only**. File Transformation is "highly recommended" and avoids injection permission problems. |
| **Home Screen Sections** | Server-provided custom rows on the home screen | Needs **3.0.2.0** on 12.x. Requires File Transformation *and* Plugin Pages. |
| **Media Bar** | Featured-content hero carousel on the home screen | Needs **3.0.0.0**, which covers both the classic Desktop layout and the new React/MUI one. |
| **Intro Skipper** | Skip intros/credits | Requires server 12.0+; build must match the exact server release. |
| **Webhook** | → Gotify, matching the cluster's alerting path | Official catalogue. |

Optional, from the official catalogue: **Playback Reporting** (watch statistics), **Trakt**
(scrobbling — installed, deliberately unauthorized, see below), **Open Subtitles** if
subtitle fetching is wanted. TMDb and TheTVDB providers ship built in — do not install
them.

**Never authorize the Trakt plugin.** There is no Trakt integration anywhere in this
design — watch history sync is Plex ↔ Jellyfin only, via CrossWatch
(`design/decisions/watch-sync.md`). Authorizing it opens a direct Jellyfin→Trakt scrobble
path that nothing in this repo manages or accounts for. Left installed because removing it
would only invite reinstalling it later; left unauthorized because "installed but not
configured" reads identically to an oversight six months from now, and this one is
deliberate.

### KefinTweaks — considered and rejected

Evaluated and **not installed**, on two independent grounds.

**Its v12 support is incomplete by its own account.** The v0.4.10 notes (2026-09-09) put
v12 support behind the older **'Desktop (legacy)' UI**; its README is staler still. Taking
it means putting every user on the legacy UI to suit one plugin — while Media Bar handles
the modern layout natively. The cost lands on the whole install.

**It is not a plugin and has no manifest** — a JavaScript bundle pulled from jsDelivr at
`@latest` through the JavaScript Injector plugin, running in every browser that loads the
UI, unpinned and invisible to Renovate. A sharper supply-chain edge than anything else
here, next to an auth path already leaning on a forked SSO plugin.

Revisit when the legacy-UI caveat is gone. Dropping it also drops **JavaScript Injector**
and **Custom Tabs**, which existed only to serve it; its features overlap Jellyfin
Enhanced, which is installed and supports 12.x.

### Version coupling is the standing hazard

Every plugin above is compiled against, or patches, a specific server version, and fails
silently rather than erroring. Home Screen Sections **3.0.1.0 was pulled** for breaking on
**12.1** — precisely the line pinned here. Hence the `major-update` Renovate label: before
merging a jellyfin bump, check SSO Authentication, File Transformation, Home Screen
Sections, Media Bar and Intro Skipper all ship a build for the target version. SSO first —
it turns cosmetic breakage into a lockout.

### The SSO plugin is a fork, and it is on the auth path

`9p4/jellyfin-plugin-sso`, which every guide still points at, **was archived 2026-05-12
and does not load on 10.11 or newer**. The successor is a community revival published as
`Flowfin`/`kernicek`; one URL serves both 10.11 and 12.0.

A revived third-party fork therefore mediates every login. The local Jellyfin admin is the
break-glass path and must keep a real password — do not delete it after SSO works.


## Settings

Applied 2026-09-21. Every value below lives in the plugin's XML on the config PVC and in
nothing else, so this section is the half of the rebuild script that the install list
above does not cover.

### Configure them over the API, not by clicking

`POST /Plugins/<guid>/Configuration` with the whole config object does exactly what the
dashboard's Save button does, and it is reproducible. Three things make it sharp:

- **Only one auth header works on 12.x.** `Authorization: MediaBrowser Token="<key>"`.
  The `X-Emby-Token` header and the `?api_key=` query parameter both **401** — they were
  dropped, so a script written against any pre-12 guide fails looking like a bad key.
- **Never round-trip the SSO plugin's config through the API.** `GET` returns `OidSecret`
  as **`null`** — the plugin masks it — so reading the config and posting it back writes
  `null` over the encrypted client secret and locks out every SSO user. The SSO provider
  form is UI-only. Everything else here round-trips cleanly.
- **A POST replaces the whole object**, so always GET first and mutate, never post a
  partial config.

The plugin GUID is in `meta.json` in each plugin's directory under
`/config/data/plugins/<Name>_<version>/`.

### Enabling Home Screen Sections requires a restart

Flipping `Enabled` at runtime leaves the plugin half-alive: it registers no section
handlers, and `GET /HomeScreen/Sections?userId=<id>` then **blocks forever** instead of
erroring. Worse, the stuck request appears to hold per-user state, so every later request
for that user queues behind it — which reads as "every section is broken" when bisecting.
The same call with no `userId` answers instantly the whole time, which is the tell.

Restart the pod after enabling. Both users answered in under a tenth of a second
afterwards.

### What is set

| Plugin | Settings |
|---|---|
| **Home Screen Sections** | Enabled, lazy load on, ten sections (below), Movies/Shows set as the default libraries, Sonarr and Radarr wired, dates `DD/MM/YYYY` |
| **Intro Skipper** | `UseFileTransformationPlugin` **on**, `ProcessThreads` 2 × `MaxParallelism` 2 at `BelowNormal`, commercial scanning off, `ReanalyzeSettledSeasons` on, auto-skip off |
| **Jellyfin Enhanced** | Quality/language/rating tags, coloured ratings, metadata icons, watch progress, file sizes, release dates, reviews; Bookmarks / Activity Feed / Calendar / Downloads pages via Plugin Pages; active streams (own only); arr links |
| **Media Bar** | Shuffle 15 s, 25 items (12 films / 13 shows), preload 2, page backdrop synced, trailers on |
| **File Transformation** | Stock. Its `Transformations` list reads empty in config because other plugins register theirs at runtime — that is not a broken install. |

Home screen row order: My Media, Continue Watching / Next Up, Recently Added Movies,
Recently Added Shows, Because You Watched, Watch Again, My List, Collections, Upcoming
Shows, Upcoming Movies. Ten rows against `NumSectionsPerPage` 10. Music, book, audiobook,
music-video and Live TV sections are all left off — there are no such libraries, only
**Movies** and **Shows** — and the Discover / My Requests sections are off because they
need Jellyseerr.

**`BecauseYouWatched` will not appear until there is local play history** to anchor it.
Nine of ten rows rendering is the healthy state on a fresh library, not a misconfiguration.

### Media Bar's `-1` means "use the built-in default"

Every numeric in its `WebConfig` ships as `-1`, which is a sentinel, not a value. The real
defaults and the accepted ranges are compiled into the plugin — the settings above are
deliberate departures from them, so do not read them as the defaults written out longhand.

### Wiring Sonarr and Radarr without leaking cluster-internal URLs

Home Screen Sections calls the arrs **server-side**, so it takes the cluster-internal
service URLs directly.

Jellyfin Enhanced is the awkward one: `SonarrInstances` / `RadarrInstances` are **JSON
strings**, each holding `Name`, `Url`, `ApiKey`, `Enabled` and `UrlMappings`, and the same
`Url` is used both server-side *and* as the href of a link the browser follows. The
`UrlMappings` field resolves that, and it is keyed on **the Jellyfin URL the browser is
using**, not on the arr's address:

```
https://jellyfin.blackcats.cc|https://sonarr.blackcats.cc
```

So `Url` stays internal for the server's own calls, and anyone browsing through the
Gateway gets a link to the public host. Getting this backwards yields links to
`*.svc.cluster.local` that only resolve inside the cluster.

The API keys are read out of each arr's own `config.xml` on its PVC, and they come to rest
in Jellyfin's plugin config on **its** PVC — unowned state in two places, git in neither.

### Plugins auto-update, deliberately

The built-in **Update Plugins** task carries a startup trigger *and* a 24-hour interval
trigger, so plugins move on their own — the SSO plugin upgraded itself mid-session during
a routine pod restart. This is left on by choice, but it interacts badly with the version
coupling above: **a pod restart is when plugin upgrades land**, so a restart is never
purely a restart. After one, check that SSO still redirects before assuming the cluster is
healthy:

```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://jellyfin.blackcats.cc/sso/OID/start/keycloak
```

A **302 carrying a `request_uri=urn:ietf:params:oauth:request_uri:...`** is the proof that
matters: the plugin pushed the authorization request to Keycloak, which it can only do
with a readable discovery document and a valid client secret. Anything else is a lockout
in progress, and the local admin is how you get back in.


## Theme

**Abyss** (`AumGupta/abyss-jellyfin`).

Jellyfin 12's React/MUI migration broke essentially the whole community theme scene: the
curated `awesome-jellyfin` list now hides every theme it used to carry — Ultrachromic,
Zesty, Finity, Flow, scyfin, Catppuccin, NetFin, Jamfin — as "may not work anymore",
leaving Abyss, Jellyfish and ElegantFin.

Abyss was chosen because it is tested against **12.1.x specifically** and styles **both**
the modern React/MUI interface and Desktop (Legacy) from one stylesheet, so it survives a
Display Mode change instead of forcing one. CSS variables declared after the import cover
accent colour, corner radius and fonts; it ships a "Lite mode" for weak clients.

Not chosen: **Jellyfish** (n00bcodr), a close second, sharing an author with Jellyfin
Enhanced; **ElegantFin**, which on v12 needs an un-upstreamed fork
(`mihaif7/elegantfin-jf12`).

### Install it inline, not as an `@import`

The documented install is a rolling CDN reference:

```css
@import url("https://cdn.jsdelivr.net/gh/AumGupta/abyss-jellyfin@main/abyss.css");
```

That `@main` is the unpinned-CDN shape KefinTweaks was rejected over, so it gets the same
treatment: **fetch `abyss.css` once and paste its contents** into *Dashboard → General →
Custom CSS* (per-user overrides live in each user's Display settings), ~68 KB. Re-paste to
update, deliberately. A stylesheet can restyle and hide but not execute, so it is a
smaller hazard than KefinTweaks' JS — but the pin costs nothing.

**Pasting inline does not eliminate the external fetches.** Wherever the CSS lives it
pulls two sub-resources:

- `fonts.googleapis.com` — Google Sans, unpinned, a request to Google from every client
  that loads the UI.
- `cdn.jsdelivr.net/npm/material-icons` — the Material Icons Round webfont, version-pinned.

Delete those two rules (the leading `@import` and the `@font-face` block) for zero
external requests; icons fall back to what Jellyfin ships, type to the system stack.

### Gotchas

- **Custom CSS never reaches `/dashboard` on Jellyfin 12.** The admin dashboard is a
  separate React app that does not load it, so admin pages stay stock forever. Upstream
  behaviour, not a broken theme.
- **The theme must match Display Mode** (*Settings → Display → Display Mode*). Abyss
  covers Auto/modern and Desktop (Legacy) both, which is why it was picked, but a
  half-styled UI is the symptom to check this against first.
- **Re-check the theme after any Jellyfin image bump.** Unversioned CSS pinned to nothing
  degrades silently — same failure class as the UI plugins, without even a plugin
  dashboard to show a red mark.
