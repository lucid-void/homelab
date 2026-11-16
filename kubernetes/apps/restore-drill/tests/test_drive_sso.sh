#!/usr/bin/env bash
# Tests for scripts/drive-sso.sh with curl and psql stubbed.
# Run: bash kubernetes/apps/restore-drill/tests/test_drive_sso.sh
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../app/scripts/drive-sso.sh"
# shellcheck source=harness.sh
source "$HERE/harness.sh"

setup() {
  mkdir -p "$T/bin" "$T/scratch" "$T/run" "$T/tls" "$T/user" "$T/tmp"
  echo cert > "$T/tls/sso.crt"; echo cert > "$T/tls/gitea.crt"
  printf 'restore-drill' > "$T/user/username"; printf 'p@ss word' > "$T/user/password"
  cat > "$T/scratch/READY" <<EOF
DRILL_DATE=$(date -u +%Y-%m-%d)
GOTIFY_IP=10.96.0.9
SNAPTIME_KEYCLOAK=2026-11-21T03:30:41Z
SNAPTIME_GITEA=2026-11-21T05:00:33Z
EOF
  cat > "$T/run/fsck.env" <<'EOF'
REPOS_ON_DISK=7
FSCK_REPO=lucid-void/homelab.git
FSCK_RC=0
FSCK_COMMITS=120
EOF

  # curl stub: answers by URL, honours -o and the two -w formats the script uses.
  cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
out="" fmt="" url="" prev=""
for a in "$@"; do
  case "$prev" in -o) out=$a ;; -w) fmt=$a ;; esac
  case "$a" in http://*|https://*) url=$a ;; esac
  prev=$a
done
echo "$url" >> "$STUB_DIR/curl.urls"
body="" eff=$url code=200
case "$url" in
  http://10.96.0.9/message) printf '%s\n' "$@" >> "$STUB_DIR/gotify.args"; exit 0 ;;
  */.well-known/openid-configuration) body='{"issuer":"https://sso.example.com/realms/homelab"}' ;;
  */api/healthz) body='{"status":"pass"}' ;;
  */user/oauth2/Keycloak)
    eff="https://sso.example.com/realms/homelab/protocol/openid-connect/auth?client_id=gitea"
    body='<html><title>Sign in to homelab</title><form id="kc-form-login" action="https://sso.example.com/realms/homelab/login-actions/authenticate?session_code=abc&amp;tab_id=xyz" method="post">' ;;
  */login-actions/authenticate*)
    printf '%s\n' "$@" >> "$STUB_DIR/login.args"
    if [[ "${STUB_LOGIN:-ok}" == "ok" ]]; then
      eff="https://gitea.example.com/"; body='<html><title>Gitea</title>'
    else
      eff="https://sso.example.com/realms/homelab/login-actions/authenticate?execution=e1"
      body='<html><title>Sign in to homelab</title><span>Invalid username or password.</span>'
    fi ;;
  */user/settings) [[ "${STUB_LOGIN:-ok}" == "ok" ]] && code=200 || code=303 ;;
esac
if [[ -n "$out" ]]; then printf '%s' "$body" > "$out"; else printf '%s' "$body"; fi
case "$fmt" in '%{url_effective}') printf '%s' "$eff" ;; '%{http_code}') printf '%s' "$code" ;; esac
exit 0
EOF
  cat > "$T/bin/psql" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *user_entity*) echo "${STUB_USERS-12}" ;;
  *repository*) echo "${STUB_REPOS-7}" ;;
esac
EOF
  chmod +x "$T/bin/"*
  export STUB_DIR="$T"
  : > "$T/curl.urls"; : > "$T/gotify.args"; : > "$T/login.args"
}

run_script() {
  SCRATCH="$T/scratch" RUN_DIR="$T/run" TLS_DIR="$T/tls" USER_DIR="$T/user" TMP_DIR="$T/tmp" \
    LOG="$T/run.log" BOOT_TIMEOUT=2 GOTIFY_TOKEN=tok MIN_KEYCLOAK_USERS=5 MIN_GITEA_REPOS=3 \
    PATH="$T/bin:$PATH" bash "$SCRIPT" >"$T/stdout" 2>&1
}

test_pass_message() {
  setup
  yes run_script
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: sso ✓"'
  contains "$g" '"priority":5'
  contains "$g" "12 Keycloak users"
  contains "$g" "7 Gitea repositories"
  contains "$g" "2026-11-21T03:30:41Z"
  contains "$g" "2026-11-21T05:00:33Z"
  contains "$g" "lucid-void/homelab.git"
  eq "$(grep -c '"title"' "$T/gotify.args")" "1"
}

test_login_posts_the_drill_credentials_to_the_form_action() {
  setup
  yes run_script
  local l; l=$(cat "$T/login.args")
  # &amp; in the HTML attribute must be decoded before use
  contains "$l" "https://sso.example.com/realms/homelab/login-actions/authenticate?session_code=abc&tab_id=xyz"
  contains "$l" "username=restore-drill"
  contains "$l" "password=p@ss word"
}

test_stale_wins() {
  setup
  echo "keycloak image 26.8.1 is older than the restored data (26.9.0)" > "$T/run/STALE"
  run_script; eq "$?" "3"
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: sso stale — bump the image"'
  contains "$g" '"priority":8'
  contains "$g" "older than the restored data (26.9.0)"
  eq "$(cat "$T/login.args")" ""
  eq "$(grep -c '"title"' "$T/gotify.args")" "1"
}

test_floor_fails() {
  setup
  export STUB_USERS=0
  no run_script
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: sso ✗"'
  contains "$g" "is below the floor"
  eq "$(cat "$T/login.args")" ""
}

test_login_failure_names_the_page() {
  setup
  export STUB_LOGIN=fail
  no run_script
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: sso ✗"'
  contains "$g" "login stopped at: https://sso.example.com/realms/homelab/login-actions/authenticate?execution=e1"
  contains "$g" "Sign in to homelab"
}

test_missing_drill_user_says_so() {
  setup
  rm "$T/user/username" "$T/user/password"
  no run_script
  contains "$(cat "$T/gotify.args")" "the drill-user Secret is missing"
  eq "$(cat "$T/login.args")" ""
}

test_repo_count_mismatch_fails() {
  setup
  sed -i 's/REPOS_ON_DISK=7/REPOS_ON_DISK=5/' "$T/run/fsck.env"
  no run_script
  contains "$(cat "$T/gotify.args")" "5 repositories on disk but 7 in the database"
}

test_fsck_failure_fails() {
  setup
  sed -i 's/FSCK_RC=0/FSCK_RC=1/' "$T/run/fsck.env"
  no run_script
  contains "$(cat "$T/gotify.args")" "git fsck failed on lucid-void/homelab.git"
}

test_app_that_never_comes_up_fails() {
  setup
  sed -i 's#{"status":"pass"}#{"status":"fail"}#' "$T/bin/curl"
  no run_script
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: sso ✗"'
  contains "$g" "Gitea did not come up"
}

run_tests
