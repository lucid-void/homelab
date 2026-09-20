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

## Client-side stack

Plugins and the theme are **runtime state on the config PVC** — git owns none of it, and
a PVC rebuild means redoing it by hand. Both are recorded in
`design/decisions/jellyfin-ui.md`, together with the install order, the version pins that
matter on 12.x, and what was rejected.

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
