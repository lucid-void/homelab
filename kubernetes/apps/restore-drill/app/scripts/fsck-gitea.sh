#!/bin/sh
# drill-sso, init step run in the Gitea image (the only drill image with git):
# count the restored repositories and run git fsck on the one with the newest
# commit. Needs no database. Writes its findings to fsck.env for the driver,
# which decides pass or fail — this script only fails if it cannot write.
# Tests: tests/test_fsck_gitea.sh
set -eu

REPO_ROOT=$${REPO_ROOT:-/data/git/gitea-repositories}
RUN_DIR=$${RUN_DIR:-/run/drill}

has_refs() {
  [ -s "$1/packed-refs" ] || [ -n "$(find "$1/refs" -type f 2>/dev/null | head -1)" ]
}

count=0 newest="" newest_time=0 broken=""
# <owner>/<repo>.git, two levels down; *.wiki.git has no row in `repository`
for repo in "$REPO_ROOT"/*/*.git; do
  [ -d "$repo" ] || continue
  case "$repo" in *.wiki.git) continue ;; esac
  count=$((count + 1))
  when=$(git -C "$repo" log -1 --all --format=%ct 2>/dev/null || true)
  if [ -n "$when" ] && [ "$when" -gt "$newest_time" ]; then
    newest_time=$when newest=$repo
  elif [ -z "$when" ] && has_refs "$repo"; then
    # It has branches but git cannot read them: that is damage, not an empty
    # repository. Never skip it in favour of a healthy older one.
    broken=$repo
  fi
done

rc=0 commits=0 name=""
if [ -n "$broken" ]; then
  name=$${broken#"$REPO_ROOT"/}
  echo "[drill] $name has refs but git cannot read its history"
  git -C "$broken" fsck --no-dangling || rc=$?
  [ "$rc" -ne 0 ] || rc=1
elif [ -n "$newest" ]; then
  name=$${newest#"$REPO_ROOT"/}
  echo "[drill] git fsck on $name"
  git -C "$newest" fsck --no-dangling || rc=$?
  commits=$(git -C "$newest" rev-list --count --all 2>/dev/null || echo 0)
fi

mkdir -p "$RUN_DIR"
cat > "$RUN_DIR/fsck.env" <<EOF
REPOS_ON_DISK=$count
FSCK_REPO=$name
FSCK_RC=$rc
FSCK_COMMITS=$commits
EOF
echo "[drill] $count repositories on disk; checked '$${name:-none}' rc=$rc commits=$commits"
