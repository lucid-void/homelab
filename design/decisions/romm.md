# RomM — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## Deployment, storage, OIDC

Game/ROM manager (Sonarr-for-consoles) in `media` at `romm.blackcats.cc`
(`rommapp/romm`, port 8080). Single `app-template` controller `app` →
Deployment/Service `romm`.

**External CNPG Postgres** via `ROMM_DB_DRIVER=postgresql` (password mirrored from
`romm-role-secret`); **embedded Valkey** persists to an `emptyDir` at `/redis-data` —
keep it off NFS (AOF/fsync locking). ROM library = `media-nfs` subPath `Games` at
`/romm/library`, organised as `Games/roms/<platform>/…`. Image **runs as root and
ignores PUID/PGID** ([rommapp/romm#1302](https://github.com/rommapp/romm/issues/1302))
— no securityContext override; forcing non-root breaks its s6 init.
`ROMM_AUTH_SECRET_KEY` (session signing, ≥32 bytes, must stay **stable**) from
`romm-secret` SealedSecret; `HASHEOUS_API_ENABLED=true` = keyless metadata, IGDB
(Twitch dev app) optional.

**OIDC via Zitadel**: Web app / `client_secret_basic`, redirect `…/api/oauth/openid`,
"User Info inside ID Token" enabled; all `OIDC_*` vars (incl. `OIDC_ENABLED`) written
into `media/romm-oidc-secret` by `zitadel-bootstrap` Terraform and consumed via
**optional `envFrom`** — absent = OIDC off (local admin via first-run wizard),
present = OIDC on (Reloader restarts on rotation). No bootstrap Job needed.

**v5+**: `OIDC_ALLOW_REGISTRATION` gates whether an OIDC login may create an account
— upstream default is `true`, pinned explicitly in the HelmRelease `env` (Terraform
does *not* write this key, so it doesn't shadow `romm-oidc-secret`). Must stay `true`:
Zitadel is the only user store, so an account only exists after its first sign-in. v5
also **squashed migrations 0002–0008 into `0001_initial_models`** — a DB whose
`alembic_version` sits in that range can no longer be upgraded; ours was at
`0082_save_origin_device`, which `0083` still chains from.

