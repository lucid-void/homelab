# Jellyfin

**Read before editing:** `kubernetes/apps/media/jellyfin/`, `kubernetes/apps/keycloak/clients/app/jellyfin.yml`

## Why it exists

A second, fully independent media server beside Plex — not a cold spare. It serves the
same `media-nfs` share, keeps its own library database, and is reachable at
`jellyfin.blackcats.cc` whether or not Plex, plex.tv or the node holding Plex's config
is up.

That last clause is the design constraint. `plex-config-local` is an
`openebs-hostpath` PVC on **cp-1** and has no backup CronJob, so losing that node's disk
loses the Plex library outright. Jellyfin's config PVC is therefore pinned to **cp-2**
with a `nodeSelector`. `openebs-hostpath` is `WaitForFirstConsumer`: the PV is created
wherever the pod first lands and never moves. Changing that selector after first boot
does not migrate the library, it abandons it and forces a full re-scan.

## Current state

`media` namespace, `replicas: 1`, `bjw-s/app-template`, `lscr.io/linuxserver/jellyfin`
(PUID 2202 / PGID 2200, matching the rest of the stack). `media-nfs` mounted read-only
at `/Media`. Web access is HTTPRoute-only — **no `pool-b` LoadBalancer**, so
`172.16.20.51` (Plex direct/GDM) and `.52` (Velocity) are untouched.

Transcoding is CPU-only. No GPU device plugin exists in this cluster, and the Talos VMs
get no iGPU passthrough from the Proxmox host.

`JELLYFIN_PublishedServerUrl` is Jellyfin's analogue of Plex's `ADVERTISE_IP`: it is the
absolute base URL handed to clients and written into OIDC callbacks. Without it clients
arriving through the Gateway are served internal cluster URLs.

## Rules

- **Never mount transcode scratch on the config PVC.** `/config/transcodes` is an
  `emptyDir`. A single long 4K transcode writes tens of GB of segments, and filling the
  50Gi hostpath volume corrupts the SQLite library sharing it.
- **Never pin the config PVC to the node Plex is on.** It defeats the only failure this
  deployment exists to survive. Check with
  `kubectl get pod -n media -l app.kubernetes.io/name=plex -o wide` before changing the
  selector.
- **Never drop the startup probe.** `/health` returns non-200 for the whole of a
  startup migration, and a 12.x first boot converts the entire library database. With
  liveness alone the kubelet kills the pod mid-conversion and crash-loops it against a
  half-migrated DB. The startup probe holds liveness off (the kubelet does not run
  liveness until startup succeeds) and buys 15 minutes. If a migration ever needs
  longer, raise `failureThreshold`, not `periodSeconds`.
- **Never set a CPU limit.** Every transcode is software on the Arrow Lake P-cores;
  throttling one produces buffering that looks like a network fault, not a clean failure.
- **Pin the full lscr tag, never the short `12.1`.** Same mutable-short-tag trap as Plex
  (`design/decisions/plex.md`). The tag shape here — `X.Yubu####-lsNN` — is a fourth
  variant none of the existing Renovate regexes parse, so it has its own rule; the
  `ubu####` segment is matched but not captured, since it tracks the Ubuntu base rather
  than Jellyfin.
- **Never install a theme or script from an unpinned CDN reference.** `@main`/`@latest`
  on jsDelivr means a third party can change what every viewer's browser loads, with no
  version for Renovate to track and no signal when it moves. Paste CSS inline, or pin a
  tag or commit. This is why KefinTweaks was rejected and why the Abyss import is not
  used as documented upstream.
- **Do not install KefinTweaks** — it was evaluated and rejected. Its own release notes
  put Jellyfin 12 support behind the legacy Desktop UI, so adopting it means downgrading
  the UI for everyone, and it ships as an unpinned `@latest` CDN script rather than a
  plugin. Full reasoning below; revisit only when the legacy-UI caveat is gone.
