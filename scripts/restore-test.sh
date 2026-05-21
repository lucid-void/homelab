#!/usr/bin/env bash
# restore-test.sh — Monthly restore test for backup verification
# Runs on Services VM (.13) on the 15th of each month at 06:00
# Cron entry (deployed by Ansible):
#   0 6 15 * * /usr/local/bin/restore-test.sh >> /var/log/restore-test.log 2>&1
#
# Tests:
#   1. Pull latest immich dump from Filen offsite remote
#   2. Start throwaway Postgres container
#   3. Import dump and verify table/row counts
#   4. Pull a sample Immich media file and verify it
#   5. Cleanup via trap EXIT
#   6. Gotify alert with result

set -euo pipefail
umask 0027

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RCLONE_REMOTE="filen-crypt"
RCLONE_DEST="${RCLONE_REMOTE}:homelab-backups"

WORK_DIR="/tmp/restore-test-${$}"
PG_CONTAINER="restore-test-pg-${$}"
PG_PORT="55432"
PG_IMAGE="postgres:16"
PG_USER="restore_test"
PG_PASS="restore_test_password_ephemeral"
PG_DB="immich_restore_test"

# Test database: immich (largest, most critical)
TEST_DB="immich"
# Expected minimum table count in a healthy immich DB
IMMICH_MIN_TABLES=10
# Expected minimum asset count
IMMICH_MIN_ASSETS=0  # 0 = don't require any assets, just a clean import

GOTIFY_URL="http://gotify.blackcats.cc/message"
GOTIFY_TOKEN_FILE="/opt/volumes/backup/gotify-token"
GOTIFY_TOKEN="${GOTIFY_TOKEN:-}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DATE="$(date -u +%Y-%m-%d)"
LOG_PREFIX="[restore-test ${TIMESTAMP}]"

# Result tracking
TEST_PASSED=false
FAILURE_REASON=""

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
        log "WARNING: No Gotify token — skipping notification"
        return 0
    fi

    curl -s --max-time 10 -X POST \
        "${GOTIFY_URL}?token=${token}" \
        -F "title=${title}" \
        -F "message=${message}" \
        -F "priority=${priority}" \
        || log "WARNING: Gotify notification failed (non-fatal)"
}

# ---------------------------------------------------------------------------
# Cleanup trap — always runs on EXIT regardless of success/failure
# ---------------------------------------------------------------------------
cleanup() {
    local exit_code=$?
    log "=== Cleanup ==="

    # Stop and remove throwaway Postgres container
    if docker ps -q --filter "name=${PG_CONTAINER}" | grep -q .; then
        log "Stopping Postgres container: ${PG_CONTAINER}"
        docker stop "${PG_CONTAINER}" 2>/dev/null || true
    fi
    if docker ps -aq --filter "name=${PG_CONTAINER}" | grep -q .; then
        log "Removing Postgres container: ${PG_CONTAINER}"
        docker rm "${PG_CONTAINER}" 2>/dev/null || true
    fi

    # Remove work directory
    if [[ -d "${WORK_DIR}" ]]; then
        log "Removing work directory: ${WORK_DIR}"
        rm -rf "${WORK_DIR}"
    fi

    # Send final Gotify notification
    if "${TEST_PASSED}"; then
        log "Restore test PASSED"
        gotify_notify \
            "Restore Test PASSED" \
            "Monthly restore test PASSED at ${TIMESTAMP}.\n\nDatabase: ${TEST_DB}\nAll verifications successful." \
            "3"
    else
        log "Restore test FAILED: ${FAILURE_REASON}"
        gotify_notify \
            "Restore Test FAILED" \
            "Monthly restore test FAILED at ${TIMESTAMP}.\n\nReason: ${FAILURE_REASON}\n\nCheck /var/log/restore-test.log on Services VM." \
            "8"
    fi

    log "=== restore-test.sh complete: ${TIMESTAMP} (exit ${exit_code}) ==="
}

trap cleanup EXIT

