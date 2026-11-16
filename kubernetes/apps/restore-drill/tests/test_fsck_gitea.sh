#!/usr/bin/env bash
# Tests for scripts/fsck-gitea.sh against real throwaway bare repositories.
# Run: bash kubernetes/apps/restore-drill/tests/test_fsck_gitea.sh
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=harness.sh
source "$HERE/harness.sh"
SCRIPT="$SCRIPTS/fsck-gitea.sh"

g() { git -c user.name=t -c user.email=t@example.invalid -c init.defaultBranch=main "$@" >/dev/null 2>&1; }

# make_repo <bare path> <commit count> <commit time epoch>
make_repo() {
  local bare=$1 n=$2 when=$3 work i
  mkdir -p "$(dirname "$bare")"
  g init --bare "$bare"
  [[ $n -eq 0 ]] && return
  work=$(mktemp -d -p "$T")
  g clone "$bare" "$work"
  for i in $(seq 1 "$n"); do
    echo "$i" > "$work/f"
    g -C "$work" add f
    GIT_AUTHOR_DATE="@$when +0000" GIT_COMMITTER_DATE="@$when +0000" g -C "$work" commit -m "c$i"
  done
  g -C "$work" push origin HEAD
}

setup() {
  mkdir -p "$T/root" "$T/run"
  make_repo "$T/root/owner/old.git" 2 1700000000
  make_repo "$T/root/owner/new.git" 3 1790000000
  make_repo "$T/root/owner/new.wiki.git" 1 1795000000   # wikis are not repository rows
  make_repo "$T/root/other/empty.git" 0 0
}

run_script() { REPO_ROOT="$T/root" RUN_DIR="$T/run" sh "$SCRIPT" >"$T/stdout" 2>&1; }

test_counts_repositories_but_not_wikis() {
  setup
  yes run_script
  contains "$(cat "$T/run/fsck.env")" "REPOS_ON_DISK=3"
}

test_checks_the_repository_with_the_newest_commit() {
  setup
  yes run_script
  local e; e=$(cat "$T/run/fsck.env")
  contains "$e" "FSCK_REPO=owner/new.git"
  contains "$e" "FSCK_RC=0"
  contains "$e" "FSCK_COMMITS=3"
}

test_reports_a_corrupt_repository() {
  setup
  # truncate every loose object of the newest repository
  find "$T/root/owner/new.git/objects" -type f ! -name '*.pack' ! -name '*.idx' -exec sh -c 'chmod u+w "$1"; : > "$1"' _ {} \;
  yes run_script
  local e; e=$(cat "$T/run/fsck.env")
  contains "$e" "FSCK_REPO=owner/new.git"
  lacks "$e" "FSCK_RC=0"
}

test_no_repository_with_commits() {
  mkdir -p "$T/root" "$T/run"
  make_repo "$T/root/other/empty.git" 0 0
  yes run_script
  local e; e=$(cat "$T/run/fsck.env")
  contains "$e" "REPOS_ON_DISK=1"
  contains "$e" "FSCK_COMMITS=0"
}

test_output_is_sourceable() {
  setup
  yes run_script
  yes bash -c "set -eu; source '$T/run/fsck.env'; [ \"\$FSCK_COMMITS\" = 3 ]"
}

run_tests