- **Never merge a Jellyfin image bump unattended.** Plugin builds are compiled against an
  exact server version and refuse to load on a mismatch — and the plugin that refuses to
  load is the one holding SSO, so a bad bump is a lockout, not a cosmetic regression.
  Renovate labels these `major-update` for manual review. Confirm that SSO Authentication,
  File Transformation, Home Screen Sections, Media Bar and Intro Skipper all ship a build
  for the target version *before* merging — SSO first, since it is the one whose failure
  is a lockout.

## Plugins

**Plugin installs are runtime state on the config PVC. Git cannot own them** — same class
of thing as Suwayomi's extension repos or Kavita's library settings. This list is the
only record; a PVC rebuild means re-doing it by hand.

Almost nothing here comes from the official catalogue. Everything below is third-party.

### Repositories to add

Four URLs under *Dashboard → Plugins → Repositories* cover every plugin listed:

| Repository | Serves |
|---|---|
| `https://www.iamparadox.dev/jellyfin/plugins/manifest.json` | File Transformation, Plugin Pages, Home Screen Sections, Media Bar |
| `https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/manifest.json` | Jellyfin Enhanced |
| `https://raw.githubusercontent.com/Flowfin/jellyfin-plugin-sso/manifest-beta/manifest.json` | SSO Authentication |
| the `intro-skipper` org's manifest | Intro Skipper |

### Install order matters

Most of these rewrite `jellyfin-web` at serve time rather than shipping server code, and
they all route that through one plugin. **Install File Transformation first, then the
other enablers, then the plugins that use them.** Home Screen Sections and Media Bar
installed ahead of their dependencies do not error — they install, report healthy and
render nothing, which reads as "the plugin is broken" rather than "a dependency is
missing".

1. **File Transformation** — the foundation. Rewrites served web content without touching
   files on disk, which is also what keeps these plugins working on a read-only-ish
   container filesystem. Media Bar 3.x requires **File Transformation 3.x specifically**.
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

Evaluated for this deployment and **not installed**, on two independent grounds.

**Its Jellyfin 12 support is incomplete by its own account.** The project's v0.4.10 notes
(2026-09-09) say v12 support "is still incomplete, but should be fully functional when
opting into the older **'Desktop (legacy)' UI**"; its README is staler still and claims
10.10.7-or-earlier. Taking it would mean putting every user on the legacy UI to suit one
plugin, on a server pinned to 12.x — while Media Bar 3.0.0.0 supports the modern React/MUI
layout natively. The cost lands on the whole install, not on the plugin.

**It is not a plugin and has no manifest.** It is a JavaScript bundle pulled from jsDelivr
at `@latest` and injected through the JavaScript Injector plugin — a rolling reference to
a third-party CDN, executing in every browser that loads the UI, with no pin and nothing
for Renovate to track. That is a sharper supply-chain edge than anything else in this
cluster, and it sits next to an auth path already leaning on a forked SSO plugin.

Revisit when its release notes drop the legacy-UI caveat. Nothing else here depends on
it: dropping it also drops **JavaScript Injector** and **Custom Tabs**, which existed in
the plan only to serve it. Its features overlap Jellyfin Enhanced considerably, which is
installed and does support 12.x.

### Version coupling is the standing hazard

Every plugin above is compiled against, or patches, a specific server version, and the
failure mode is silence rather than an error. Two live examples from this ecosystem:

- Home Screen Sections **3.0.1.0 was pulled** because it broke on Jellyfin **12.1** —
  precisely the release line pinned here. 3.0.2.0 is the fix.
- Home Screen Sections 3.0.0.0 dropped Jellyfin 10.10.7 support entirely.

This is why the Renovate rule labels jellyfin image bumps `major-update`. Before merging
one, check that **SSO Authentication, File Transformation, Home Screen Sections, Media
Bar and Intro Skipper** all ship a build for the target version. SSO is the one that
turns a cosmetic breakage into a lockout, so it is the one to check first.

### The SSO plugin is a fork, and it is on the auth path

`9p4/jellyfin-plugin-sso` — the one every guide still points at — **was archived
2026-05-12 and does not load on Jellyfin 10.11 or newer**. The maintained successor is a
community revival published as `Flowfin`/`kernicek`. One URL serves both 10.11 (.NET 9)
and 12.0 (.NET 10); the server picks its matching build.

