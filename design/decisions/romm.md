# RomM

**Read before editing:** `kubernetes/apps/media/romm/`

## Current state

Game/ROM manager in `media` at `romm.blackcats.cc` (`rommapp/romm`, port `8080`).
Single `app-template` controller `app` → Deployment/Service `romm`.

External CNPG Postgres via `ROMM_DB_DRIVER=postgresql` (password mirrored from
`romm-role-secret`); embedded Valkey persists to an `emptyDir` at `/redis-data` — kept
off NFS because of AOF/fsync locking. ROM library is `media-nfs` subPath `Games`
mounted at `/romm/library`, organised as `Games/roms/<platform>/…`.

`ROMM_AUTH_SECRET_KEY` (session signing, ≥32 bytes, must stay stable) comes from the
`romm-secret` SealedSecret. `HASHEOUS_API_ENABLED=true` gives keyless metadata; IGDB
(Twitch dev app) is optional.

OIDC via Keycloak: `client_secret_basic`, redirect `…/api/oauth/openid`. All `OIDC_*`
vars (including `OIDC_ENABLED`) live in the sealed `media/romm-oidc-secret` and are
consumed via **optional** `envFrom` — absent means OIDC off (local admin via the
first-run wizard), present means OIDC on (Reloader restarts on rotation). No bootstrap
Job needed.

**RomM matches OIDC logins on the `email` claim and stores no subject**, which makes it
the one app that survived the Zitadel→Keycloak cutover without re-linking. The same
design means roles cannot come from the IdP: `users.role` and `permission_group_id` are
local columns with no OIDC mapping, so an account's privileges are managed in RomM and
nowhere else.

## Rules

- **The image runs as root and ignores PUID/PGID** — no securityContext override
  ([rommapp/romm#1302](https://github.com/rommapp/romm/issues/1302)); forcing non-root
  breaks its s6 init.
- **Pin `OIDC_ALLOW_REGISTRATION=true` explicitly in the HelmRelease `env`** (v5+) —
  this gates whether an OIDC login may create an account, and it is set in the
  HelmRelease `env` rather than the secret, so it never shadows `romm-oidc-secret`. It
  must stay `true` because Keycloak is the only user store: an account only exists after
  its first sign-in.
- **Never assume a squashed-migration DB can be upgraded across the boundary** — v5
  squashed migrations 0002–0008 into `0001_initial_models`, so a DB whose
  `alembic_version` sits in that squashed range can no longer be upgraded from there.
