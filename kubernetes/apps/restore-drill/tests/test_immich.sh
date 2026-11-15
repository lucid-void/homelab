#!/usr/bin/env bash
# Tests for scripts/load-immich.sh and scripts/drive-immich.sh with the
# Postgres tools and curl stubbed. sha1sum is real, on real temp files.
# Run: bash kubernetes/apps/restore-drill/tests/test_immich.sh
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
LOAD="$HERE/../app/scripts/load-immich.sh"
DRIVE="$HERE/../app/scripts/drive-immich.sh"
# shellcheck source=harness.sh
source "$HERE/harness.sh"

# setup <files sampled> <files that have a database row>
setup() {
  local sampled=${1:-6} with_row=${2:-6} i p
  mkdir -p "$T/bin" "$T/scratch/immich/db" "$T/scratch/immich/library/upload/u/aa" "$T/run" "$T/tmp"
  : > "$T/scratch/immich/db/immich.pgdump"
  cat > "$T/scratch/READY" <<EOF
DRILL_DATE=$(date -u +%Y-%m-%d)
GOTIFY_IP=10.96.0.9
SNAPTIME_IMMICH=2026-11-21T03:00:12Z
EOF
  : > "$T/scratch/immich/sample.txt"; : > "$T/checksums.tsv"
  for i in $(seq 1 "$sampled"); do
    p="/library/upload/u/aa/f$i.jpg"
    echo "content $i" > "$T/scratch/immich$p"
    echo "$p" >> "$T/scratch/immich/sample.txt"
    if (( i <= with_row )); then
      # database path -> hex sha1, as the asset table would hold it
      printf '%s\t%s\n' "/usr/src/app/upload/upload/u/aa/f$i.jpg" "$(sha1sum "$T/scratch/immich$p" | cut -d' ' -f1)" >> "$T/checksums.tsv"
    fi
  done

  for tool in pg_isready createdb pg_restore; do
    printf '#!/usr/bin/env bash\necho "%s $*" >> "%s/pg.log"\n[[ "%s" == "${STUB_FAIL_TOOL:-}" ]] && { echo "%s: stub failure"; exit 1; }\nexit 0\n' \
      "$tool" "$T" "$tool" "$tool" > "$T/bin/$tool"
  done
  # psql stub: version query, asset count, or a checksum lookup by -v p=<path>
  cat > "$T/bin/psql" <<'EOF'
#!/usr/bin/env bash
sql=$(cat 2>/dev/null </dev/stdin || true); all="$* $sql" path=""
for a in "$@"; do case "$a" in p=*) path=${a#p=} ;; esac; done
case "$all" in
  *version_history*) echo "${STUB_DATA_VERSION-3.3.5}" ;;
  *"count(*)"*) echo "${STUB_ASSETS-11908}" ;;
  *checksum*) awk -F'\t' -v p="$path" '$1 == p { print $2 }' "$STUB_DIR/checksums.tsv" ;;
esac
EOF
  cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
url=""; for a in "$@"; do case "$a" in http://*) url=$a ;; esac; done
case "$url" in
  http://10.96.0.9/message) printf '%s\n' "$@" >> "$STUB_DIR/gotify.args" ;;
  */api/server/ping) [[ "${STUB_PING:-ok}" == "ok" ]] && printf '{"res":"pong"}' || exit 7 ;;
esac
EOF
  chmod +x "$T/bin/"*
  export STUB_DIR="$T"
  : > "$T/pg.log"; : > "$T/gotify.args"
}

env_run() {
  SCRATCH="$T/scratch" RUN_DIR="$T/run" TMP_DIR="$T/tmp" LOG="$T/run.log" BOOT_TIMEOUT=2 \
    GOTIFY_TOKEN=tok IMMICH_IMAGE_VERSION=v3.3.5 MIN_IMMICH_ASSETS=100 \
    MIN_SAMPLE_MATCHED="${MIN_SAMPLE_MATCHED:-4}" PATH="$T/bin:$PATH" bash "$1" >"$T/stdout" 2>&1 </dev/null
}
run_load() { env_run "$LOAD"; }
run_drive() { env_run "$DRIVE"; }
scratch_is_empty() { [[ -z "$(ls -A "$T/scratch")" ]]; }

