# Jellyfin

**Read before editing:** `kubernetes/apps/media/jellyfin/`, `kubernetes/apps/keycloak/clients/app/jellyfin.yml`

## Why it exists

A second, fully independent media server beside Plex — not a cold spare. It serves the
same `media-nfs` share, keeps its own library database, and is reachable at
`jellyfin.blackcats.cc` whether or not Plex, plex.tv or the node holding Plex's config
is up.

That last clause is the design constraint. `plex-config-local` is an `openebs-hostpath`
PVC on **cp-1** with no backup CronJob, so losing that node's disk loses the Plex library
outright. Jellyfin's config PVC is therefore pinned to **cp-2**. `openebs-hostpath` is
`WaitForFirstConsumer`: the PV lands where the pod first runs and never moves, so
changing that selector later abandons the library rather than migrating it.

## Current state

`media` namespace, `replicas: 1`, `bjw-s/app-template`, `lscr.io/linuxserver/jellyfin`
(PUID 2202 / PGID 2200, matching the rest of the stack). `media-nfs` mounted read-only
at `/Media`. Web access is HTTPRoute-only — **no `pool-b` LoadBalancer**, so
`172.16.20.51` (Plex direct/GDM) and `.52` (Velocity) are untouched.

Transcoding uses Intel Quick Sync (VAAPI/QSV), via cp-2's passed-through iGPU
(`design/architecture.md`'s Talos Extensions table, `infra/terraform/kubernetes.tf`'s
`hostpci` block on cp-2). It reaches the container as the schedulable resource
`gpu.intel.com/i915`, not a hostPath volume — the `media` namespace enforces
PodSecurity `baseline`, which forbids hostPath volumes outright, and broadening the
whole namespace to `privileged` for every app in it just for this was rejected. The
Intel GPU device plugin DaemonSet (`kubernetes/apps/kube-system/intel-gpu-plugin`,
`kube-system` — PSA-exempt) exposes the resource instead; it's node-pinned to cp-2,
the only node with the device. Software transcoding is still the fallback for codecs
QSV doesn't cover — the container keeps no CPU limit for that reason.

The container still needs POSIX group access to open `/dev/dri/renderD128` even
though the device plugin (not a hostPath mount) is what gets it there — the
`supplementalGroups` value in the HelmRelease was read live off cp-2
(`stat -c '%g' /dev/dri/renderD128` inside the pod), not guessed. **Re-run that check
after any Talos upgrade touching cp-2** — nothing pins this gid stable across an
`i915` extension or Talos version change, and a silent mismatch means transcodes
quietly stop using hardware instead of failing loudly.

`JELLYFIN_PublishedServerUrl` is Jellyfin's analogue of Plex's `ADVERTISE_IP`: the
absolute base URL handed to clients. Without it, clients arriving through the Gateway are
served internal cluster URLs. It does **not** fix the SSO plugin's redirect URI — see
below.

## Rules

- **Never add a hostPath volume to this HelmRelease.** The `media` namespace inherits
  the cluster-default PodSecurity `baseline`, which forbids hostPath volumes outright —
  a Helm upgrade adding one fails admission (`PodSecurity "baseline:latest": hostPath
  volumes`) and Flux auto-rolls back. GPU access goes through the Intel GPU device
  plugin's `gpu.intel.com/i915` resource instead (see Current state above).
- **Never mount transcode scratch on the config PVC.** `/config/transcodes` is an
  `emptyDir`. A single long 4K transcode writes tens of GB of segments, and filling the
  50Gi hostpath volume corrupts the SQLite library sharing it.
- **Never pin the config PVC to the node Plex is on.** It defeats the only failure this
  deployment exists to survive. Check with
  `kubectl get pod -n media -l app.kubernetes.io/name=plex -o wide` before changing the
  selector.
- **Never drop the startup probe.** `/health` returns non-200 for the whole of a startup
  migration, and a 12.x first boot converts the entire library database. With liveness
  alone the kubelet kills the pod mid-conversion and crash-loops it against a half-migrated
  DB. The startup probe holds liveness off and buys 15 minutes. If a migration needs
  longer, raise `failureThreshold`, not `periodSeconds`.
- **Never set a CPU limit.** Transcodes are software on the Arrow Lake P-cores;
  throttling one produces buffering that looks like a network fault, not a failure.
- **Pin the full lscr tag, never the short `12.1`.** Same mutable-short-tag trap as Plex
  (`design/decisions/plex.md`). The tag shape here — `X.Yubu####-lsNN` — is a fourth
  variant none of the existing Renovate regexes parse, so it has its own rule; the
  `ubu####` segment is matched but not captured, since it tracks the Ubuntu base rather
  than Jellyfin.
- **Never install a theme or script from an unpinned CDN reference.** `@main`/`@latest`
  on jsDelivr lets a third party change what every viewer's browser loads, with nothing
  for Renovate to track. Paste CSS inline, or pin a tag or commit.
- **Do not install KefinTweaks** — evaluated and rejected: its v12 support requires the
  legacy Desktop UI for everyone, and it ships as an unpinned `@latest` CDN script, not
  a plugin. Reasoning in `jellyfin-ui.md`.
