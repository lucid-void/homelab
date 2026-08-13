# Immich — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## OAuth (Zitadel)

Zitadel Web app type + `/api/oauth/mobile-redirect` endpoint as redirect URI (proxies
to `app.immich:///oauth-callback`); Web type required because Native type rejects
https:// redirect URIs.

## User migration

Must transfer `asset` + `album` + `person` rows; omitting `person` breaks mobile sync
(FK violation on `asset_face_entity`).

## Vector DB (VectorChord)

Embeddings run on **VectorChord** (`vchord`; embedding indexes use the `vchordrq`
access method) in the shared CNPG Postgres. `DB_VECTOR_EXTENSION` is intentionally
**unset** in the HelmRelease — Immich v3 auto-selects VectorChord over pgvector, and
the value cannot be *changed* once initialized (it must be absent for Immich to
migrate). Custom image `ghcr.io/lucid-void/postgres-cnpg-immich` bundles pgvector +
VectorChord; cluster `shared_preload_libraries: [vchord.so]`.

**VectorChord moved org `tensorchord` → `supervc-stack`** (repo transfer, not a fork)
— the Dockerfile now fetches from the new org, since GitHub's rename redirect dies if
anyone ever re-creates the old path. pgvector and VectorChord are built from pinned
source/release archives that no Renovate manager sees natively, so both are covered by
`customManagers` in `renovate.json` — and each version lives in **two** places
(Dockerfile `ARG` default + workflow `env`) that must move together. `IMAGE_VERSION` in
the build workflow is the newest **built** tag; `imagecatalog.yml` pins the
**deployed** one and is intentionally allowed to lag, because moving it rolls the DB.

**pgvecto.rs** (`vectors`) was fully removed after the v3 migration (extension dropped,
`vectors.so` out of shared_preload, layer dropped from the image).

