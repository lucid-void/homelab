# Jellyfin — plugins and theme

**Read before editing:** nothing in git. Everything here is applied through the Jellyfin
web UI and lives only on the config PVC, so this file is the install script nobody can
run. The server deployment is `design/decisions/jellyfin.md`.

## Plugins

Almost nothing here is from the official catalogue; the same class of unowned state as
Suwayomi's extension repos or Kavita's library settings.

### Repositories to add

Four URLs under *Dashboard → Plugins → Repositories* cover every plugin listed:

| Repository | Serves |
|---|---|
| `https://www.iamparadox.dev/jellyfin/plugins/manifest.json` | File Transformation, Plugin Pages, Home Screen Sections, Media Bar |
| `https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/manifest.json` | Jellyfin Enhanced |
| `https://raw.githubusercontent.com/Flowfin/jellyfin-plugin-sso/manifest-beta/manifest.json` | SSO Authentication |
| the `intro-skipper` org's manifest | Intro Skipper |

### Install order matters

Most of these rewrite `jellyfin-web` at serve time, routed through one plugin.
**Install File Transformation first.** Home Screen Sections and Media Bar installed
ahead of their dependencies do not error — they install, report healthy and render
nothing, which reads as "the plugin is broken" rather than "a dependency is missing".

1. **File Transformation** — rewrites served web content without touching files on disk.
   Media Bar 3.x requires **File Transformation 3.x specifically**.
2. **Plugin Pages** — required by Home Screen Sections.

That is the whole dependency set. **JavaScript Injector** and **Custom Tabs** appear in
most guides for this stack, but they were only ever needed by KefinTweaks and are not
installed here — see the rejection note below.

### The plugins

| Plugin | Why | Notes |
|---|---|---|
| **SSO Authentication** | Keycloak OIDC | Required. A fork — see below. |
| **Jellyfin Enhanced** | Keyboard shortcuts, subtitle styling, TMDB/user reviews, Seerr integration, media calendar, Sonarr/Radarr download monitoring | From v11 it supports **10.11+ and 12.x only**. File Transformation is "highly recommended" and avoids injection permission problems. |
| **Home Screen Sections** | Server-provided custom rows on the home screen | Needs **3.0.2.0** on 12.x. Requires File Transformation *and* Plugin Pages. |
| **Media Bar** | Featured-content hero carousel on the home screen | Needs **3.0.0.0**, which covers both the classic Desktop layout and the new React/MUI one. |
| **Intro Skipper** | Skip intros/credits | Requires server 12.0+; build must match the exact server release. |
| **Webhook** | → Gotify, matching the cluster's alerting path | Official catalogue. |

Optional additions from the official catalogue: **Playback Reporting** (watch statistics)
and **Trakt** (scrobbling). TMDb and TheTVDB metadata providers ship built in — do not
install them. Open Subtitles is worth adding only if subtitle fetching is wanted.

### KefinTweaks — considered and rejected

Evaluated and **not installed**, on two independent grounds.

**Its v12 support is incomplete by its own account.** The v0.4.10 notes (2026-09-09) say
v12 support "is still incomplete, but should be fully functional when opting into the
older **'Desktop (legacy)' UI**"; its README is staler and claims 10.10.7-or-earlier.
Taking it means putting every user on the legacy UI to suit one plugin — while Media Bar
3.0.0.0 handles the modern layout natively. The cost lands on the whole install.

**It is not a plugin and has no manifest** — a JavaScript bundle pulled from jsDelivr at
`@latest` through the JavaScript Injector plugin, executing in every browser that loads
the UI, with no pin and nothing for Renovate to track. A sharper supply-chain edge than
anything else here, next to an auth path already leaning on a forked SSO plugin.

Revisit when the legacy-UI caveat is gone. Dropping it also drops **JavaScript Injector**
and **Custom Tabs**, which existed only to serve it; its features overlap Jellyfin
Enhanced, which is installed and does support 12.x.

### Version coupling is the standing hazard

