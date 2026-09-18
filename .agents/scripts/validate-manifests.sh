#!/usr/bin/env bash
# Validate one kubernetes manifest against upstream + CRD schemas.
# Usage: .agents/scripts/validate-manifests.sh <file>
# Exits 0 if valid or not a kubernetes manifest; non-zero on a schema violation.
set -euo pipefail

FILE="${1:-}"
[[ -z "$FILE" ]] && { echo "usage: $0 <file>" >&2; exit 2; }
[[ ! -f "$FILE" ]] && exit 0
case "$FILE" in
  */kubernetes/*.yml|*/kubernetes/*.yaml|kubernetes/*.yml|kubernetes/*.yaml) ;;
  *) exit 0 ;;
esac
command -v mise >/dev/null 2>&1 || exit 0

mise exec -- kubeconform \
  -strict \
  -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  "$FILE"
