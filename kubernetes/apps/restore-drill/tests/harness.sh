#!/usr/bin/env bash
# Tiny test harness shared by the restore-drill test files. Source it, define
# test_* functions, then call run_tests. Each test runs in its own subshell with
# its own temp dir in $T. Assertions exit the subshell on failure — `set -e` is
# not relied on, because bash never trips it for `! cmd`.

fail() { echo "    assertion failed: $*" >&2; exit 1; }
eq() { [[ "$1" == "$2" ]] || fail "expected [$2], got [$1]"; }
contains() { [[ "$1" == *"$2"* ]] || fail "expected to contain [$2], got [$1]"; }
lacks() { [[ "$1" != *"$2"* ]] || fail "expected NOT to contain [$2], got [$1]"; }
yes() { "$@" >/dev/null 2>&1 || fail "expected success: $*"; }
no() { if "$@" >/dev/null 2>&1; then fail "expected failure: $*"; fi; }

# The scripts in ../app/scripts are Flux-substituted: every shell `${VAR}` is
# written `$${VAR}`, and the domain is `${CLUSTER_DOMAIN}`. Render them the way
# Flux does (domain -> example.com) into $SCRIPTS, and test those copies.
SCRIPTS=$(mktemp -d)
trap 'rm -rf "$SCRIPTS"' EXIT
for _s in "$(dirname "${BASH_SOURCE[0]}")"/../app/scripts/*.sh; do
  sed -e 's/\${CLUSTER_DOMAIN}/example.com/g' -e 's/\$\${/${/g' "$_s" > "$SCRIPTS/$(basename "$_s")"
done
unset _s

run_tests() {
  local failed=0 t
  for t in $(declare -F | awk '{print $3}' | grep '^test_'); do
    if ( T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; "$t" ); then
      echo "ok   $t"
    else
      echo "FAIL $t"; failed=1
    fi
  done
  [[ $failed -eq 0 ]] && echo "all passed" || echo "FAILURES"
  return $failed
}