Every plugin above is compiled against, or patches, a specific server version, and fails
silently rather than erroring. Home Screen Sections **3.0.1.0 was pulled** for breaking on
**12.1** — precisely the line pinned here (3.0.2.0 is the fix), and 3.0.0.0 dropped
10.10.7 outright. Hence the `major-update` Renovate label: before merging a jellyfin bump,
check SSO Authentication, File Transformation, Home Screen Sections, Media Bar and Intro
Skipper all ship a build for the target version. Check SSO first — it is the one that
turns cosmetic breakage into a lockout.

### The SSO plugin is a fork, and it is on the auth path

`9p4/jellyfin-plugin-sso`, which every guide still points at, **was archived 2026-05-12
and does not load on 10.11 or newer**. The successor is a community revival published as
`Flowfin`/`kernicek`; one URL serves both 10.11 (.NET 9) and 12.0 (.NET 10).

A revived third-party fork therefore mediates every login. The local Jellyfin admin is the
break-glass path and must keep a real password — do not delete it after SSO works.


## Theme

**Abyss** (`AumGupta/abyss-jellyfin`).

Jellyfin 12's React/MUI migration broke essentially the whole community theme scene. The
curated `awesome-jellyfin` list now hides every theme it used to carry —  Ultrachromic,
Zesty, Finity, Flow, scyfin, Catppuccin, NetFin, Jamfin and the rest — as "may not work
anymore", leaving three: Abyss, Jellyfish and ElegantFin.

Abyss was chosen because it is tested against **12.1.x specifically** (the line pinned
here) and styles **both** the modern React/MUI interface and Desktop (Legacy) from the
same stylesheet, so it survives a Display Mode change instead of forcing one. It is
customisable through CSS variables declared after the import — accent colour, corner
radius, fonts — and ships a "Lite mode" for low-powered clients.

Not chosen: **Jellyfish** (n00bcodr), a close second with the advantage of sharing an
author with Jellyfin Enhanced; **ElegantFin**, which on v12 needs a separate un-upstreamed
community fork (`mihaif7/elegantfin-jf12`).

### Install it inline, not as an `@import`

The documented install is a rolling CDN reference:

```css
@import url("https://cdn.jsdelivr.net/gh/AumGupta/abyss-jellyfin@main/abyss.css");
```

That `@main` is the unpinned-CDN shape KefinTweaks was rejected over, so it gets the same
treatment: **fetch `abyss.css` once and paste its contents** into *Dashboard → General →
Custom CSS* (per-user overrides live in each user's Display settings). ~68 KB / ~2900
lines. Re-paste to update, deliberately. A stylesheet can restyle and hide but not
execute, so it is a smaller hazard than KefinTweaks' JS — still third-party content in
every viewer's browser, and the pin costs nothing.

**Pasting inline does not eliminate the external fetches.** The stylesheet pulls two
sub-resources, wherever the CSS lives:

- `fonts.googleapis.com` — Google Sans, unpinned, and a request to Google from every
  client that loads the UI.
- `cdn.jsdelivr.net/npm/material-icons@1.13.12` — the Material Icons Round webfont. This
  one *is* version-pinned.

Delete those two rules (the leading `@import` and the `@font-face` block) for zero
external requests; icons fall back to what Jellyfin ships, type to the system stack.

### Gotchas

- **Custom CSS never reaches `/dashboard` on Jellyfin 12.** The admin dashboard is a
  separate React app that does not load it, so admin pages stay stock-looking forever.
  That is upstream behaviour, not a broken theme.
- **The theme must match Display Mode** (*Settings → Display → Display Mode*). Abyss
  covers Auto/modern and Desktop (Legacy) both, which is why it was picked, but a
  half-styled UI is the symptom to check this against first.
- **Re-check the theme after any Jellyfin image bump.** It is unversioned CSS pinned to
  nothing, so it degrades silently — same class of failure as the UI plugins, without
  even a plugin dashboard to show a red mark.
