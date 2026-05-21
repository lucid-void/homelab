#!/usr/bin/env bash
# backup.sh — Daily coordinated backup for homelab databases and config
# Runs as cron job on TrueNAS (.2) at 03:00 daily, deployed by Ansible
# Wrapped in a 4-hour timeout by the cron entry:
#   0 3 * * * timeout 4h /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1

set -euo pipefail
umask 0027

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TRUENAS_API_URL="http://localhost/api/v2.0"
TRUENAS_API_KEY_FILE="/tank/backups/keys/truenas-api-key"
SERVICES_VM="172.16.20.13"
SERVICES_USER="backup"

BACKUP_BASE="/tank/backups"
DB_DIR="${BACKUP_BASE}/databases"
SERVICES_DIR="${BACKUP_BASE}/services"
TRUENAS_CONFIG_DIR="${SERVICES_DIR}/truenas"

PG_HOST="172.16.20.2"
PG_PORT="5432"
PG_USER="backup"
PGPASSFILE="/tank/backups/keys/.pgpass"

RCLONE_REMOTE="filen-crypt"
RCLONE_DEST="${RCLONE_REMOTE}:homelab-backups"

GOTIFY_URL="http://gotify.blackcats.cc/message"
GOTIFY_TOKEN_FILE="/tank/backups/keys/gotify-token"
GOTIFY_TOKEN="${GOTIFY_TOKEN:-}"

HEARTBEAT_FILE="${SERVICES_DIR}/backup-heartbeat"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DATE="$(date -u +%Y-%m-%d)"
LOG_PREFIX="[backup ${TIMESTAMP}]"

# Databases to dump (all on TrueNAS Postgres)
DATABASES=(immich paperless gitea authentik freshrss)

# Swarm services to stop/start around backup (scale to 0/1)
# Format: "stack_name service_name"
SWARM_SERVICES=(
    "immich immich-server"
    "immich immich-microservices"
    "immich immich-machine-learning"
    "paperless paperless-webserver"
    "paperless paperless-worker"
    "gitea gitea-server"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
    echo "${LOG_PREFIX} $*"
}

gotify_notify() {
    local title="$1"
    local message="$2"
    local priority="${3:-5}"
    local token

    if [[ -n "${GOTIFY_TOKEN}" ]]; then
        token="${GOTIFY_TOKEN}"
    elif [[ -f "${GOTIFY_TOKEN_FILE}" ]]; then
        token="$(cat "${GOTIFY_TOKEN_FILE}")"
    else
        log "WARNING: No Gotify token available — skipping notification"
        return 0
    fi

    curl -s --max-time 10 -X POST \
        "${GOTIFY_URL}?token=${token}" \
        -F "title=${title}" \
        -F "message=${message}" \
        -F "priority=${priority}" \
        || log "WARNING: Gotify notification failed (non-fatal)"
}

# Read TrueNAS API key
truenas_api_key() {
    if [[ -f "${TRUENAS_API_KEY_FILE}" ]]; then
        cat "${TRUENAS_API_KEY_FILE}"
    else
        log "ERROR: TrueNAS API key file not found: ${TRUENAS_API_KEY_FILE}"
        exit 1
    fi
}

# Scale a Swarm service on the services VM
swarm_scale() {
    local stack="$1"
    local service="$2"
    local replicas="$3"
    log "Scaling ${stack}_${service} to ${replicas} replica(s)..."
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "${SERVICES_USER}@${SERVICES_VM}" \
        "docker service scale ${stack}_${service}=${replicas}" \
        || { log "ERROR: Failed to scale ${stack}_${service} to ${replicas}"; return 1; }
}

# Wait for a Swarm service to reach desired replica count
swarm_wait() {
    local stack="$1"
    local service="$2"
    local desired="$3"
    local timeout=120
    local elapsed=0
    local interval=5

    log "Waiting for ${stack}_${service} to reach ${desired} replica(s)..."
    while true; do
        local running
        running=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
            "${SERVICES_USER}@${SERVICES_VM}" \
            "docker service ls --filter name=${stack}_${service} --format '{{.Replicas}}'" 2>/dev/null \
            | awk -F'/' '{print $1}') || true

        if [[ "${running}" == "${desired}" ]]; then
            log "${stack}_${service} reached ${desired} replica(s)"
            return 0
        fi

        if (( elapsed >= timeout )); then
            log "WARNING: Timed out waiting for ${stack}_${service} (${elapsed}s)"
            return 1
        fi

        sleep "${interval}"
        (( elapsed += interval ))
    done
}

# ---------------------------------------------------------------------------
# Cleanup and error trap
# ---------------------------------------------------------------------------
SERVICES_STOPPED=false

cleanup_on_error() {
    local exit_code=$?
    log "ERROR: Backup failed with exit code ${exit_code}. Attempting to restart services..."

    if "${SERVICES_STOPPED}"; then
        restart_services || log "WARNING: Service restart during cleanup also failed"
    fi

    gotify_notify \
        "Backup FAILED" \
        "Daily backup failed at ${TIMESTAMP} with exit code ${exit_code}. Services may need manual restart. Check /var/log/backup.log on TrueNAS." \
        "8"

    exit "${exit_code}"
}

