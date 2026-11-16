#!/usr/bin/env bash
# drill-sso, main container: wait for the restored Keycloak and Gitea to come
# up inside this pod, check them, and report. Its exit code is the Job's result.
#
# Checks, in order: image-older-than-data short-circuit, both apps answer on
# their real hostnames, the restored data is not empty, a real SSO login to
# Gitea through Keycloak as the drill user, and Gitea's repositories are on
# disk and pass git fsck. Tests: tests/test_drive_sso.sh
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SCRATCH=$${SCRATCH:-/scratch}
RUN_DIR=$${RUN_DIR:-/run/drill}
TLS_DIR=$${TLS_DIR:-/tls}
USER_DIR=$${USER_DIR:-/secrets/drill-user}
TMP_DIR=$${TMP_DIR:-/tmp}
LOG=$${LOG:-/tmp/drive-sso.log}
BOOT_TIMEOUT=$${BOOT_TIMEOUT:-900}
MIN_KEYCLOAK_USERS=$${MIN_KEYCLOAK_USERS:-1}
MIN_GITEA_REPOS=$${MIN_GITEA_REPOS:-1}
PG=(-h 127.0.0.1 -U postgres)
CA="$TMP_DIR/drill-ca.pem"
JAR="$TMP_DIR/cookies.txt"
SSO=https://sso.${CLUSTER_DOMAIN}
GITEA=https://gitea.${CLUSTER_DOMAIN}

exec > >(tee "$LOG") 2>&1

marker() { sed -n "s/^$1=//p" "$SCRATCH/READY"; }
GOTIFY_IP=$(marker GOTIFY_IP)
REPORTED=0

on_exit() {
  local rc=$?
  [[ $rc -eq 0 || $REPORTED -eq 1 ]] && return
  echo "[drill] FAILED with exit $rc"
  notify_gotify "$GOTIFY_IP" "Restore drill: sso ✗" \
    "Exit $${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)\n\n$(fail_body "$LOG")" 8
}
trap on_exit EXIT

# --- 0. An image older than the data proves nothing about the backup ----------
if [[ -s "$RUN_DIR/STALE" ]]; then
  reason=$(head -1 "$RUN_DIR/STALE")
  echo "[drill] STALE: $reason"
  notify_gotify "$GOTIFY_IP" "Restore drill: sso stale — bump the image" \
    "$${reason}. The drill did not run; raise the image tag in cronjob-sso.yml." 8
  REPORTED=1
  exit 3
fi

# Both apps serve their own throwaway certificate; trust exactly those two.
cat "$TLS_DIR/sso.crt" "$TLS_DIR/gitea.crt" > "$CA"
c() { curl -sS -m 30 --cacert "$CA" "$@"; }

# --- 1. Both apps boot on the restored data -----------------------------------
# wait_for <label> <url> <jq filter that must be true>
wait_for() {
  local label=$1 url=$2 filter=$3 deadline=$(( $(date +%s) + BOOT_TIMEOUT ))
  echo "[drill] Waiting for $label"
  until c -f "$url" 2>/dev/null | jq -e "$filter" >/dev/null 2>&1; do
    if (( $(date +%s) > deadline )); then
      echo "[drill] last answer from $url: $(c "$url" 2>&1 | head -c 300 | tr '\n' ' ')"
      echo "[drill] $label did not come up within $${BOOT_TIMEOUT}s (if its image is older than production, bump it)"
      return 1
    fi
    sleep 5
  done
  echo "[drill] $label is up"
}
wait_for "Keycloak" "$SSO/realms/homelab/.well-known/openid-configuration" \
  ".issuer == \"$SSO/realms/homelab\""
wait_for "Gitea" "$GITEA/api/healthz" '.status == "pass"'

# --- 2. The restored data is not empty ----------------------------------------
users=$(psql "$${PG[@]}" -Atc "SELECT count(*) FROM user_entity u JOIN realm r ON r.id = u.realm_id WHERE r.name = 'homelab'" keycloak)
repos=$(psql "$${PG[@]}" -Atc "SELECT count(*) FROM repository" gitea)
floor "Keycloak users" "$users" "$MIN_KEYCLOAK_USERS"
floor "Gitea repositories" "$repos" "$MIN_GITEA_REPOS"
echo "[drill] $users Keycloak users, $repos Gitea repositories"

# --- 3. A real login to Gitea through Keycloak --------------------------------
title_of() { tr '\n' ' ' < "$1" | sed -n 's/.*<title>\([^<]*\)<\/title>.*/\1/p' | head -c 120; }

if [[ ! -s "$USER_DIR/username" || ! -s "$USER_DIR/password" ]]; then
  echo "[drill] the drill-user Secret is missing (keys username, password) — see design/decisions/backups.md"
  exit 1
fi
drill_user=$(cat "$USER_DIR/username")
echo "[drill] Logging in to Gitea through Keycloak as $drill_user"
at=$(c -L -c "$JAR" -b "$JAR" -o "$TMP_DIR/login.html" -w '%{url_effective}' "$GITEA/user/oauth2/Keycloak")
action=$(tr '\n' ' ' < "$TMP_DIR/login.html" | sed -n 's/.*<form[^>]*action="\([^"]*\)".*/\1/p' | sed 's/&amp;/\&/g')
if [[ -z "$action" ]]; then
  echo "[drill] login stopped at: $at — $(title_of "$TMP_DIR/login.html") (no login form on that page)"
  exit 1
fi
at=$(c -L -c "$JAR" -b "$JAR" -o "$TMP_DIR/after.html" -w '%{url_effective}' \
  --data-urlencode "username=$${drill_user}" \
  --data-urlencode "password=$(cat "$USER_DIR/password")" \
  "$action")
code=$(c -b "$JAR" -o /dev/null -w '%{http_code}' "$GITEA/user/settings")
if [[ "$at" != "$GITEA"/* || "$code" != "200" ]]; then
  # Name the page, so a rotated password or a new required action is not
  # mistaken for a broken backup.
  echo "[drill] login stopped at: $at — $(title_of "$TMP_DIR/after.html") (Gitea settings page answered $code)"
  exit 1
fi
echo "[drill] SSO login OK"

# --- 4. Gitea's repositories are really there ---------------------------------
# Written by the `fsck` init container, which has git; this image does not.
REPOS_ON_DISK="" FSCK_REPO="" FSCK_RC="" FSCK_COMMITS=""
# shellcheck disable=SC1091
source "$RUN_DIR/fsck.env"
if [[ "$REPOS_ON_DISK" != "$repos" ]]; then
  echo "[drill] $REPOS_ON_DISK repositories on disk but $repos in the database"
  exit 1
fi
if [[ "$FSCK_RC" != "0" ]]; then
  echo "[drill] git fsck failed on $FSCK_REPO (exit $FSCK_RC)"
  exit 1
fi
floor "commits in $FSCK_REPO" "$FSCK_COMMITS" 1
echo "[drill] git fsck OK on $FSCK_REPO ($FSCK_COMMITS commits)"

notify_gotify "$GOTIFY_IP" "Restore drill: sso ✓" \
  "Keycloak snapshot $(marker SNAPTIME_KEYCLOAK), Gitea snapshot $(marker SNAPTIME_GITEA)\n$${users} Keycloak users, $${repos} Gitea repositories\nSSO login as $${drill_user} OK\ngit fsck OK on $${FSCK_REPO} ($${FSCK_COMMITS} commits)" 5
REPORTED=1
echo "[drill] PASS"
