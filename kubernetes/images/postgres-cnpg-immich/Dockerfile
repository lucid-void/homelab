ARG PG_MAJOR=17
ARG PG_BASE_TAG=bookworm
ARG PGVECTOR_VERSION=0.8.2
ARG VECTORCHORD_VERSION=1.1.1

# Build pgvector from source (pinned version, no apt version drift)
FROM ghcr.io/cloudnative-pg/postgresql:${PG_MAJOR}-${PG_BASE_TAG} AS builder

ARG PG_MAJOR
ARG PG_BASE_TAG
ARG PGVECTOR_VERSION

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
        postgresql-server-dev-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/*

# OPTFLAGS is load-bearing — do NOT drop it and do NOT set it to -march=native.
#
# pgvector's Makefile defaults to `OPTFLAGS = -march=native`, which bakes the *build
# machine's* ISA into vector.so. The build machine is a GitHub Actions runner from a
# mixed fleet, so a bare `make` produces different bytes from the same pinned source
# depending on which runner won the lottery. On 2026-08-01 it landed on an AVX-512
# runner and auto-vectorized cosine_distance, inner_product, l2_distance and ~30 other
# *unguarded* functions with EVEX instructions. Deploying that (v1.1.1) killed the
# shared Postgres primary every ~9 minutes for four days: the nodes are Intel Core
# Ultra 5 235HX (Arrow Lake-HX) with AVX-512 fused off, so Immich's first `<=>`
# comparison raised SIGILL and took the postmaster down with restart_after_crash=off.
#
# x86-64-v3 (AVX2 + FMA + BMI, no AVX-512) is satisfied by every CPU in this homelab
# and keeps the vectorized distance paths. Empty OPTFLAGS would work too but drops to
# SSE2 baseline. pgvector's own runtime-dispatched AVX-512 helpers
# (BitHammingDistanceAvx512Popcount et al.) are guarded by __builtin_cpu_supports and
# stay safe regardless of this flag.
#
# Verify a build is portable before deploying it:
#   objdump -d vector.so | awk '/^[0-9a-f]+ <.*>:/{fn=$2} /%zmm|%k[1-7]/{print fn}' | sort -u
# Only *Avx512* symbols may appear. cosine_distance in that list means do not ship it.
RUN curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
        | tar -xz -C /tmp \
    && cd /tmp/pgvector-${PGVECTOR_VERSION} \
    && make OPTFLAGS="-march=x86-64-v3" \
    && make OPTFLAGS="-march=x86-64-v3" install \
    && mkdir /pgvector-artifacts \
    && cp /usr/lib/postgresql/${PG_MAJOR}/lib/vector.so /pgvector-artifacts/ \
    && cp /usr/share/postgresql/${PG_MAJOR}/extension/vector* /pgvector-artifacts/ \
    && rm -rf /tmp/pgvector-${PGVECTOR_VERSION}

# Final image — CNPG base + pgvector + VectorChord
FROM ghcr.io/cloudnative-pg/postgresql:${PG_MAJOR}-${PG_BASE_TAG}

ARG PG_MAJOR
ARG PG_BASE_TAG
ARG PGVECTOR_VERSION
ARG VECTORCHORD_VERSION

LABEL org.opencontainers.image.description="CNPG PostgreSQL ${PG_MAJOR}-${PG_BASE_TAG} with pgvector ${PGVECTOR_VERSION} and VectorChord ${VECTORCHORD_VERSION}"

USER root

# pgvector
COPY --from=builder /pgvector-artifacts/ /pgvector-artifacts/
RUN cp /pgvector-artifacts/vector.so /usr/lib/postgresql/${PG_MAJOR}/lib/ \
    && cp /pgvector-artifacts/vector*.control /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && cp /pgvector-artifacts/vector*.sql /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && rm -rf /pgvector-artifacts

# VectorChord — zip layout: pkglibdir/vchord.so  sharedir/extension/*.{control,sql}
# (pgvecto.rs removed: Immich v3 dropped pgvecto.rs support and now runs on VectorChord.)
#
# Org is supervc-stack, not tensorchord: the repo was transferred (same repo, not a
# fork). GitHub still redirects the old path, so builds pinned to tensorchord kept
# working -- but that redirect dies the moment anyone creates a repo at the old
# name, which would break this build with a 404 at an arbitrary future date.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && curl -fsSL \
        "https://github.com/supervc-stack/VectorChord/releases/download/${VECTORCHORD_VERSION}/postgresql-${PG_MAJOR}-vchord_${VECTORCHORD_VERSION}_x86_64-linux-gnu.zip" \
        -o /tmp/vchord.zip \
    && unzip /tmp/vchord.zip -d /tmp/vchord \
    && cp /tmp/vchord/pkglibdir/vchord.so /usr/lib/postgresql/${PG_MAJOR}/lib/ \
    && cp /tmp/vchord/sharedir/extension/* /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && rm -rf /tmp/vchord /tmp/vchord.zip \
    && apt-get purge -y curl unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

USER 26