trap cleanup_on_error ERR

# ---------------------------------------------------------------------------
# Step 1: Export TrueNAS config via REST API
# ---------------------------------------------------------------------------
log "=== Step 1: Export TrueNAS config ==="
mkdir -p "${TRUENAS_CONFIG_DIR}"
API_KEY="$(truenas_api_key)"

log "Requesting TrueNAS config export..."
curl -s --max-time 60 \
    -H "Authorization: Bearer ${API_KEY}" \
    -o "${TRUENAS_CONFIG_DIR}/truenas-config-${DATE}.db" \
    "${TRUENAS_API_URL}/config/save" \
    || { log "ERROR: TrueNAS config export failed"; exit 1; }

log "TrueNAS config exported to ${TRUENAS_CONFIG_DIR}/truenas-config-${DATE}.db"

# Prune old TrueNAS configs (keep 30 days)
find "${TRUENAS_CONFIG_DIR}" -name "truenas-config-*.db" -mtime +30 -delete

# ---------------------------------------------------------------------------
# Step 2: Stop Swarm services on Services VM (.13)
# ---------------------------------------------------------------------------
log "=== Step 2: Stop Swarm services ==="
for svc in "${SWARM_SERVICES[@]}"; do
    stack=$(echo "${svc}" | awk '{print $1}')
    service=$(echo "${svc}" | awk '{print $2}')
    swarm_scale "${stack}" "${service}" 0
done

SERVICES_STOPPED=true

# Brief pause to let containers stop cleanly
sleep 10

# ---------------------------------------------------------------------------
# Step 3: pg_dump all databases
# ---------------------------------------------------------------------------
log "=== Step 3: Dump databases ==="
mkdir -p "${DB_DIR}"

export PGPASSFILE

for db in "${DATABASES[@]}"; do
    DUMP_FILE="${DB_DIR}/${db}-${DATE}.sql.gz"
    log "Dumping database: ${db} -> ${DUMP_FILE}"
    pg_dump \
        -h "${PG_HOST}" \
        -p "${PG_PORT}" \
        -U "${PG_USER}" \
        -d "${db}" \
        --no-password \
        | gzip -9 > "${DUMP_FILE}" \
        || { log "ERROR: pg_dump failed for ${db}"; exit 1; }
    log "Dump complete: ${db} ($(du -sh "${DUMP_FILE}" | awk '{print $1}'))"
done

# ---------------------------------------------------------------------------
# Step 4: Pull Terraform state from MinIO (via rclone, as snapshot reference)
# ---------------------------------------------------------------------------
log "=== Step 4: Pull Terraform state backup ==="
TF_STATE_DIR="${SERVICES_DIR}/terraform-state"
mkdir -p "${TF_STATE_DIR}"

log "Syncing Terraform state from MinIO..."
rclone copy \
    "truenas-minio:terraform-state/" \
    "${TF_STATE_DIR}/" \
    --progress \
    --log-level INFO \
    2>&1 | tail -5 \
    || log "WARNING: Terraform state sync failed (non-fatal — state is recoverable)"

# ---------------------------------------------------------------------------
# Step 5: Wait for ZFS snapshots (scheduled at 03:05 by TrueNAS periodic task)
# ---------------------------------------------------------------------------
log "=== Step 5: Wait for ZFS snapshots ==="
NOW_SECONDS=$(date -u +%s)
SNAPSHOT_TIME_TODAY=$(date -u -d "$(date -u +%Y-%m-%d) 03:05:00 UTC" +%s)

if (( NOW_SECONDS < SNAPSHOT_TIME_TODAY )); then
    WAIT_SECS=$(( SNAPSHOT_TIME_TODAY - NOW_SECONDS + 30 ))
    log "Waiting ${WAIT_SECS}s for ZFS snapshot at 03:05..."
    sleep "${WAIT_SECS}"
else
    log "ZFS snapshot window already passed (03:05 UTC). Verifying latest snapshot exists..."
fi

# Verify the snapshot was created
EXPECTED_SNAP_PREFIX="$(date -u +%Y-%m-%d)"
if zfs list -t snapshot -o name | grep -q "tank/media@auto-${EXPECTED_SNAP_PREFIX}"; then
    log "ZFS snapshot verified: tank/media@auto-${EXPECTED_SNAP_PREFIX}"
else
    log "WARNING: Could not verify ZFS snapshot for today — snapshot may not exist yet or naming differs"
fi

# ---------------------------------------------------------------------------
# Step 6: Restart Swarm services
# ---------------------------------------------------------------------------
log "=== Step 6: Restart Swarm services ==="
restart_services() {
    for svc in "${SWARM_SERVICES[@]}"; do
        stack=$(echo "${svc}" | awk '{print $1}')
        service=$(echo "${svc}" | awk '{print $2}')
        swarm_scale "${stack}" "${service}" 1
    done
    SERVICES_STOPPED=false

    # Wait for critical services to come back
    for svc in "${SWARM_SERVICES[@]}"; do
        stack=$(echo "${svc}" | awk '{print $1}')
        service=$(echo "${svc}" | awk '{print $2}')
        swarm_wait "${stack}" "${service}" 1 || true
    done
}

