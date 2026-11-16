#!/usr/bin/env bash
# drill-immich, init step: load the restored Immich dump into the pod's own
# throwaway Postgres (127.0.0.1, trust auth). Refuses to run unless
# drill-restore finished today. drill-immich is the last stage of the drill, so
# a failure here also empties the scratch volume — the driver, which normally
# does that, never starts when an init container fails.
set -euo pipefail

# shellcheck source=lib.sh
source "$(dirname "$0")/lib.sh"

SCRATCH=${SCRATCH:-/scratch}
RUN_DIR=${RUN_DIR:-/run/drill}
LOG=${LOG:-/tmp/load-immich.log}
PG=(-h 127.0.0.1 -U postgres)

exec > >(tee "$LOG") 2>&1

on_exit() {
  local rc=$? ip=""
  [[ $rc -eq 0 ]] && return
  echo "[drill] load FAILED with exit $rc"
  [[ -f "$SCRATCH/READY" ]] && ip=$(sed -n 's/^GOTIFY_IP=//p' "$SCRATCH/READY")
  if [[ -n "$ip" ]]; then
    notify_gotify "$ip" "Restore drill: immich ✗" \
      "Loading the dump failed, exit ${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)\n\n$(fail_body "$LOG")" 8
  fi
  find "$SCRATCH" -mindepth 1 -delete 2>/dev/null || true
}
trap on_exit EXIT

if ! marker_is_today "$SCRATCH/READY" "$(date -u +%Y-%m-%d)"; then
  echo "[drill] no restore from today on the scratch volume — drill-restore did not finish"
  exit 1
fi

echo "[drill] Waiting for Postgres"
for _ in $(seq 1 60); do
  pg_isready "${PG[@]}" >/dev/null 2>&1 && break
  sleep 2
done
pg_isready "${PG[@]}"

echo "[drill] Loading immich"
createdb "${PG[@]}" immich
# The dump creates its own extensions (vector, vchord, ...); the drill's
# Postgres is the production image, so they are all available.
pg_restore "${PG[@]}" --no-owner --no-acl -d immich "$SCRATCH/immich/db/immich.pgdump"

data_version=$(psql "${PG[@]}" -Atc 'SELECT version FROM version_history ORDER BY "createdAt" DESC LIMIT 1' immich)
echo "[drill] immich data version: ${data_version:-unknown}, image: ${IMMICH_IMAGE_VERSION}"
if version_older "$IMMICH_IMAGE_VERSION" "$data_version"; then
  mkdir -p "$RUN_DIR"
  echo "immich image ${IMMICH_IMAGE_VERSION} is older than the restored data (${data_version})" > "$RUN_DIR/STALE"
fi
echo "[drill] Load complete"
