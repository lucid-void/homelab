ARG ALPINE_VERSION=3.21
ARG KUBECTL_VERSION=1.32.0

FROM alpine:${ALPINE_VERSION}

ARG KUBECTL_VERSION

RUN apk add --no-cache \
        bash \
        curl \
        ca-certificates \
        restic \
        rclone \
        postgresql17-client \
    && curl -fsSL \
        "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

USER nobody
