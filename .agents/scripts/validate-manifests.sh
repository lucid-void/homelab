#!/usr/bin/env bash
# Validate one kubernetes manifest against upstream + CRD schemas.
# Usage: .agents/scripts/validate-manifests.sh <file>
# Takes ONE file — loop over files to check more than one.
# Exits 0 if valid, not a kubernetes manifest, or not validated; non-zero only
# on a genuine schema violation (2 on a missing argument).
# A run that could not validate prints one "SKIPPED: <reason>" line to stderr
# and still exits 0: a directory argument, mise not installed, or a schema
# that could not be fetched/decoded. Silent exit 0 is reserved for "valid" and
# for the ordinary not-a-manifest cases (missing path, outside kubernetes/, no
# `kind:`) — the PostToolUse hook runs this on every edit, so those stay quiet.
set -uo pipefail

FILE="${1:-}"
[[ -z "$FILE" ]] && { echo "usage: $0 <file>" >&2; exit 2; }
[[ -d "$FILE" ]] && { echo "SKIPPED: $FILE is a directory, nothing validated — pass one file at a time" >&2; exit 0; }
[[ ! -f "$FILE" ]] && exit 0
case "$FILE" in
  */kubernetes/*.yml|*/kubernetes/*.yaml|kubernetes/*.yml|kubernetes/*.yaml) ;;
  *) exit 0 ;;
esac
# Not every YAML file under kubernetes/ is a Kubernetes manifest — Helm values
# files, kustomizeconfig.yml, helmfile.yml, talconfig.yaml, and SOPS-encrypted
# Talos files all live there but carry no `kind:`. kubeconform correctly
# rejects those with "missing 'kind' key"; reporting that as a manifest
# defect is the bug, not the file. Skip anything with no top-level `kind:` in
# any document (a multi-doc file only needs one document to carry it).
grep -qE '^kind:[[:space:]]*[A-Za-z]' "$FILE" || exit 0

command -v mise >/dev/null 2>&1 || { echo "SKIPPED: mise not found, $FILE not validated" >&2; exit 0; }

# CRD schemas are fetched unauthenticated from raw.githubusercontent.com, once
# per CRD kind per run. Cache them on disk so a session's repeated edits cost
# one fetch, not one per write — which is also what keeps this usable while
# rate-limited or offline.
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/kubeconform"
CACHE_ARGS=()
mkdir -p "$CACHE" 2>/dev/null && CACHE_ARGS=(-cache "$CACHE")

OUT=$(mise exec -- kubeconform \
  -strict \
  -ignore-missing-schemas \
  "${CACHE_ARGS[@]}" \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  "$FILE" 2>&1)
RC=$?

[[ $RC -eq 0 ]] && exit 0

# `-ignore-missing-schemas` covers a 404 but NOT a transport failure: an
# unreachable or rate-limiting raw.githubusercontent.com makes kubeconform
# exit 1 with "failed downloading schema", which is indistinguishable from a
# real violation to the caller. Reporting that as a violation feeds the agent
# a fabricated error on a perfectly good manifest, so a run whose only
# failures are download failures is a SKIP. A run that also carries a genuine
# violation still fails, with the full output.
#
# A truncated -cache entry (interrupted run, full disk, mid-download network
# cut) fails the same way: kubeconform's own JSON decode of the schema
# surfaces as "failed validation: <go json error>" — e.g. "invalid character
# 'o' in literal null (expecting 'u')", "unexpected EOF", plain "EOF". A real
# jsonschema violation never reads that way — it's always "... is invalid:
# problem validating schema ... jsonschema validation failed with ... got X,
# want Y" — so anchoring on the "failed validation:" prefix can't mask one.
RESIDUAL=$(printf '%s\n' "$OUT" \
  | grep -v 'failed downloading schema' \
  | grep -Ev 'failed validation: (invalid character|unexpected EOF|EOF$)' \
  | tr -d '[:space:]')
[[ -z "$RESIDUAL" ]] && { echo "SKIPPED: schema download or cache decode failed, $FILE not validated" >&2; exit 0; }

printf '%s\n' "$OUT" >&2
exit "$RC"
