ARG PG_MAJOR=17
ARG PG_BASE_TAG=bookworm
ARG PGVECTOR_VERSION=0.8.2
ARG VECTORCHORD_VERSION=1.1.1
ARG PGVECTORS_VERSION=0.3.0

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

RUN curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
        | tar -xz -C /tmp \
    && cd /tmp/pgvector-${PGVECTOR_VERSION} \
    && make \
    && make install \
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
ARG PGVECTORS_VERSION

LABEL org.opencontainers.image.description="CNPG PostgreSQL ${PG_MAJOR}-${PG_BASE_TAG} with pgvector ${PGVECTOR_VERSION} and VectorChord ${VECTORCHORD_VERSION}"

USER root

# pgvector
COPY --from=builder /pgvector-artifacts/ /pgvector-artifacts/
RUN cp /pgvector-artifacts/vector.so /usr/lib/postgresql/${PG_MAJOR}/lib/ \
    && cp /pgvector-artifacts/vector*.control /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && cp /pgvector-artifacts/vector*.sql /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && rm -rf /pgvector-artifacts

# VectorChord + pgvecto.rs — single layer to keep image size down
# VectorChord zip layout: pkglibdir/vchord.so  sharedir/extension/*.{control,sql}
# pgvecto.rs deb provides vectors.so; required by Immich's InitialMigration even when
# DB_VECTOR_EXTENSION=pgvector, because the migration unconditionally runs
# CREATE EXTENSION IF NOT EXISTS "vectors" before the pgvector code path.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && curl -fsSL \
        "https://github.com/tensorchord/VectorChord/releases/download/${VECTORCHORD_VERSION}/postgresql-${PG_MAJOR}-vchord_${VECTORCHORD_VERSION}_x86_64-linux-gnu.zip" \
        -o /tmp/vchord.zip \
    && unzip /tmp/vchord.zip -d /tmp/vchord \
    && cp /tmp/vchord/pkglibdir/vchord.so /usr/lib/postgresql/${PG_MAJOR}/lib/ \
    && cp /tmp/vchord/sharedir/extension/* /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && curl -fsSL \
        "https://github.com/tensorchord/pgvecto.rs/releases/download/v${PGVECTORS_VERSION}/vectors-pg${PG_MAJOR}-linux_${PGVECTORS_VERSION}_amd64.deb" \
        -o /tmp/vectors.deb \
    && dpkg-deb --extract /tmp/vectors.deb /tmp/vectors-pkg \
    && cp /tmp/vectors-pkg/usr/lib/postgresql/${PG_MAJOR}/lib/vectors.so /usr/lib/postgresql/${PG_MAJOR}/lib/ \
    && cp /tmp/vectors-pkg/usr/share/postgresql/${PG_MAJOR}/extension/vectors* /usr/share/postgresql/${PG_MAJOR}/extension/ \
    && rm -rf /tmp/vchord /tmp/vchord.zip /tmp/vectors.deb /tmp/vectors-pkg \
    && apt-get purge -y curl unzip \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

USER 26
