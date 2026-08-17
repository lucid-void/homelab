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

**pgvector must be built with an explicit `OPTFLAGS`** — this is the single most
expensive gotcha in this image. pgvector's Makefile defaults to
`OPTFLAGS = -march=native`, so a bare `make` bakes the *GitHub Actions runner's* ISA
into `vector.so`. The runner fleet is mixed, so the same pinned source produces
different bytes run to run, and nothing in git records which ISA you got. The Dockerfile
now pins `OPTFLAGS="-march=x86-64-v3"` (AVX2 + FMA, satisfied by every CPU here, no
AVX-512). Do not remove it and do not "restore the default".

**2026-08-13 → 08-17 incident.** Renovate #155 moved `imagecatalog.yml` v1.1.0 → v1.1.1
and crash-looped the shared Postgres primary every ~9 minutes for four days (593
restarts), taking Immich down and stalling every `*-database` Kustomization behind
`postgres-cluster`'s health check:

    server process was terminated by signal 4: Illegal instruction
    Failed process was running: ... face_search.embedding <=> $1 ...
    shutting down because "restart_after_crash" is off

The v1.1.1 build (2026-08-01) landed on an AVX-512 runner and auto-vectorized
`cosine_distance`, `inner_product`, `l2_distance` and ~30 other **unguarded** functions
with EVEX instructions. The nodes are **Intel Core Ultra 5 235HX** (Arrow Lake-HX) with
`cpu.type = "host"`; Intel fuses AVX-512 off on hybrid P/E-core parts, so
`grep -c avx512 /proc/cpuinfo` is **0**. Immich's first `<=>` comparison raised SIGILL.

VectorChord was **not** at fault, and "same version, same source" is not evidence —
diff the actual binaries:

    vchord.so   v1.1.0 == v1.1.1   (sha256 1300ed9e…, byte-identical)
    vector.so   v1.1.0 != v1.1.1   (7a9c44ef… vs 45a0933d…)

Gate any new build on this before deploying it:

    objdump -d vector.so | awk '/^[0-9a-f]+ <.*>:/{fn=$2} /%zmm|%k[1-7]/{print fn}' | sort -u

Only `*Avx512*` symbols may appear — those are pgvector's own
`__builtin_cpu_supports`-guarded helpers and are safe on any CPU. `cosine_distance` in
that list means the build is not portable; do not ship it. **v1.1.1 is a permanently
broken tag** — it is still published, and both the workflow and `imagecatalog.yml` say
so inline.

**Renovate no longer touches `imagecatalog.yml`** (`matchFileNames` rule, `enabled:
false`). That file is a deploy trigger, not a version to chase: moving the line rolls
the Postgres pods. `IMAGE_VERSION` in the workflow is still tracked, so builds keep
being published; only the deploy is manual now.

**pgvecto.rs** (`vectors`) was fully removed after the v3 migration (extension dropped,
`vectors.so` out of shared_preload, layer dropped from the image).

