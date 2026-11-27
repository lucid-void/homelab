#!/bin/sh
# Starts the sandbox's sshd in the foreground. Runs as uid 1000 (agent).
set -eu

key=/etc/hermes-sandbox/ssh/ssh_host_ed25519_key
if [ ! -r "$key" ]; then
  echo "[hermes-sandbox] missing host key at $key (sandbox SealedSecret not mounted?)" >&2
  exit 1
fi
if [ ! -r /etc/hermes-sandbox/ssh/authorized_keys ]; then
  echo "[hermes-sandbox] missing authorized_keys" >&2
  exit 1
fi

# The home directory is a PVC; seed shell init on first boot only.
[ -f "$HOME/.bashrc" ] || cp /etc/skel/.bashrc "$HOME/.bashrc" 2>/dev/null || true
if ! grep -q 'mise activate' "$HOME/.bashrc" 2>/dev/null; then
  echo 'eval "$(mise activate bash)"' >> "$HOME/.bashrc"
fi

# sshd refuses a group/world-readable host key; the Secret volume is mounted
# 0400 but copy to tmp to be independent of fsGroup handling.
install -m 0600 "$key" /tmp/ssh_host_ed25519_key
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config -o HostKey=/tmp/ssh_host_ed25519_key
