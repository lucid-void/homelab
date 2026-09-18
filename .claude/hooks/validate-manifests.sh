#!/usr/bin/env bash
# Claude Code PostToolUse wrapper: parse file_path from stdin JSON, delegate.
# Filtering (which files count as a k8s manifest) lives only in
# .agents/scripts/validate-manifests.sh, so there is exactly one place that
# decides — this wrapper just extracts file_path and translates the result.
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

# Silent on success and on skip. Under the PostToolUse contract, stdout on
# a zero exit only reaches the human transcript — it is exit 2 with the
# message on stderr that gets fed back into the agent's own context. So a
# failure must exit 2 and report on stderr, never stdout; never block.
"$REPO/.agents/scripts/validate-manifests.sh" "$FILE_PATH" >&2 || {
  echo "kubeconform FAILED: $FILE_PATH" >&2
  exit 2
}
