# Joplin — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## Deployment, storage, SAML SSO

Joplin Server in its own `joplin` ns at `joplin.blackcats.cc` (`joplin/server`, port
22300). Single `app-template` controller `app` → Deployment/Service `joplin`.

**External CNPG Postgres** (`DB_CLIENT=pg`, password by `secretKeyRef` from the
reflected `joplin-role-secret`).

**`STORAGE_DRIVER: "Type=Filesystem; Path=/mnt/joplin-blobs"`** on a dedicated RWO
`nfs-client` PVC — the default `Type=Database` would push every attachment into the
shared CNPG 20Gi PVC.

**SSO is SAML, not OIDC** — upstream has no OIDC
([#14252](https://github.com/laurent22/joplin/issues/14252)). SP metadata is a static
ConfigMap (`/saml-sp/sp.xml`) that must stay **byte-identical** to the `metadata_xml`
in `zitadel_application_saml.joplin`, or the audience check fails; IdP metadata is
curl'd from `https://zitadel.blackcats.cc/saml/v2/metadata` by an initContainer on
every boot (Zitadel's signing cert rotates), with retries so a cold start can't wedge
the pod. ACS `…/api/saml`, entityID `https://joplin.blackcats.cc`. `API_BASE_URL`
**must equal** `APP_BASE_URL` (SAML unsupported on a split API domain);
`DELETE_EXPIRED_SESSIONS_SCHEDULE=""` disables the 6h session purge (it assumes silent
API re-login, impossible with a browser flow). `LOCAL_AUTH_ENABLED=true` retained:
`admin@localhost` is break-glass while the desktop SAML sync target is upstream-beta
(**CLI does not support SAML at all**). Four gotchas, all hit in practice: (1) **probes
need an explicit `Host` header** — Joplin routes API vs website by request host and
404s the kubelet's pod-IP probe, restart-looping a healthy app; (2) **Zitadel emits
`Email`/`FullName`, Joplin wants exactly `email`/`displayName`** → bridged by
`zitadel_action.joplin_saml_attributes` on `FLOW_TYPE_SAML_RESPONSE`; (3) that action
must **coerce with `String()`, never `typeof x === 'string'`** — Zitadel types
`DisplayName` as a plain Go string but `Email` as `domain.EmailAddress` (named string
type), which goja doesn't surface as a JS primitive, so a typeof guard silently drops
email; the struct is flat (`human.email`, no `human.profile`); (4) **the stable server
has no attribute type guards** (those are `dev`-only, and were still absent as of
3.7.1), so an absent attribute reads as `Could not login using email "undefined"`.
Also: **never create a local Joplin account with a Zitadel email** — `ssoLogin`
refuses SAML for an email owned by a non-external user and never upgrades
`is_external`, permanently blocking SSO for that address; SAML auto-provisions on first
login, so no pre-creation is needed.

