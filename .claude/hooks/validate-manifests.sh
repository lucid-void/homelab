#!/usr/bin/env bash
# Claude Code PostToolUse wrapper: parse file_path from stdin JSON, delegate.
# The real logic lives in .agents/scripts/validate-manifests.sh so any harness
# (or a human) can run it without a hook.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FILE_PATH=$(python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null || true)

[[ -z "$FILE_PATH" ]] && exit 0
case "$FILE_PATH" in
  */kubernetes/*.yml|*/kubernetes/*.yaml) ;;
  *) exit 0 ;;
esac

# Silent on success; only a failure is worth transcript space.
"$REPO/.agents/scripts/validate-manifests.sh" "$FILE_PATH" || {
  echo "kubeconform FAILED: $FILE_PATH"
  exit 0   # report, never block the edit
}
