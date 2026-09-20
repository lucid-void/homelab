# Immich

**Read before editing:** `kubernetes/apps/immich/`, `kubernetes/images/postgres-cnpg-immich/`

## Current state

OIDC via Keycloak. Historically this was a Zitadel Web app (not Native — Native rejects `https://` redirect
URIs), with `/api/oauth/mobile-redirect` as the redirect URI, proxying to
`app.immich:///oauth-callback`.

Embeddings run on **VectorChord** (`vchord`; embedding indexes use the `vchordrq`
access method) in the shared CNPG Postgres, via the custom image
`ghcr.io/lucid-void/postgres-cnpg-immich` (bundles pgvector + VectorChord; the cluster
sets `shared_preload_libraries: [vchord.so]`). `pgvecto.rs` (`vectors`) was fully
removed after the Immich v3 migration.

VectorChord's upstream org is `supervc-stack` (moved from `tensorchord` via a repo
transfer — the Dockerfile fetches from the new org since GitHub's rename redirect does
not survive someone re-creating the old path). pgvector and VectorChord build from
pinned source/release archives that no Renovate manager sees natively, so both are
covered by `customManagers` in `renovate.json`; each version lives in **two** places
(Dockerfile `ARG` default + workflow `env`) that must move together. `IMAGE_VERSION` in
the build workflow is the newest **built** tag; `imagecatalog.yml` pins the
**deployed** one, and is intentionally allowed to lag.

## Rules

- **Never set `DB_VECTOR_EXTENSION`** — Immich v3 auto-selects VectorChord over
  pgvector, the value cannot be *changed* once initialized, and it must stay absent for
  Immich to migrate correctly.
- **User migration must transfer `asset` + `album` + `person`** — omitting `person`
  breaks mobile sync with a foreign-key violation on `asset_face_entity`.
- **Build pgvector with an explicit `OPTFLAGS`, pinned to `-march=x86-64-v3`** —
  pgvector's Makefile defaults to `OPTFLAGS = -march=native`, which bakes the GitHub
  Actions runner's ISA into `vector.so`. The runner fleet mixes AVX-512 and
  non-AVX-512 machines, and an AVX-512 build SIGILLs on this cluster's nodes (no
  AVX-512). `-march=x86-64-v3` (AVX2 + FMA) is satisfied by every CPU here.
- **Image tag `v1.1.1` is permanently broken — never deploy it.** It auto-vectorized
  `cosine_distance`, `inner_product`, `l2_distance` and ~30 other unguarded pgvector
  functions with EVEX instructions, which SIGILLs on non-AVX-512 hardware. `vchord.so`
  is unaffected (byte-identical to `v1.1.0`); only `vector.so` differs. Both the
  workflow and `imagecatalog.yml` note this inline.
- **Gate any new pgvector build with the AVX-512 symbol check before deploying it**
  (see Verify) — only `*Avx512*`-guarded symbols (pgvector's own
  `__builtin_cpu_supports` helpers) may appear; `cosine_distance` or similar in the
  list means the build is not portable.
- **Never move `imagecatalog.yml` forward casually** — it is a deploy trigger, not a
  version to chase: moving it rolls the Postgres pods. Renovate is disabled on it
  (`matchFileNames` rule, `enabled: false`) while `IMAGE_VERSION` in the build workflow
  stays tracked so builds keep publishing.

## Verify

```bash
objdump -d vector.so | awk '/^[0-9a-f]+ <.*>:/{fn=$2} /%zmm|%k[1-7]/{print fn}' | sort -u
grep -c avx512 /proc/cpuinfo
```