restart_services

# ---------------------------------------------------------------------------
# Step 7: rclone sync to Filen encrypted remote
# ---------------------------------------------------------------------------
log "=== Step 7: Sync to Filen offsite remote ==="

log "Syncing databases to ${RCLONE_DEST}/databases/..."
rclone sync \
    "${DB_DIR}/" \
    "${RCLONE_DEST}/databases/" \
    --progress \
    --log-level INFO \
    2>&1 | tail -10 \
    || { log "ERROR: rclone sync of databases failed"; exit 1; }

log "Syncing services config to ${RCLONE_DEST}/services/..."
rclone sync \
    "${SERVICES_DIR}/" \
    "${RCLONE_DEST}/services/" \
    --progress \
    --log-level INFO \
    --exclude "backup-heartbeat" \
    2>&1 | tail -10 \
    || { log "ERROR: rclone sync of services config failed"; exit 1; }

# Sync media metadata (not raw media — too large for offsite; metadata only)
log "Syncing media metadata to ${RCLONE_DEST}/media-meta/..."
rclone sync \
    "/tank/media/" \
    "${RCLONE_DEST}/media-meta/" \
    --progress \
    --log-level INFO \
    --include "*.json" \
    --include "*.yml" \
    --include "*.yaml" \
    --include "*.conf" \
    2>&1 | tail -10 \
    || log "WARNING: Media metadata sync had errors (non-fatal)"

# ---------------------------------------------------------------------------
# Step 8: Monthly archive (1st of month)
# ---------------------------------------------------------------------------
log "=== Step 8: Monthly archive check ==="
DAY_OF_MONTH="$(date -u +%d)"
if [[ "${DAY_OF_MONTH}" == "01" ]]; then
    ARCHIVE_DIR="${BACKUP_BASE}/archives/$(date -u +%Y-%m)"
    mkdir -p "${ARCHIVE_DIR}"
    log "1st of month — creating monthly archive in ${ARCHIVE_DIR}..."

    for db in "${DATABASES[@]}"; do
        SRC="${DB_DIR}/${db}-${DATE}.sql.gz"
        DST="${ARCHIVE_DIR}/${db}-monthly-${DATE}.sql.gz"
        if [[ -f "${SRC}" ]]; then
            cp "${SRC}" "${DST}"
            log "Archived: ${db} -> ${DST}"
        fi
    done

    cp "${TRUENAS_CONFIG_DIR}/truenas-config-${DATE}.db" \
        "${ARCHIVE_DIR}/truenas-config-monthly-${DATE}.db" \
        || log "WARNING: Failed to archive TrueNAS config"

    log "Syncing monthly archive to offsite..."
    rclone sync \
        "${BACKUP_BASE}/archives/" \
        "${RCLONE_DEST}/archives/" \
        --progress \
        --log-level INFO \
        2>&1 | tail -5 \
        || log "WARNING: Monthly archive sync had errors"
else
    log "Not 1st of month (day=${DAY_OF_MONTH}) — skipping monthly archive"
fi

# ---------------------------------------------------------------------------
# Step 9: Prune old local dumps (30-day retention)
# ---------------------------------------------------------------------------
log "=== Step 9: Prune old local dumps (>30 days) ==="
PRUNED=0
while IFS= read -r -d '' f; do
    log "Removing old dump: ${f}"
    rm -f "${f}"
    (( PRUNED++ ))
done < <(find "${DB_DIR}" -name "*.sql.gz" -mtime +30 -print0)

log "Pruned ${PRUNED} old dump file(s)"

# ---------------------------------------------------------------------------
# Step 10: Emit heartbeat file
# ---------------------------------------------------------------------------
log "=== Step 10: Emit heartbeat ==="
mkdir -p "$(dirname "${HEARTBEAT_FILE}")"
date -u +%s > "${HEARTBEAT_FILE}"
log "Heartbeat written: $(cat "${HEARTBEAT_FILE}") ($(date -u))"

# ---------------------------------------------------------------------------
# Step 11: Notify Gotify on success
# ---------------------------------------------------------------------------
log "=== Step 11: Notify success ==="

DB_SIZES=""
for db in "${DATABASES[@]}"; do
    F="${DB_DIR}/${db}-${DATE}.sql.gz"
    if [[ -f "${F}" ]]; then
        SIZE=$(du -sh "${F}" | awk '{print $1}')
        DB_SIZES+="${db}: ${SIZE}\n"
    fi
done

gotify_notify \
    "Backup SUCCESS" \
    "Daily backup completed at ${TIMESTAMP}.\n\nDumps:\n${DB_SIZES}\nOffsite sync: OK\nHeartbeat: updated" \
    "3"

log "=== Backup complete: ${TIMESTAMP} ==="
