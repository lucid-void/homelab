#!/usr/bin/env bash
# Runs after Edit/Write tool calls. Validates any kubernetes YAML file that was just modified.
set -euo pipefail

# Parse file_path from stdin JSON (Claude Code PostToolUse hook input)
STDIN=$(cat)
FILE_PATH=$(echo "$STDIN" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Only validate files inside kubernetes/
[[ -z "$FILE_PATH" ]] && exit 0
[[ "$FILE_PATH" != */kubernetes/*.yml ]] && [[ "$FILE_PATH" != */kubernetes/*.yaml ]] && exit 0
[[ ! -f "$FILE_PATH" ]] && exit 0

# Run via mise exec so kubeconform is found regardless of PATH
which mise &>/dev/null || exit 0

echo "kubeconform: $FILE_PATH"

mise exec -- kubeconform \
  -strict \
  -ignore-missing-schemas \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  "$FILE_PATH" 2>&1 && echo "OK" || true
