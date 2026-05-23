ARG ALPINE_VERSION=3.23
ARG KUBECTL_VERSION=1.32.0
ARG RCLONE_VERSION=1.74.1

FROM alpine:${ALPINE_VERSION}

ARG KUBECTL_VERSION
ARG RCLONE_VERSION

RUN apk add --no-cache \
        bash \
        curl \
        unzip \
        ca-certificates \
        restic \
        postgresql17-client \
    && curl -fsSL \
        "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" \
        -o /tmp/rclone.zip \
    && unzip -q /tmp/rclone.zip -d /tmp/rclone \
    && mv /tmp/rclone/rclone-v${RCLONE_VERSION}-linux-amd64/rclone /usr/local/bin/rclone \
    && chmod +x /usr/local/bin/rclone \
    && rm -rf /tmp/rclone.zip /tmp/rclone \
    && curl -fsSL \
        "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

USER nobody