Accept the consequence knowingly: a revived third-party fork mediates every login. The
local Jellyfin admin account is the break-glass path and must keep a real password — do
not delete it after SSO works.


## Theme

**Abyss** (`AumGupta/abyss-jellyfin`). Like plugins, a theme is runtime state on the
config PVC — git does not own it, so this is the only record.

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

That `@main` is the same unpinned-third-party-CDN shape KefinTweaks was rejected over, so
it gets the same treatment: **fetch `abyss.css` once and paste its contents** into
*Dashboard → General → Custom CSS* (server-wide; per-user overrides live in each user's
Display settings). It is ~68 KB / ~2900 lines — large for a text box, well within what
Jellyfin stores. Re-paste to update, deliberately.

A stylesheet is a smaller hazard than KefinTweaks' JavaScript bundle — it can restyle and
hide, not execute — but it is still third-party content rendering for every viewer, and
the pin costs nothing.

**Pasting inline does not eliminate the external fetches.** The stylesheet itself pulls
two sub-resources, and they remain wherever the CSS lives:

- `fonts.googleapis.com` — Google Sans, unpinned, and a request to Google from every
  client that loads the UI.
- `cdn.jsdelivr.net/npm/material-icons@1.13.12` — the Material Icons Round webfont. This
  one *is* version-pinned.

Delete those two rules (the leading `@import` and the `@font-face` block) for zero
external requests; icons fall back to what Jellyfin ships and the typeface to the system
stack. Worth doing if client-side calls to Google are unwanted.

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


## Keycloak wiring

The `KeycloakOIDCClient` CR covers the client, its sealed secret and the `user`/`admin`
roles. Redirect URIs:

- `https://jellyfin.blackcats.cc/sso/OID/redirect/keycloak` — the path shape is fixed by
  the plugin (`/sso/OID/redirect/<provider name>`). The trailing segment is the
  **provider name configured inside the plugin**, not the realm and not the client id.
  They must match exactly or the callback 404s.
- `org.jellyfin.mobile://login-callback` — the official mobile client completes on a
  custom scheme.

**Three things the CRD cannot express and git therefore does not hold** (the same gap
documented in `design/decisions/keycloak.md`):

1. Groups `/jellyfin/user` and `/jellyfin/admin`, both carrying the `user` role.
2. **A protocol mapper emitting a FLAT roles claim.** The plugin's `RoleClaim` takes a
   claim *name* and reads a flat array; it cannot walk the nested
   `resource_access.jellyfin.roles` that Grafana reaches with JMESPath. Add a client-roles
   mapper on the `jellyfin` client with a flat token claim name, *Multivalued* on, and
   *Add to ID token* / *Add to access token* / *Add to userinfo* all on. Set the plugin's
   `RoleClaim` to that name, `Roles` to `[user]`, `AdminRoles` to `[admin]`.
3. The `browser-jellyfin` flow override — nested `jellyfin-authenticate` sub-flow plus a
   CONDITIONAL gate on negated `jellyfin.user`. Build it exactly as
   `design/decisions/keycloak.md` describes; appending the gate to a plain copy of
   `browser` locks out **everyone**.

Keycloak's roles gate login only. Jellyfin's own admin bit comes from `AdminRoles` above.

## Seerr

Seerr talks to **one** media server at a time — Plex *or* Jellyfin, not both. It is
currently pointed at Plex and was deliberately left that way; switching it is a separate
decision, not a consequence of this deployment.

Jellyfin Enhanced ships a Seerr integration that puts requesting inside the Jellyfin UI.
It is worth wiring only after deciding which server Seerr is backed by — with Seerr on
Plex, Jellyfin users are not Seerr users and the integration has no account to act as.

## Verify

```bash
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=jellyfin -o wide
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=plex -o wide   # must differ
mise exec -- kubectl get keycloakoidcclient jellyfin -n keycloak \
  -o custom-columns='NAME:.metadata.name,ERRORS:.status.conditions[?(@.type=="HasErrors")].status'
curl -sf https://jellyfin.blackcats.cc/health && echo
```
