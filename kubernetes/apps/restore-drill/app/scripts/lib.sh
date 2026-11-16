#!/usr/bin/env bash
# Shared functions for the restore-drill scripts. Sourced, never executed.
# Runs in ghcr.io/lucid-void/backup-tools: bash, jq, curl, busybox awk/shuf —
# no python3, no GNU date. Tests: kubernetes/apps/restore-drill/tests/test_lib.sh

# check_subset <iso_week> -> "N/52" for `restic check --read-data-subset`.
# restic rejects n > t, and some years have an ISO week 53, hence the modulo.
check_subset() {
  local week=$((10#$1))
  echo "$(( (week - 1) % 52 + 1 ))/52"
}

# snapshot_fresh <snapshot_time_iso8601> <now_epoch> -> 0 if under 48 h old.
# An unparseable time is treated as stale.
snapshot_fresh() {
  local taken
  taken=$(jq -rn --arg t "$1" '$t | sub("\\.[0-9]+"; "") | fromdateiso8601' 2>/dev/null) || return 1
  [[ "$taken" =~ ^[0-9]+$ ]] || return 1
  (( $2 - taken < 48 * 3600 ))
}

# marker_is_today <marker_file> <today YYYY-MM-DD> -> 0 only if the restore that
# wrote the marker ran today. Guards the boot jobs against last week's data.
marker_is_today() {
  [[ -f "$1" ]] && grep -qx "DRILL_DATE=$2" "$1"
}

# floor <label> <actual> <minimum> -> 0 if actual >= minimum. Guards against a
# backup that restores cleanly but is empty.
floor() {
  local label=$1 actual=$2 minimum=$3
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= minimum )); then
    return 0
  fi
  echo "$${label}: $${actual:-nothing} is below the floor of $${minimum}"
  return 1
}

# pick_sample <count> <max_bytes> : `restic ls --json` on stdin -> up to <count>
# random original files, stopping before their total size passes <max_bytes>.
# Only upload/ and library/ hold originals with a checksum in the database.
pick_sample() {
  local count=$1 max_bytes=$2
  jq -r 'select(.struct_type == "node" and .type == "file")
         | select(.path | test("^/library/(upload|library)/.+/"))
         | "\(.size)\t\(.path)"' \
    | shuf \
    | awk -F'\t' -v n="$count" -v max="$max_bytes" '
        picked >= n { exit }
        total + $1 > max { next }
        { total += $1; picked++; print $2 }'
}

# glob_escape : paths on stdin -> the same paths as literal restic patterns.
# `restic restore --include-file` reads patterns, so [ ] * ? in a real file
# name would otherwise match nothing (or the wrong thing).
glob_escape() {
  sed 's/[][*?\\]/\\&/g'
}

# version_older <image_version> <data_version> -> 0 if the image is older than
# the data it would have to read. An unknown data version is not "older".
version_older() {
  local image=$${1#v} data=$${2#v} i a b
  [[ -n "$data" ]] || return 1
  local -a iv dv
  IFS=. read -ra iv <<<"$image"
  IFS=. read -ra dv <<<"$data"
  for i in 0 1 2; do
    a=$${iv[i]:-0}; b=$${dv[i]:-0}
    a=$${a%%[!0-9]*}; b=$${b%%[!0-9]*}
    (( 10#$${a:-0} < 10#$${b:-0} )) && return 0
    (( 10#$${a:-0} > 10#$${b:-0} )) && return 1
  done
  return 1
}

# fail_body <logfile> -> the last 10 log lines, escaped for a JSON string.
# Escaped by jq, not by the awk one-liner the backup scripts use: busybox awk
# (the image's awk) does not double a backslash in gsub(/\\/, "\\\\"), so a log
# line containing one produced invalid JSON and Gotify dropped the message.
fail_body() {
  tail -10 "$1" 2>/dev/null | jq -Rs . | sed -e 's/^"//' -e 's/"$//'
}

# notify_gotify <ip> <title> <message> <priority>. Posts by IP because the boot
# pods have no DNS. Never fails the caller.
notify_gotify() {
  local ip=$1 title=$2 msg=$3 priority=$${4:-5}
  curl -sf -m 20 -X POST "http://$${ip}/message" \
    -H "Content-Type: application/json" \
    -H "X-Gotify-Key: $${GOTIFY_TOKEN:-}" \
    -d "{\"title\":\"$${title}\",\"message\":\"$${msg}\",\"priority\":$${priority}}" \
    >/dev/null || true
}