# --- load-immich.sh -----------------------------------------------------------

test_load_restores_the_dump() {
  setup
  yes run_load
  contains "$(cat "$T/pg.log")" "createdb -h 127.0.0.1 -U postgres immich"
  contains "$(cat "$T/pg.log")" "pg_restore -h 127.0.0.1 -U postgres --no-owner --no-acl -d immich $T/scratch/immich/db/immich.pgdump"
  no test -e "$T/run/STALE"
}

test_load_refuses_last_weeks_marker() {
  setup
  printf 'DRILL_DATE=2020-01-01\nGOTIFY_IP=10.96.0.9\n' > "$T/scratch/READY"
  no run_load
  eq "$(cat "$T/pg.log")" ""
  contains "$(cat "$T/gotify.args")" '"title":"Restore drill: immich ✗"'
}

test_load_failure_wipes_the_scratch_volume() {
  setup
  export STUB_FAIL_TOOL=pg_restore
  no run_load
  contains "$(cat "$T/gotify.args")" "pg_restore: stub failure"
  yes scratch_is_empty
}

test_load_writes_stale_for_an_older_image() {
  setup
  export STUB_DATA_VERSION=3.4.0
  yes run_load
  contains "$(cat "$T/run/STALE")" "immich image v3.3.5 is older than the restored data (3.4.0)"
}

# --- drive-immich.sh ----------------------------------------------------------

test_pass() {
  setup 6 6
  yes run_drive
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: immich ✓"'
  contains "$g" '"priority":5'
  contains "$g" "11908 assets"
  contains "$g" "6 of 6 sampled files matched"
  contains "$g" "2026-11-21T03:00:12Z"
}

test_skips_unmatched() {
  setup 6 5
  yes run_drive
  contains "$(cat "$T/gotify.args")" "5 of 6 sampled files matched"
}

test_too_few_matched() {
  setup 6 3
  no run_drive
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: immich ✗"'
  contains "$g" "3 of 6 matched, need 4"
}

test_mismatch_fails() {
  setup 6 6
  echo "bit rot" >> "$T/scratch/immich/library/upload/u/aa/f3.jpg"
  no run_drive
  local g; g=$(cat "$T/gotify.args")
  contains "$g" '"title":"Restore drill: immich ✗"'
  contains "$g" "checksum mismatch: /library/upload/u/aa/f3.jpg"
}

test_sampled_file_not_restored_fails() {
  setup 6 6
  rm "$T/scratch/immich/library/upload/u/aa/f2.jpg"
  no run_drive
  contains "$(cat "$T/gotify.args")" "sampled file was not restored: /library/upload/u/aa/f2.jpg"
}

test_asset_floor() {
  setup
  export STUB_ASSETS=3
  no run_drive
  contains "$(cat "$T/gotify.args")" "Immich assets: 3 is below the floor of 100"
}

test_server_that_never_answers_fails() {
  setup
  export STUB_PING=down
  no run_drive
  contains "$(cat "$T/gotify.args")" "Immich did not come up"
}

test_stale() {
  setup
  echo "immich image v3.3.5 is older than the restored data (3.4.0)" > "$T/run/STALE"
  run_drive; eq "$?" "3"
  contains "$(cat "$T/gotify.args")" '"title":"Restore drill: immich stale — bump the image"'
  eq "$(grep -c '"title"' "$T/gotify.args")" "1"
}

test_wipes_scratch_on_pass() {
  setup
  yes run_drive
  yes scratch_is_empty
}

test_wipes_scratch_on_failure() {
  setup 6 6
  echo "bit rot" >> "$T/scratch/immich/library/upload/u/aa/f3.jpg"
  no run_drive
  yes scratch_is_empty
}

test_wipes_scratch_when_stale() {
  setup
  echo "stale" > "$T/run/STALE"
  run_drive
  yes scratch_is_empty
}

run_tests
