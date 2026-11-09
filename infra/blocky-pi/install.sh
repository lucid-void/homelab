#!/usr/bin/env bash
# One-time setup of Blocky on the NUT Pi. Safe to re-run, which is also how changes
# to the units or to blocky-sync reach the Pi:
#   curl -fsSL https://raw.githubusercontent.com/lucid-void/Homelab/main/infra/blocky-pi/install.sh | sudo bash
set -euo pipefail

BASE=https://raw.githubusercontent.com/lucid-void/Homelab/main/infra/blocky-pi

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

case "$(uname -m)" in
  aarch64|x86_64) ;;
  *) echo "unsupported architecture $(uname -m): Blocky here needs a 64-bit OS" >&2; exit 1 ;;
esac

# Something else on port 53 would stop Blocky from binding, unless it is Blocky.
if ss -H -lntup 'sport = :53' | grep -v '"blocky"' | grep -q .; then
  echo "port 53 is already in use:" >&2
  ss -H -lntup 'sport = :53' >&2
  exit 1
fi

fetch() { curl -fsS --max-time 60 --retry 2 -o "$1" "$BASE$2"; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fetch "$tmp/blocky-sync" /usr/local/sbin/blocky-sync
for unit in blocky.service blocky-sync.service blocky-sync.timer; do
  fetch "$tmp/$unit" "/etc/systemd/system/$unit"
done

install -m 0755 "$tmp/blocky-sync" /usr/local/sbin/blocky-sync
for unit in blocky.service blocky-sync.service blocky-sync.timer; do
  install -m 0644 "$tmp/$unit" "/etc/systemd/system/$unit"
done

id blocky >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin blocky

# Blocky refuses to start if this file is missing. The placeholder means "run
# without query logging" until the real DSN is written (design/decisions/dns.md).
mkdir -p /etc/blocky
if [[ ! -f /etc/blocky/querylog-dsn ]]; then
  echo 'postgres://none:none@127.0.0.1:1/none?connect_timeout=1' > /etc/blocky/querylog-dsn
fi
chown root:blocky /etc/blocky/querylog-dsn
chmod 0640 /etc/blocky/querylog-dsn

# Firewall before Blocky: port 4000 is an unauthenticated API.
mkdir -p /etc/nftables.d
fetch /etc/nftables.d/blocky.nft /etc/nftables.d/blocky.nft
line='include "/etc/nftables.d/blocky.nft"'
grep -qxF "$line" /etc/nftables.conf || echo "$line" >> /etc/nftables.conf
nft -c -f /etc/nftables.conf
nft -f /etc/nftables.conf

systemctl daemon-reload
systemctl enable blocky.service

/usr/local/sbin/blocky-sync
systemctl enable --now blocky-sync.timer

echo
echo "Blocky is running. This Pi resolves through:"
grep '^nameserver' /etc/resolv.conf
echo "Those must not be only this Pi: blocky-sync needs DNS that works when Blocky doesn't."
