#!/usr/bin/env bash
# verify-offsite.sh — Monthly integrity check of offsite backups (Filen via rclone crypt)
# Runs on TrueNAS (.2) on the 1st of each month at 06:00
# Cron entry (deployed by Ansible):
#   0 6 1 * * /usr/local/bin/verify-offsite.sh >> /var/log/verify-offsite.log 2>&1

set -euo pipefail
umask 0027

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKUP_BASE="/tank/backups"
DB_DIR="${BACKUP_BASE}/databases"
SERVICES_DIR="${BACKUP_BASE}/services"

RCLONE_REMOTE="filen-crypt"
RCLONE_DEST="${RCLONE_REMOTE}:homelab-backups"

GOTIFY_URL="http://gotify.blackcats.cc/message"
GOTIFY_TOKEN_FILE="/tank/backups/keys/gotify-token"
GOTIFY_TOKEN="${GOTIFY_TOKEN:-}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_PREFIX="[verify-offsite ${TIMESTAMP}]"

# Number of mismatches that will trigger a failure notification
MISMATCH_THRESHOLD=0

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
# Error trap
# ---------------------------------------------------------------------------
trap_error() {
    local exit_code=$?
    log "ERROR: verify-offsite.sh failed with exit code ${exit_code}"
    gotify_notify \
        "Offsite Verify FAILED" \
        "verify-offsite.sh encountered a fatal error at ${TIMESTAMP} (exit ${exit_code}). Check /var/log/verify-offsite.log on TrueNAS." \
        "8"
    exit "${exit_code}"
}

trap trap_error ERR

# ---------------------------------------------------------------------------
# Accumulate results
# ---------------------------------------------------------------------------
TOTAL_CHECKS=0
TOTAL_MISMATCHES=0
MISMATCH_DETAILS=""

run_check() {
    local label="$1"
    local local_path="$2"
    local remote_path="$3"
    local extra_flags="${4:-}"

    log "Checking: ${label}"
    local output
    # rclone check exits non-zero on mismatch; capture both stdout and stderr
    if output=$(rclone check \
        "${local_path}" \
        "${remote_path}" \
        ${extra_flags} \
        --log-level INFO \
        2>&1); then
        log "OK: ${label} — no mismatches"
    else
        local mismatches
        mismatches=$(echo "${output}" | grep -c "ERROR\|not found\|differ" || true)
        log "MISMATCH: ${label} — ${mismatches} issue(s)"
        MISMATCH_DETAILS+="[${label}]: ${mismatches} mismatch(es)\n"
        (( TOTAL_MISMATCHES += mismatches ))
        log "Details:\n${output}"
    fi
    (( TOTAL_CHECKS++ ))
}

# ---------------------------------------------------------------------------
# Step 1: Check database dumps
# ---------------------------------------------------------------------------
log "=== Checking database dumps ==="
run_check \
    "databases" \
    "${DB_DIR}/" \
    "${RCLONE_DEST}/databases/"

# ---------------------------------------------------------------------------
# Step 2: Check services config (TrueNAS config exports, Terraform state)
# ---------------------------------------------------------------------------
log "=== Checking services config ==="
run_check \
    "services" \
    "${SERVICES_DIR}/" \
    "${RCLONE_DEST}/services/" \
    "--exclude backup-heartbeat"

# ---------------------------------------------------------------------------
# Step 3: Sampled media metadata check
# ---------------------------------------------------------------------------
log "=== Checking sampled media metadata ==="

# Sample check: verify a handful of recently-backed-up metadata files exist remotely
SAMPLE_COUNT=0
SAMPLE_FAILURES=0

while IFS= read -r -d '' meta_file; do
    rel_path="${meta_file#/tank/media/}"
    remote_obj="${RCLONE_DEST}/media-meta/${rel_path}"

    log "Sampling: ${rel_path}"
    if rclone ls "${remote_obj}" --log-level ERROR > /dev/null 2>&1; then
        (( SAMPLE_COUNT++ ))
    else
        log "MISSING remote: ${remote_obj}"
        MISMATCH_DETAILS+="[media-meta sample] Missing: ${rel_path}\n"
        (( SAMPLE_FAILURES++ ))
        (( TOTAL_MISMATCHES++ ))
    fi

    # Cap at 20 samples
    if (( SAMPLE_COUNT + SAMPLE_FAILURES >= 20 )); then
        break
    fi
done < <(find /tank/media -name "*.json" -newer "${DB_DIR}" -print0 2>/dev/null | head -z -n 20)

(( TOTAL_CHECKS++ ))
log "Media metadata sample: ${SAMPLE_COUNT} OK, ${SAMPLE_FAILURES} missing"

# ---------------------------------------------------------------------------
# Step 4: Verify monthly archive exists for last month
# ---------------------------------------------------------------------------
log "=== Checking last month's archive ==="
LAST_MONTH="$(date -u -d "last month" +%Y-%m)"
ARCHIVE_REMOTE="${RCLONE_DEST}/archives/${LAST_MONTH}/"

log "Checking archive for ${LAST_MONTH}..."
ARCHIVE_COUNT=$(rclone ls "${ARCHIVE_REMOTE}" --log-level ERROR 2>/dev/null | wc -l || echo "0")
(( TOTAL_CHECKS++ ))

if (( ARCHIVE_COUNT > 0 )); then
    log "OK: Archive for ${LAST_MONTH} exists (${ARCHIVE_COUNT} file(s))"
else
    log "MISSING: Archive for ${LAST_MONTH} not found at ${ARCHIVE_REMOTE}"
    MISMATCH_DETAILS+="[monthly archive] ${LAST_MONTH} archive missing or empty\n"
    (( TOTAL_MISMATCHES++ ))
fi

# ---------------------------------------------------------------------------
# Step 5: Report results
# ---------------------------------------------------------------------------
log "=== Verification summary ==="
log "Total checks: ${TOTAL_CHECKS}"
log "Total mismatches: ${TOTAL_MISMATCHES}"

if (( TOTAL_MISMATCHES > MISMATCH_THRESHOLD )); then
    log "RESULT: FAILED — ${TOTAL_MISMATCHES} mismatch(es) detected"
    gotify_notify \
        "Offsite Verify FAILED" \
        "Monthly offsite integrity check FAILED at ${TIMESTAMP}.\n\nMismatches: ${TOTAL_MISMATCHES}\n\nDetails:\n${MISMATCH_DETAILS}\nCheck /var/log/verify-offsite.log on TrueNAS." \
        "8"
    exit 1
else
    log "RESULT: PASSED — all checks clean"
    gotify_notify \
        "Offsite Verify OK" \
        "Monthly offsite integrity check PASSED at ${TIMESTAMP}.\n\nChecks: ${TOTAL_CHECKS}\nMismatches: 0" \
        "3"
fi

log "=== verify-offsite.sh complete: ${TIMESTAMP} ==="
