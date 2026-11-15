#!/usr/bin/env bash
# drill-sso, main container: wait for the restored Keycloak and Gitea to come
# up inside this pod, then check them. Its exit code is the Job's result.
#
# STAGE: boot only. This version proves the two apps start on the restored
# data and answer on their real hostnames. The login, data and floor checks and
# the pass/fail report are the next change (plan Task 5).
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SCRATCH=${SCRATCH:-/scratch}
TLS_DIR=${TLS_DIR:-/tls}
LOG=${LOG:-/tmp/drive-sso.log}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-900}
CA=/tmp/drill-ca.pem

exec > >(tee "$LOG") 2>&1

GOTIFY_IP=$(sed -n 's/^GOTIFY_IP=//p' "$SCRATCH/READY")

on_exit() {
  local rc=$?
  [[ $rc -eq 0 ]] && return
  echo "[drill] FAILED with exit $rc"
  notify_gotify "$GOTIFY_IP" "Restore drill: sso ✗" \
    "Exit ${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)\n\n$(fail_body "$LOG")" 8
}
trap on_exit EXIT

# Both apps serve their own throwaway certificate; trust exactly those two.
cat "$TLS_DIR/sso.crt" "$TLS_DIR/gitea.crt" > "$CA"

# wait_for <label> <url> <jq filter that must be true>
wait_for() {
  local label=$1 url=$2 filter=$3 deadline=$(( $(date +%s) + BOOT_TIMEOUT ))
  echo "[drill] Waiting for $label"
  until curl -fsS -m 10 --cacert "$CA" "$url" 2>/dev/null | jq -e "$filter" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
      echo "[drill] $label did not come up within ${BOOT_TIMEOUT}s; last answer:"
      curl -sS -m 10 --cacert "$CA" "$url" 2>&1 | head -c 400 || true
      echo
      return 1
    fi
    sleep 5
  done
  echo "[drill] $label is up"
}

wait_for "Keycloak" "https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration" \
  '.issuer == "https://sso.blackcats.cc/realms/homelab"'
wait_for "Gitea" "https://gitea.blackcats.cc/api/healthz" '.status == "pass"'

echo "[drill] Both apps booted on the restored data"
