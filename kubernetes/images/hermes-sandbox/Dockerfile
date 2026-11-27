# The shell Hermes Agent runs commands in. Hermes reaches it over SSH
# (terminal.backend: ssh); this pod holds no Hermes secrets, no Kubernetes
# token, and only the git credentials in /etc/hermes-sandbox/git. See
# design/decisions/hermes.md.
#
# sshd runs as the unprivileged `agent` user on 2222: a non-root sshd can only
# log in as itself, which is exactly the one account this image has.
FROM debian:trixie-20261020-slim

ARG MISE_VERSION=2026.11.14
ARG MISE_SHA256=6fa596291e9758106f761fea4f8e7fac6c3e7ff4714d5c0b9a76e16068db14f6

RUN apt-get -o Acquire::Retries=3 update && \
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
      openssh-server openssh-sftp-server \
      bash ca-certificates curl git jq less make procps tar xz-utils unzip \
      python3 python3-venv python-is-python3 nodejs npm && \
    rm -rf /var/lib/apt/lists/* && \
    curl -fsSL --retry 3 -o /usr/local/bin/mise \
      "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-x64" && \
    echo "${MISE_SHA256}  /usr/local/bin/mise" | sha256sum -c - && \
    chmod 0755 /usr/local/bin/mise && \
    useradd --uid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent && \
    passwd -d agent && usermod -p '*' agent && \
    mkdir -p /run/sshd /etc/hermes-sandbox && \
    rm -f /etc/ssh/ssh_host_*

COPY sshd_config /etc/ssh/sshd_config
COPY gitconfig /etc/gitconfig
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0644 /etc/ssh/sshd_config /etc/gitconfig && chmod 0755 /usr/local/bin/entrypoint.sh

USER 1000:1000
WORKDIR /home/agent
EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