- **Never merge a Jellyfin image bump unattended.** Plugin builds are compiled against an
  exact server version and refuse to load on a mismatch — and the plugin that refuses to
  load is the one holding SSO, so a bad bump is a lockout, not a cosmetic regression.
  Renovate labels these `major-update` for manual review. Confirm that SSO Authentication,
  File Transformation, Home Screen Sections, Media Bar and Intro Skipper all ship a build
  for the target version *before* merging — SSO first, since it is the one whose failure
  is a lockout.
- **Never treat a pod restart as a no-op.** Jellyfin's *Update Plugins* task runs on a
  startup trigger, so every restart is also an unattended plugin upgrade — including the
  plugin holding SSO, which has moved this way once already. Auto-update is on by choice;
  the cost is that `curl`ing the SSO redirect is part of finishing a restart, not an
  optional check. The command is in `jellyfin-ui.md`.

## Client-side stack

Plugins and the theme are **runtime state on the config PVC** — git owns none of it and a
PVC rebuild means redoing it by hand. `design/decisions/jellyfin-ui.md` holds the install
order, the pins that matter on 12.x, and what was rejected.

## Keycloak wiring

The `KeycloakOIDCClient` CR covers the client, its sealed secret and the `user`/`admin`
roles. Redirect URIs:

- `https://jellyfin.blackcats.cc/sso/OID/redirect/keycloak` — the path shape is fixed by
  the plugin (`/sso/OID/redirect/<provider name>`). The trailing segment is the
  **provider name configured inside the plugin**, not the realm and not the client id.
  They must match exactly or the callback 404s.
- `org.jellyfin.mobile://login-callback` — the official mobile client completes on a
  custom scheme.

**Three things live only in the Keycloak console.** The CRD expresses none of them, so
git does not hold them and a realm rebuild loses them (the same gap documented in
`design/decisions/keycloak.md`). All three are in place:

1. Groups `/jellyfin/user` and `/jellyfin/admin`, both carrying the `user` role.
2. **A flat roles claim**, `jellyfin_roles` — a *User Client Role* mapper on the
   `jellyfin` client, *Multivalued* on, ID token / access token / userinfo all on.
   `RoleClaim` reads a flat array and cannot walk the nested
   `resource_access.jellyfin.roles` that Grafana reaches with JMESPath. Two silent
   failures: *Multivalued* off emits a bare string the array read misses, and the
   mapper's *Name* is not the claim — *Token Claim Name* is, and only that must match
   `RoleClaim`.
3. The `browser-jellyfin` flow override, **bound to the client**: nested
   `jellyfin-authenticate` sub-flow plus a CONDITIONAL `jellyfin-gate` on negated
   `jellyfin.user`. Mirror an existing `browser-<svc>` flow rather than building from
   scratch; appending the gate to a plain copy of `browser` locks out **everyone**.

Keycloak's roles gate login only. Jellyfin's own admin bit comes from the plugin's
`AdminRoles`, ignored entirely unless *Enable Authorization* is on — with it off every
SSO user is created as a plain user and the mapper is never consulted.

## The SSO plugin fails closed, twice

Both defaults are right in general and wrong here. Each surfaces only once the previous
is cleared, and each reads as a login error, not a configuration error.

- **`AllowPrivateNetworkAddresses` must be ON.** `sso.blackcats.cc` resolves to the
  Gateway VIP `172.16.20.50`, so the plugin refuses to read the discovery document
  (*"the outbound host resolves only to blocked addresses"*) without ever contacting
  Keycloak. The guard stops a hostile IdP URL probing internal hosts — moot when the
  IdP **is** the internal host. Saving it logs an audit warning naming the check.
- **`SchemeOverride` must be `https`.** The Gateway terminates TLS, so the pod sees
  plain HTTP and builds an `http://` redirect URI; Keycloak matches exactly and rejects
  the pushed authorization request (*`invalid_request` — Failed to push authorization
  parameters*). `JELLYFIN_PublishedServerUrl` does not cover it — the plugin builds the
  URI from the incoming request.

**The provider form does not edit in place** — click the provider in the OID list to
load it, then save from the bottom of that section. Editing without loading it first
writes nothing, which reads as "the setting will not save".

**Never configure this plugin over the REST API.** Every other plugin here can be driven
by `GET`ting `/Plugins/<guid>/Configuration`, mutating it and `POST`ing it back. This one
returns `OidSecret` as **`null`** — it masks the secret on read — so that round trip
writes `null` over the encrypted client secret and takes SSO down for everyone. The UI is
the only safe way in. Details and the rest of the API notes are in `jellyfin-ui.md`.

## Seerr

Seerr talks to **one** media server at a time — Plex *or* Jellyfin. It points at Plex and
was deliberately left there; switching it is a separate decision. Jellyfin Enhanced's
Seerr integration is therefore not worth wiring yet: with Seerr on Plex, Jellyfin users
are not Seerr users and it has no account to act as.

## Verify

```bash
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=jellyfin -o wide
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=plex -o wide   # must differ
mise exec -- kubectl get keycloakoidcclient jellyfin -n keycloak \
  -o custom-columns='NAME:.metadata.name,ERRORS:.status.conditions[?(@.type=="HasErrors")].status'
curl -sf https://jellyfin.blackcats.cc/health && echo

# SSO still works: expect 302 to a Keycloak URL carrying request_uri=urn:ietf:...
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://jellyfin.blackcats.cc/sso/OID/start/keycloak
```
