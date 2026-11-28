#!/usr/bin/env bash
# Creates the two Hermes SealedSecrets. Run from the repo root:
#
#   kubernetes/apps/hermes/seal-secrets.sh            # gateway + sandbox
#   kubernetes/apps/hermes/seal-secrets.sh dashboard  # desktop-app login only
#
# `dashboard` prompts for a username and password, generates the session
# secret, writes kubernetes/apps/hermes/hermes/app/hermes-dashboard-sealed.yml
# and touches nothing else (no key rotation).
#
# Prompts (input hidden) for the four values, generates the two SSH keypairs
# itself, and writes:
#   kubernetes/apps/hermes/hermes/app/hermes-sealed.yml    (Hermes pod)
#   kubernetes/apps/hermes/sandbox/app/sandbox-sealed.yml  (sandbox pod)
# Plaintext lives only in a mktemp dir that is shredded on exit; nothing is
# echoed. Re-running rotates everything, including both SSH keys (Hermes's
# known_hosts entry for the sandbox then needs clearing — see hermes.md).
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
cert=kubernetes/flux/pub-cert.pem
seal() { mise exec -- kubeseal --cert "$cert" --format yaml; }

work=$(mktemp -d)
trap 'find "$work" -type f -exec shred -u {} + 2>/dev/null; rm -rf "$work"' EXIT
chmod 700 "$work"

ask() { # ask VAR "prompt"
  local v
  read -rsp "$2: " v; echo >&2
  [ -n "$v" ] || { echo "empty value for $1" >&2; exit 1; }
  printf '%s' "$v" > "$work/$1"
}

if [ "${1:-}" = dashboard ]; then
  echo "Hermes desktop-app login. Input is hidden." >&2
  ask username "Dashboard username"
  ask password "Dashboard password (long; LAN/Netbird only, still the only gate)"
  ask password2 "Repeat the password"
  cmp -s "$work/password" "$work/password2" || { echo "passwords differ" >&2; exit 1; }
  openssl rand -hex 32 | tr -d '\n' > "$work/session-secret"
  mise exec -- kubectl create secret generic hermes-dashboard-secret --namespace hermes --dry-run=client -o yaml \
    --from-file=username="$work/username" \
    --from-file=password="$work/password" \
    --from-file=session-secret="$work/session-secret" \
    | seal > kubernetes/apps/hermes/hermes/app/hermes-dashboard-sealed.yml
  echo "Wrote hermes-dashboard-sealed.yml. Commit it." >&2
  exit 0
fi

cat >&2 <<'EOF'
Hermes secrets. See design/decisions/hermes.md → "Owner setup" for where each
value comes from. Input is hidden.
EOF
ask TELEGRAM_BOT_TOKEN     "Telegram bot token (new bot from @BotFather, not the alerts bot)"
ask TELEGRAM_ALLOWED_USERS "Your numeric Telegram user id"
ask LITELLM_API_KEY        "LiteLLM virtual key (allowed model: hermes)"
ask GITEA_TOKEN            "Gitea token of the 'hermes' user"

ssh-keygen -q -t ed25519 -N '' -C hermes-gateway -f "$work/client"
ssh-keygen -q -t ed25519 -N '' -C hermes-sandbox -f "$work/host"

# git credential-store format; the Gitea host is the in-cluster Service.
printf 'http://hermes:%s@gitea-http.gitea.svc.cluster.local:3000\n' "$(cat "$work/GITEA_TOKEN")" \
  > "$work/git-credentials"

mise exec -- kubectl create secret generic hermes-secret --namespace hermes --dry-run=client -o yaml \
  --from-file=TELEGRAM_BOT_TOKEN="$work/TELEGRAM_BOT_TOKEN" \
  --from-file=TELEGRAM_ALLOWED_USERS="$work/TELEGRAM_ALLOWED_USERS" \
  --from-file=LITELLM_API_KEY="$work/LITELLM_API_KEY" \
  --from-file=GITEA_TOKEN="$work/GITEA_TOKEN" \
  --from-file=id_ed25519="$work/client" \
  | seal > kubernetes/apps/hermes/hermes/app/hermes-sealed.yml

mise exec -- kubectl create secret generic hermes-sandbox-secret --namespace hermes --dry-run=client -o yaml \
  --from-file=ssh_host_ed25519_key="$work/host" \
  --from-file=authorized_keys="$work/client.pub" \
  --from-file=git-credentials="$work/git-credentials" \
  | seal > kubernetes/apps/hermes/sandbox/app/sandbox-sealed.yml

echo "Wrote hermes-sealed.yml and sandbox-sealed.yml. Commit both." >&2
