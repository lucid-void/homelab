#!/usr/bin/env bash
# Tests for scripts/restore.sh with restic, getent and curl stubbed on PATH.
# Run: bash kubernetes/apps/restore-drill/tests/test_restore.sh
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SCRIPT="$HERE/../app/scripts/restore.sh"
# shellcheck source=harness.sh
source "$HERE/harness.sh"

# Builds stubs in $T/bin and a scratch dir in $T/scratch. Stub behaviour is
# steered by STUB_* variables exported by the individual test.
setup() {
  mkdir -p "$T/bin" "$T/scratch" "$T/secrets"/{gitea,postgres,immich}
  for app in gitea postgres immich; do
    echo "rclone:filen:backups/restic/$app" > "$T/secrets/$app/RESTIC_REPOSITORY"
    echo "pw" > "$T/secrets/$app/RESTIC_PASSWORD"
  done

  cat > "$T/bin/restic" <<'EOF'
#!/usr/bin/env bash
repo=$(basename "$(dirname "$RESTIC_REPOSITORY_FILE")")
marker=absent; [[ -e "$STUB_SCRATCH/READY" ]] && marker=READY_PRESENT
echo "$repo $marker $*" >> "$STUB_LOG"
sub=""
for a in "$@"; do case "$a" in --no-lock) ;; -*) ;; *) sub=$a; break ;; esac; done
case "$sub" in
  snapshots)
    echo "[{\"id\":\"old-$repo\",\"time\":\"2020-01-01T00:00:00Z\"},{\"id\":\"snap-$repo\",\"time\":\"${STUB_SNAP_TIME}\"}]" ;;
  ls)
    for i in 1 2 3; do
      echo "{\"type\":\"file\",\"path\":\"/library/upload/u/aa/f$i.jpg\",\"size\":10,\"struct_type\":\"node\"}"
    done ;;
  restore)
    n=$(grep -c ' restore ' "$STUB_LOG")
    [[ "${STUB_FAIL_RESTORE_N:-0}" == "$n" ]] && { echo "Fatal: stub restore failure"; exit 1; }
    ;;
esac
exit 0
EOF
  printf '#!/usr/bin/env bash\necho "10.96.0.9   gotify.monitoring.svc.cluster.local"\n' > "$T/bin/getent"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$@" >> "%s/curl.args"\n' "$T" > "$T/bin/curl"
  chmod +x "$T/bin/"*

  export STUB_LOG="$T/restic.log" STUB_SCRATCH="$T/scratch"
  export STUB_SNAP_TIME; STUB_SNAP_TIME=$(date -u -d "@$(( $(date +%s) - 3600 ))" +%Y-%m-%dT%H:%M:%SZ)
  : > "$STUB_LOG"; : > "$T/curl.args"
}

run_script() {
  SCRATCH="$T/scratch" SECRETS="$T/secrets" LOG="$T/run.log" GOTIFY_TOKEN=tok \
    PATH="$T/bin:$PATH" bash "$SCRIPT" >"$T/stdout" 2>&1
}

test_success_writes_the_marker() {
  setup
  yes run_script
  local m; m=$(cat "$T/scratch/READY")
  contains "$m" "DRILL_DATE=$(date -u +%Y-%m-%d)"
  contains "$m" "GOTIFY_IP=10.96.0.9"
  contains "$m" "SNAP_KEYCLOAK=snap-postgres"
  contains "$m" "SNAP_GITEA=snap-gitea"
  contains "$m" "SNAP_IMMICH=snap-immich"
  contains "$m" "SNAPTIME_KEYCLOAK=$STUB_SNAP_TIME"
  contains "$m" "SNAPTIME_GITEA=$STUB_SNAP_TIME"
  contains "$m" "SNAPTIME_IMMICH=$STUB_SNAP_TIME"
  eq "$(wc -l < "$T/scratch/READY" | tr -d ' ')" "8"
}

test_success_sends_no_message() {
  setup
  yes run_script
  eq "$(cat "$T/curl.args")" ""
}

test_readonly() {
  setup
  yes run_script
  local line
  while read -r line; do
    contains "$line" "--no-lock"
    for banned in backup forget prune unlock init; do
      lacks " $line " " $banned "
    done
  done < "$STUB_LOG"
  # and the three kinds of call that must be there, are
  contains "$(cat "$STUB_LOG")" " restore "
  contains "$(cat "$STUB_LOG")" " check "
  contains "$(cat "$STUB_LOG")" "--read-data-subset="
}

test_restores_the_right_things() {
  setup
  yes run_script
  local log; log=$(cat "$STUB_LOG")
  contains "$log" "postgres absent --no-lock restore snap-postgres --target $T/scratch/keycloak --include /db/keycloak.pgdump"
  contains "$log" "gitea absent --no-lock restore snap-gitea --target $T/scratch/gitea"
  contains "$log" "immich absent --no-lock restore snap-immich --target $T/scratch/immich --include-file $T/scratch/immich/include.txt"
  eq "$(sort "$T/scratch/immich/sample.txt")" "/library/upload/u/aa/f1.jpg
/library/upload/u/aa/f2.jpg
/library/upload/u/aa/f3.jpg"
  local inc; inc=$(cat "$T/scratch/immich/include.txt")
  contains "$inc" "/db/immich.pgdump"
  contains "$inc" "**/.immich"
  contains "$inc" "/library/upload/u/aa/f2.jpg"
}

test_wipes_first() {
  setup
  mkdir -p "$T/scratch/old"; echo x > "$T/scratch/old/file"; echo "DRILL_DATE=2020-01-01" > "$T/scratch/READY"
  yes run_script
  lacks "$(cat "$STUB_LOG")" "READY_PRESENT"
  no test -e "$T/scratch/old"
}

test_stale_snapshot_fails() {
  setup
  STUB_SNAP_TIME=$(date -u -d "@$(( $(date +%s) - 259200 ))" +%Y-%m-%dT%H:%M:%SZ)
  no run_script
  no test -e "$T/scratch/READY"
  local args; args=$(cat "$T/curl.args")
  contains "$args" '"title":"Restore drill: restore ✗"'
  contains "$args" '"priority":8'
  contains "$args" "older than 48 hours"
  lacks "$(cat "$STUB_LOG")" " restore "
}

test_restic_failure_writes_no_marker() {
  setup
  export STUB_FAIL_RESTORE_N=2
  no run_script
  no test -e "$T/scratch/READY"
  local args; args=$(cat "$T/curl.args")
  contains "$args" '"title":"Restore drill: restore ✗"'
  contains "$args" "stub restore failure"
}

test_empty_immich_sample_fails() {
  setup
  # an ls that yields no originals at all
  sed -i 's#/library/upload/#/library/thumbs/#' "$T/bin/restic"
  no run_script
  no test -e "$T/scratch/READY"
  contains "$(cat "$T/curl.args")" "no original files to sample"
}

run_tests
