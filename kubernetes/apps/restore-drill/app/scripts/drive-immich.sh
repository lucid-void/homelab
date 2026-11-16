#!/usr/bin/env bash
# drill-immich, main container: wait for Immich to boot on the restored
# database, check it, report, and — pass or fail — empty the scratch volume.
# This is the last stage of the weekly drill.
#
# The check that matters: every sampled original file's SHA-1 equals the
# checksum the restored database holds for it. That ties the file backup and
# the database backup to each other. Tests: tests/test_immich.sh
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SCRATCH=${SCRATCH:-/scratch}
RUN_DIR=${RUN_DIR:-/run/drill}
LOG=${LOG:-/tmp/drive-immich.log}
BOOT_TIMEOUT=${BOOT_TIMEOUT:-900}
MIN_IMMICH_ASSETS=${MIN_IMMICH_ASSETS:-1}
MIN_SAMPLE_MATCHED=${MIN_SAMPLE_MATCHED:-150}
# snapshot path /library/<x>  <->  asset."originalPath" /usr/src/app/upload/<x>
SNAPSHOT_PREFIX=/library
DB_PREFIX=/usr/src/app/upload
PG=(-h 127.0.0.1 -U postgres)

exec > >(tee "$LOG") 2>&1

marker() { sed -n "s/^$1=//p" "$SCRATCH/READY"; }
GOTIFY_IP=$(marker GOTIFY_IP)
SNAPTIME=$(marker SNAPTIME_IMMICH)
REPORTED=0

on_exit() {
  local rc=$?
  if [[ $rc -ne 0 && $REPORTED -eq 0 ]]; then
    echo "[drill] FAILED with exit $rc"
    notify_gotify "$GOTIFY_IP" "Restore drill: immich ✗" \
      "Exit ${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)\n\n$(fail_body "$LOG")" 8
  fi
  # Leave nothing behind, whatever happened.
  find "$SCRATCH" -mindepth 1 -delete 2>/dev/null || true
}
trap on_exit EXIT

# --- 0. An image older than the data proves nothing about the backup ----------
if [[ -s "$RUN_DIR/STALE" ]]; then
  reason=$(head -1 "$RUN_DIR/STALE")
  echo "[drill] STALE: $reason"
  notify_gotify "$GOTIFY_IP" "Restore drill: immich stale — bump the image" \
    "${reason}. The drill did not run; raise the image tag in cronjob-immich.yml." 8
  REPORTED=1
  exit 3
fi

# --- 1. The restored database is not empty ------------------------------------
assets=$(psql "${PG[@]}" -Atc "SELECT count(*) FROM asset" immich)
floor "Immich assets" "$assets" "$MIN_IMMICH_ASSETS"
echo "[drill] $assets assets in the restored database"

# --- 2. Immich itself boots on it ---------------------------------------------
echo "[drill] Waiting for Immich"
deadline=$(( $(date +%s) + BOOT_TIMEOUT ))
until [[ "$(curl -fsS -m 10 http://127.0.0.1:2283/api/server/ping 2>/dev/null | jq -r '.res' 2>/dev/null)" == "pong" ]]; do
  if (( $(date +%s) > deadline )); then
    echo "[drill] Immich did not come up within ${BOOT_TIMEOUT}s (if its image is older than production, bump it)"
    exit 1
  fi
  sleep 5
done
echo "[drill] Immich is up"

# --- 3. Sampled files match the checksums in the database ---------------------
sampled=0 matched=0 skipped=0
while IFS= read -r path; do
  [[ -n "$path" ]] || continue
  sampled=$((sampled + 1))
  file="$SCRATCH/immich$path"
  if [[ ! -f "$file" ]]; then
    echo "[drill] sampled file was not restored: $path"
    exit 1
  fi
  want=$(psql "${PG[@]}" -At -v p="${DB_PREFIX}${path#"$SNAPSHOT_PREFIX"}" immich \
    <<<"SELECT encode(checksum, 'hex') FROM asset WHERE \"originalPath\" = :'p' LIMIT 1")
  if [[ -z "$want" ]]; then
    # on disk but not an asset (a sidecar, an orphan): not evidence either way
    skipped=$((skipped + 1))
    continue
  fi
  got=$(sha1sum "$file" | cut -d' ' -f1)
  if [[ "$got" != "$want" ]]; then
    echo "[drill] checksum mismatch: $path (file $got, database $want)"
    exit 1
  fi
  matched=$((matched + 1))
done < "$SCRATCH/immich/sample.txt"

if (( matched < MIN_SAMPLE_MATCHED )); then
  echo "[drill] only $matched of $sampled matched, need $MIN_SAMPLE_MATCHED ($skipped had no database row)"
  exit 1
fi
echo "[drill] $matched of $sampled sampled files matched ($skipped had no database row)"

notify_gotify "$GOTIFY_IP" "Restore drill: immich ✓" \
  "Immich snapshot ${SNAPTIME}\n${assets} assets, server boots\n${matched} of ${sampled} sampled files matched their database checksum" 5
REPORTED=1
echo "[drill] PASS"