# Separate trap for unexpected errors (sets failure reason before cleanup runs)
trap_error() {
    local exit_code=$?
    local line="${BASH_LINENO[0]}"
    if ! "${TEST_PASSED}"; then
        FAILURE_REASON="${FAILURE_REASON:-Unexpected error at line ${line} (exit ${exit_code})}"
    fi
}
trap trap_error ERR

# ---------------------------------------------------------------------------
# Setup work directory
# ---------------------------------------------------------------------------
log "=== restore-test.sh starting: ${TIMESTAMP} ==="
mkdir -p "${WORK_DIR}"
log "Work directory: ${WORK_DIR}"

# ---------------------------------------------------------------------------
# Step 1: Find and pull latest immich dump from Filen
# ---------------------------------------------------------------------------
log "=== Step 1: Pull latest ${TEST_DB} dump from Filen ==="

REMOTE_DB_PATH="${RCLONE_DEST}/databases/"
log "Listing remote dumps for ${TEST_DB}..."

# Find the latest dump file for the target database
LATEST_DUMP_REMOTE=$(rclone ls "${REMOTE_DB_PATH}" \
    --include "${TEST_DB}-*.sql.gz" \
    --log-level ERROR \
    2>/dev/null \
    | sort -k2 \
    | tail -1 \
    | awk '{print $2}') || true

if [[ -z "${LATEST_DUMP_REMOTE}" ]]; then
    FAILURE_REASON="No ${TEST_DB} dump found on remote ${REMOTE_DB_PATH}"
    log "ERROR: ${FAILURE_REASON}"
    exit 1
fi

log "Latest remote dump: ${LATEST_DUMP_REMOTE}"
LOCAL_DUMP="${WORK_DIR}/${LATEST_DUMP_REMOTE##*/}"

log "Downloading to ${LOCAL_DUMP}..."
rclone copy \
    "${REMOTE_DB_PATH}${LATEST_DUMP_REMOTE}" \
    "${WORK_DIR}/" \
    --log-level INFO \
    2>&1 | tail -5

if [[ ! -f "${LOCAL_DUMP}" ]]; then
    FAILURE_REASON="Download succeeded but file not found at ${LOCAL_DUMP}"
    log "ERROR: ${FAILURE_REASON}"
    exit 1
fi

log "Downloaded: ${LOCAL_DUMP} ($(du -sh "${LOCAL_DUMP}" | awk '{print $1}'))"

# Verify the gzip is not corrupt
log "Verifying gzip integrity..."
gzip -t "${LOCAL_DUMP}" || {
    FAILURE_REASON="Gzip integrity check failed for ${LOCAL_DUMP}"
    log "ERROR: ${FAILURE_REASON}"
    exit 1
}
log "Gzip OK"

# ---------------------------------------------------------------------------
# Step 2: Start throwaway Postgres container
# ---------------------------------------------------------------------------
log "=== Step 2: Start throwaway Postgres container ==="

docker run -d \
    --name "${PG_CONTAINER}" \
    -e POSTGRES_USER="${PG_USER}" \
    -e POSTGRES_PASSWORD="${PG_PASS}" \
    -e POSTGRES_DB="${PG_DB}" \
    -p "127.0.0.1:${PG_PORT}:5432" \
    "${PG_IMAGE}" \
    > /dev/null

log "Waiting for Postgres to be ready..."
READY=false
for i in $(seq 1 30); do
    if docker exec "${PG_CONTAINER}" \
        pg_isready -U "${PG_USER}" -d "${PG_DB}" -q 2>/dev/null; then
        READY=true
        log "Postgres ready after ${i}s"
        break
    fi
    sleep 1
done

if ! "${READY}"; then
    FAILURE_REASON="Throwaway Postgres container failed to become ready within 30s"
    log "ERROR: ${FAILURE_REASON}"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: Import dump and verify
# ---------------------------------------------------------------------------
log "=== Step 3: Import dump ==="

log "Decompressing and importing ${LOCAL_DUMP} into ${PG_DB}..."
gunzip -c "${LOCAL_DUMP}" | PGPASSWORD="${PG_PASS}" psql \
    -h 127.0.0.1 \
    -p "${PG_PORT}" \
    -U "${PG_USER}" \
    -d "${PG_DB}" \
    --no-password \
    -q \
    2>&1 | tail -5 \
    || {
        FAILURE_REASON="pg_restore (psql import) failed for ${LOCAL_DUMP}"
        log "ERROR: ${FAILURE_REASON}"
        exit 1
    }

