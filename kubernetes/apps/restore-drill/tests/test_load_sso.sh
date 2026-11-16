#!/usr/bin/env bash
# Tests for scripts/load-sso.sh with the Postgres client tools and curl stubbed.
# Run: bash kubernetes/apps/restore-drill/tests/test_load_sso.sh
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=harness.sh
source "$HERE/harness.sh"
SCRIPT="$SCRIPTS/load-sso.sh"

setup() {
  mkdir -p "$T/bin" "$T/scratch/keycloak/db" "$T/scratch/gitea/db" "$T/run"
  : > "$T/scratch/keycloak/db/keycloak.pgdump"; : > "$T/scratch/gitea/db/gitea.pgdump"
  printf 'DRILL_DATE=%s\nGOTIFY_IP=10.96.0.9\n' "$(date -u +%Y-%m-%d)" > "$T/scratch/READY"
  for tool in pg_isready createdb pg_restore; do
    printf '#!/usr/bin/env bash\necho "%s $*" >> "%s/pg.log"\n[[ "%s" == "${STUB_FAIL_TOOL:-}" ]] && { echo "%s: stub failure"; exit 1; }\nexit 0\n' \
      "$tool" "$T" "$tool" "$tool" > "$T/bin/$tool"
  done
  printf '#!/usr/bin/env bash\necho "psql $*" >> "%s/pg.log"\necho "${STUB_DATA_VERSION-26.8.1}"\n' "$T" > "$T/bin/psql"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" >> "%s/curl.args"\n' "$T" > "$T/bin/curl"
  chmod +x "$T/bin/"*
  : > "$T/pg.log"; : > "$T/curl.args"
}

run_script() {
  SCRATCH="$T/scratch" RUN_DIR="$T/run" LOG="$T/run.log" GOTIFY_TOKEN=tok \
    KEYCLOAK_IMAGE_VERSION=26.8.1 PATH="$T/bin:$PATH" bash "$SCRIPT" >"$T/stdout" 2>&1
}

test_loads_both_dumps() {
  setup
  yes run_script
  local log; log=$(cat "$T/pg.log")
  contains "$log" "createdb -h 127.0.0.1 -U postgres keycloak"
  contains "$log" "createdb -h 127.0.0.1 -U postgres gitea"
  contains "$log" "pg_restore -h 127.0.0.1 -U postgres --no-owner --no-acl -d keycloak $T/scratch/keycloak/db/keycloak.pgdump"
  contains "$log" "pg_restore -h 127.0.0.1 -U postgres --no-owner --no-acl -d gitea $T/scratch/gitea/db/gitea.pgdump"
  no test -e "$T/run/STALE"
  eq "$(cat "$T/curl.args")" ""
}

test_missing_marker_fails_before_touching_the_database() {
  setup
  rm "$T/scratch/READY"
  no run_script
  eq "$(cat "$T/pg.log")" ""
}

test_last_weeks_marker_fails_and_reports() {
  setup
  printf 'DRILL_DATE=2020-01-01\nGOTIFY_IP=10.96.0.9\n' > "$T/scratch/READY"
  no run_script
  eq "$(cat "$T/pg.log")" ""
  local args; args=$(cat "$T/curl.args")
  contains "$args" "http://10.96.0.9/message"
  contains "$args" '"title":"Restore drill: sso ✗"'
  contains "$args" "no restore from today"
}

test_restore_error_fails_and_reports() {
  setup
  export STUB_FAIL_TOOL=pg_restore
  no run_script
  contains "$(cat "$T/curl.args")" '"title":"Restore drill: sso ✗"'
  contains "$(cat "$T/curl.args")" "pg_restore: stub failure"
}

test_older_image_writes_stale_and_still_succeeds() {
  setup
  export STUB_DATA_VERSION=26.9.0
  yes run_script
  contains "$(cat "$T/run/STALE")" "keycloak image 26.8.1 is older than the restored data (26.9.0)"
}

run_tests
