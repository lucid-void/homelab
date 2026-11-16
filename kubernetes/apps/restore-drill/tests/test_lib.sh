#!/usr/bin/env bash
# Unit tests for scripts/lib.sh. Run: bash kubernetes/apps/restore-drill/tests/test_lib.sh
# Needs bash, jq, awk and shuf — the same tools the backup-tools image carries.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=harness.sh
source "$HERE/harness.sh"
source "$SCRIPTS/lib.sh" || { echo "FAIL: cannot source lib.sh"; exit 1; }

test_check_subset() {
  eq "$(check_subset 1)" "1/52"
  eq "$(check_subset 52)" "52/52"
  eq "$(check_subset 53)" "1/52"
  eq "$(check_subset 08)" "8/52"   # date +%V zero-pads; must not be read as octal
}

test_snapshot_fresh() {
  local now=1795255200   # 2026-11-21T10:00:00Z
  yes snapshot_fresh "2026-11-20T05:00:10Z" "$now"
  yes snapshot_fresh "2026-11-20T05:00:10.123456789Z" "$now"   # restic prints nanoseconds
  no  snapshot_fresh "2026-11-19T09:59:00Z" "$now"
  no  snapshot_fresh "not-a-date" "$now"
  no  snapshot_fresh "" "$now"
}

test_marker_is_today() {
  printf 'DRILL_DATE=2026-11-21\nGOTIFY_IP=10.96.0.9\n' > "$T/READY"
  yes marker_is_today "$T/READY" 2026-11-21
  no  marker_is_today "$T/READY" 2026-11-28
  no  marker_is_today "$T/missing" 2026-11-21
}

test_floor() {
  yes floor users 12 5
  yes floor users 5 5
  no  floor users 0 1
  no  floor users "" 1
  eq "$(floor repos 0 3 || true)" "repos: 0 is below the floor of 3"
}

write_ls_fixture() {
  cat > "$T/ls.json" <<'EOF'
{"time":"2026-11-20T03:00:00Z","paths":["/db/immich.pgdump","/library"],"struct_type":"snapshot"}
{"name":"library","type":"dir","path":"/library","struct_type":"node"}
{"name":"a.jpg","type":"file","path":"/library/upload/u1/a.jpg","size":10,"struct_type":"node"}
{"name":"b.jpg","type":"file","path":"/library/upload/u1/b.jpg","size":10,"struct_type":"node"}
{"name":"c.jpg","type":"file","path":"/library/library/u1/c.jpg","size":10,"struct_type":"node"}
{"name":"t.webp","type":"file","path":"/library/thumbs/u1/t.webp","size":10,"struct_type":"node"}
{"name":".immich","type":"file","path":"/library/upload/.immich","size":13,"struct_type":"node"}
EOF
}

test_pick_sample_size_cap() {
  write_ls_fixture
  eq "$(pick_sample 200 25 < "$T/ls.json" | wc -l | tr -d ' ')" "2"
}

test_pick_sample_count_cap() {
  write_ls_fixture
  eq "$(pick_sample 1 1000 < "$T/ls.json" | wc -l | tr -d ' ')" "1"
}

test_pick_sample_folder_filter() {
  write_ls_fixture
  local out; out=$(pick_sample 200 1000 < "$T/ls.json" | sort)
  eq "$out" "/library/library/u1/c.jpg
/library/upload/u1/a.jpg
/library/upload/u1/b.jpg"
}

test_glob_escape() {
  # restic --include-file lines are patterns; a literal path must survive as itself
  eq "$(echo '/library/library/u/trip [2024]/a*b?.jpg' | glob_escape)" '/library/library/u/trip \[2024\]/a\*b\?.jpg'
  eq "$(echo '/library/upload/u/aa/plain.jpg' | glob_escape)" '/library/upload/u/aa/plain.jpg'
}

test_version_older() {
  yes version_older 26.3.1 26.4.0
  yes version_older 3.3.5 3.10.0     # numeric, not string, comparison
  no  version_older v3.3.5 3.3.5
  no  version_older 26.4.0 26.3.9
  no  version_older 26.4.0 ""        # unknown data version is not "stale"
}

test_fail_body_is_valid_json() {
  printf 'line one\na\tb "c" \\d\n' > "$T/log"
  local body; body=$(fail_body "$T/log")
  yes jq -e . <<<"{\"m\":\"$body\"}"
  contains "$body" 'b \"c\"'
}

test_fail_body_keeps_only_the_tail() {
  seq 1 30 > "$T/log"
  local body; body=$(fail_body "$T/log")
  contains "$body" '21\n'
  lacks "$body" '20\n'
}

test_notify_gotify_posts_to_the_ip() {
  mkdir -p "$T/bin"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" > "%s/curl.args"\n' "$T" > "$T/bin/curl"
  chmod +x "$T/bin/curl"
  GOTIFY_TOKEN=tok PATH="$T/bin:$PATH" notify_gotify 10.96.0.9 "Restore drill: sso ✓" "hello" 5
  local args; args=$(cat "$T/curl.args")
  contains "$args" "http://10.96.0.9/message"
  contains "$args" "X-Gotify-Key: tok"
  contains "$args" '"title":"Restore drill: sso ✓"'
  contains "$args" '"priority":5'
}

test_notify_gotify_never_fails_the_caller() {
  mkdir -p "$T/bin"
  printf '#!/usr/bin/env bash\nexit 22\n' > "$T/bin/curl"
  chmod +x "$T/bin/curl"
  yes env GOTIFY_TOKEN=tok PATH="$T/bin:$PATH" bash -c "source '$SCRIPTS/lib.sh'; notify_gotify 10.96.0.9 t m 8"
}

run_tests
