#!/usr/bin/env bash
# drill-sso, init step: load the restored Keycloak and Gitea dumps into the
# pod's own throwaway Postgres (127.0.0.1, trust auth, never reachable from
# outside the pod). Refuses to run unless drill-restore finished today, so the
# boot stage can never test last week's data.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SCRATCH=$${SCRATCH:-/scratch}
RUN_DIR=$${RUN_DIR:-/run/drill}
LOG=$${LOG:-/tmp/load-sso.log}
PG=(-h 127.0.0.1 -U postgres)

exec > >(tee "$LOG") 2>&1

on_exit() {
  local rc=$? ip=""
  [[ $rc -eq 0 ]] && return
  echo "[drill] load FAILED with exit $rc"
  # Even a stale marker carries Gotify's IP; with no marker at all there is
  # nothing to post to (the pod has no DNS) and drill-restore already reported.
  [[ -f "$SCRATCH/READY" ]] && ip=$(sed -n 's/^GOTIFY_IP=//p' "$SCRATCH/READY")
  if [[ -n "$ip" ]]; then
    notify_gotify "$ip" "Restore drill: sso ✗" \
      "Loading the dumps failed, exit $${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)\n\n$(fail_body "$LOG")" 8
  fi
}
trap on_exit EXIT

if ! marker_is_today "$SCRATCH/READY" "$(date -u +%Y-%m-%d)"; then
  echo "[drill] no restore from today on the scratch volume — drill-restore did not finish"
  exit 1
fi

echo "[drill] Waiting for Postgres"
for _ in $(seq 1 60); do
  pg_isready "$${PG[@]}" >/dev/null 2>&1 && break
  sleep 2
done
pg_isready "$${PG[@]}"

for db in keycloak gitea; do
  echo "[drill] Loading $db"
  createdb "$${PG[@]}" "$db"
  pg_restore "$${PG[@]}" --no-owner --no-acl -d "$db" "$SCRATCH/$db/db/$db.pgdump"
done

# Keycloak records its server version; an image older than the data cannot be
# trusted to boot on it. That is "bump the drill image", not "the backup is bad".
data_version=$(psql "$${PG[@]}" -Atc "SELECT version FROM migration_model ORDER BY update_time DESC LIMIT 1" keycloak)
echo "[drill] keycloak data version: $${data_version:-unknown}, image: $${KEYCLOAK_IMAGE_VERSION}"
if version_older "$KEYCLOAK_IMAGE_VERSION" "$data_version"; then
  mkdir -p "$RUN_DIR"
  echo "keycloak image $${KEYCLOAK_IMAGE_VERSION} is older than the restored data ($${data_version})" > "$RUN_DIR/STALE"
fi
echo "[drill] Load complete"
