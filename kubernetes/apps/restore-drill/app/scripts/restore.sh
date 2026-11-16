#!/usr/bin/env bash
# drill-restore: pull the latest Keycloak dump, Gitea snapshot and an Immich
# sample from Filen onto the scratch PVC, then write the READY marker the boot
# jobs wait for. This is the only drill pod with internet access, and it runs no
# restored app.
#
# READ-ONLY BY BEHAVIOUR. Filen has no read-only login, so the guarantee lives
# here: every restic call goes through r(), which adds --no-lock, and this file
# must never call a restic subcommand that writes to a repo. A repo that cannot
# be opened is a failure, never something to create. tests/test_restore.sh pins
# all of this. See design/decisions/backups.md.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SCRATCH=$${SCRATCH:-/scratch}
SECRETS=$${SECRETS:-/secrets}
LOG=$${LOG:-/tmp/drill-restore.log}
SAMPLE_COUNT=$${SAMPLE_COUNT:-200}
SAMPLE_MAX_BYTES=$${SAMPLE_MAX_BYTES:-5368709120}
GOTIFY_HOST=gotify.monitoring.svc.cluster.local
GOTIFY_IP=""

exec > >(tee "$LOG") 2>&1

on_exit() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    rm -f "$SCRATCH/READY"
    echo "[drill] FAILED with exit $rc"
    if [[ -n "$GOTIFY_IP" ]]; then
      notify_gotify "$GOTIFY_IP" "Restore drill: restore ✗" \
        "Exit $${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)\n\n$(fail_body "$LOG")" 8
    fi
  fi
}
trap on_exit EXIT

# r <secrets-dir-name> <restic args...>
r() {
  local repo=$1; shift
  RESTIC_REPOSITORY_FILE="$SECRETS/$repo/RESTIC_REPOSITORY" \
    RESTIC_PASSWORD_FILE="$SECRETS/$repo/RESTIC_PASSWORD" \
    restic --no-lock "$@"
}

# latest <secrets-dir-name> -> "<id> <time>" of the newest snapshot. Not
# `--latest 1`: restic groups by host, and every backup pod has its own hostname.
latest() {
  r "$1" snapshots --json | jq -r 'max_by(.time) | "\(.short_id // .id) \(.time)"'
}

echo "[drill] Starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# First, so that every later failure can be reported. The boot pods have no DNS
# and read this IP from the marker.
GOTIFY_IP=$(getent hosts "$GOTIFY_HOST" | awk '{print $1; exit}')
[[ -n "$GOTIFY_IP" ]] || { echo "[drill] cannot resolve $GOTIFY_HOST"; exit 1; }

echo "[drill] Wiping $SCRATCH"
find "$SCRATCH" -mindepth 1 -delete

now=$(date +%s)
declare -A SNAP SNAPTIME
# drill name -> secrets dir (Keycloak's dump lives in the postgres-backup repo)
declare -A REPO=([keycloak]=postgres [gitea]=gitea [immich]=immich)
for app in keycloak gitea immich; do
  read -r id taken < <(latest "$${REPO[$app]}")
  [[ -n "$${id:-}" && "$id" != "null" ]] || { echo "[drill] $app: no snapshot found"; exit 1; }
  if ! snapshot_fresh "$taken" "$now"; then
    echo "[drill] $app: latest snapshot $id is from $taken, older than 48 hours"
    exit 1
  fi
  SNAP[$app]=$id; SNAPTIME[$app]=$taken
  echo "[drill] $app: snapshot $id from $taken"
done

echo "[drill] Restoring the Keycloak database dump"
r postgres restore "$${SNAP[keycloak]}" --target "$SCRATCH/keycloak" --include /db/keycloak.pgdump

echo "[drill] Restoring the Gitea snapshot"
r gitea restore "$${SNAP[gitea]}" --target "$SCRATCH/gitea"

echo "[drill] Choosing the Immich sample"
mkdir -p "$SCRATCH/immich"
r immich ls "$${SNAP[immich]}" --json \
  | pick_sample "$SAMPLE_COUNT" "$SAMPLE_MAX_BYTES" > "$SCRATCH/immich/sample.txt"
[[ -s "$SCRATCH/immich/sample.txt" ]] || { echo "[drill] immich: no original files to sample"; exit 1; }
echo "[drill] immich: sampled $(wc -l < "$SCRATCH/immich/sample.txt") files"
{
  echo "/db/immich.pgdump"
  echo "**/.immich"
  glob_escape < "$SCRATCH/immich/sample.txt"
} > "$SCRATCH/immich/include.txt"

echo "[drill] Restoring the Immich database dump and sample"
r immich restore "$${SNAP[immich]}" --target "$SCRATCH/immich" --include-file "$SCRATCH/immich/include.txt"

# A rotating slice per week reads each whole repo once a year.
subset=$(check_subset "$(date +%V)")
for repo in postgres gitea immich; do
  echo "[drill] Checking repo $repo, data subset $subset"
  r "$repo" check --read-data-subset="$subset"
done

cat > "$SCRATCH/READY.tmp" <<EOF
DRILL_DATE=$(date -u +%Y-%m-%d)
GOTIFY_IP=$GOTIFY_IP
SNAP_KEYCLOAK=$${SNAP[keycloak]}
SNAP_GITEA=$${SNAP[gitea]}
SNAP_IMMICH=$${SNAP[immich]}
SNAPTIME_KEYCLOAK=$${SNAPTIME[keycloak]}
SNAPTIME_GITEA=$${SNAPTIME[gitea]}
SNAPTIME_IMMICH=$${SNAPTIME[immich]}
EOF
mv "$SCRATCH/READY.tmp" "$SCRATCH/READY"
echo "[drill] READY written at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