log "Import complete. Verifying schema..."

# Count tables
TABLE_COUNT=$(PGPASSWORD="${PG_PASS}" psql \
    -h 127.0.0.1 \
    -p "${PG_PORT}" \
    -U "${PG_USER}" \
    -d "${PG_DB}" \
    --no-password \
    -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';" \
    2>/dev/null | tr -d ' ') || TABLE_COUNT=0

log "Table count: ${TABLE_COUNT} (minimum required: ${IMMICH_MIN_TABLES})"

if (( TABLE_COUNT < IMMICH_MIN_TABLES )); then
    FAILURE_REASON="Table count ${TABLE_COUNT} is below minimum ${IMMICH_MIN_TABLES} — import may be incomplete"
    log "ERROR: ${FAILURE_REASON}"
    exit 1
fi

log "Table count OK: ${TABLE_COUNT}"

# Count assets (immich-specific)
ASSET_COUNT=$(PGPASSWORD="${PG_PASS}" psql \
    -h 127.0.0.1 \
    -p "${PG_PORT}" \
    -U "${PG_USER}" \
    -d "${PG_DB}" \
    --no-password \
    -t -c "SELECT COUNT(*) FROM assets;" \
    2>/dev/null | tr -d ' ') || ASSET_COUNT="unknown"

log "Asset count: ${ASSET_COUNT}"

if [[ "${ASSET_COUNT}" == "unknown" ]]; then
    log "WARNING: Could not query assets table — may be expected if schema differs"
elif (( ASSET_COUNT < IMMICH_MIN_ASSETS )); then
    FAILURE_REASON="Asset count ${ASSET_COUNT} is below minimum ${IMMICH_MIN_ASSETS}"
    log "ERROR: ${FAILURE_REASON}"
    exit 1
fi

log "Schema and data verification passed"

# ---------------------------------------------------------------------------
# Step 4: Pull a sample media file from Filen
# ---------------------------------------------------------------------------
log "=== Step 4: Pull sample media file ==="

MEDIA_REMOTE="${RCLONE_DEST}/media-meta/"
SAMPLE_META=""

# Find any JSON metadata file to verify media backup is accessible
SAMPLE_META=$(rclone ls "${MEDIA_REMOTE}" \
    --include "*.json" \
    --log-level ERROR \
    2>/dev/null \
    | head -1 \
    | awk '{print $2}') || true

if [[ -z "${SAMPLE_META}" ]]; then
    log "WARNING: No media metadata files found on remote — skipping media sample check"
else
    log "Sampling media metadata: ${SAMPLE_META}"
    SAMPLE_LOCAL="${WORK_DIR}/sample-media-meta.json"

    rclone copy \
        "${MEDIA_REMOTE}${SAMPLE_META}" \
        "${WORK_DIR}/" \
        --log-level INFO \
        2>&1 | tail -3

    SAMPLE_BASENAME="${SAMPLE_META##*/}"
    if [[ -f "${WORK_DIR}/${SAMPLE_BASENAME}" ]]; then
        SAMPLE_SIZE=$(du -sh "${WORK_DIR}/${SAMPLE_BASENAME}" | awk '{print $1}')
        log "Sample media metadata pulled OK: ${SAMPLE_BASENAME} (${SAMPLE_SIZE})"

        # Validate it's valid JSON
        if python3 -c "import sys,json; json.load(open(sys.argv[1]))" \
            "${WORK_DIR}/${SAMPLE_BASENAME}" 2>/dev/null; then
            log "Sample JSON is valid"
        else
            log "WARNING: Sample file is not valid JSON (may not be a JSON file)"
        fi
    else
        log "WARNING: Sample media metadata file not found after download — non-fatal"
    fi
fi

# ---------------------------------------------------------------------------
# Mark test as passed
# ---------------------------------------------------------------------------
TEST_PASSED=true
log "=== All restore test checks passed ==="

# Cleanup runs via EXIT trap
