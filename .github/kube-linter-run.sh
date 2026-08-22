#!/usr/bin/env bash
# Run kube-linter over the repo's raw workload manifests, writing JSON to $1.
#
# ── Why directory targets instead of `kube-linter lint kubernetes/` ──────────
# kube-linter treats any directory containing a kustomization.yml as a kustomize
# root: it renders that directory and DOES NOT DESCEND into it. Every
# kubernetes/apps/<ns>/kustomization.yml renders to Flux Kustomization CRs plus a
# Namespace — zero pod specs — so a top-level scan walked straight past the whole
# app tree and only ever saw 6 files (3 Flux, 3 Talos). Targeting leaf
# directories also lets kustomize resolve each app's ServiceAccount/RBAC next to
# its workload, which removes non-existent-service-account false positives.
#
# Targets are DISCOVERED, never hardcoded, so a newly added app is linted
# automatically instead of silently escaping the gate.
set -euo pipefail

out="${1:?usage: kube-linter-run.sh <output.json>}"
cfg=".github/kube-linter-config.yaml"
err="$(mktemp)"

targets=$( { grep -rlE '^kind: (Deployment|StatefulSet|DaemonSet|Job|CronJob|Pod)$' \
               kubernetes/ --include='*.yml' --include='*.yaml' || true; } \
           | xargs -r -n1 dirname | sort -u )

if [ -z "$targets" ]; then
  echo "::error::kube-linter found no workload manifests under kubernetes/ — discovery is broken and the gate would be vacuous."
  exit 1
fi

echo "Linting $(printf '%s\n' "$targets" | wc -l) director(ies):"
printf '%s\n' "$targets" | sed 's/^/  /'

set +e
printf '%s\n' "$targets" | xargs kube-linter lint --config "$cfg" --format json >"$out" 2>"$err"
rc=$?
set -e

# kube-linter exits 1 BOTH for "lint errors found" and for a failure to load the
# config. Tell them apart by whether a parseable report came out. A config error
# must fail the job — silently counting it as zero findings is exactly the bug
# that made this gate pass on every PR while linting nothing.
if ! jq -e 'has("Reports")' "$out" >/dev/null 2>&1; then
  echo "::error::kube-linter produced no parseable report (exit ${rc}) — tooling/config failure, not a clean scan."
  echo "--- stderr ---"; cat "$err"
  echo "--- stdout (first 20 lines) ---"; head -20 "$out"
  exit 1
fi

cat "$err" >&2 || true
echo "kube-linter reported $(jq '.Reports | length' "$out") finding(s)."
