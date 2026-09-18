#!/usr/bin/env bash
# Claude Code PreToolUse hook (Write|Edit): before a manifest edit under
# kubernetes/, print the design doc(s) that govern that path so they enter
# context automatically, ahead of the edit.
#
# Vendor-specific belt-and-braces on top of the AGENTS.md routing table,
# which stays the portable mechanism for every other harness.
#
# A PreToolUse hook must never interrupt the agent on bad input: any failure
# here means "print nothing, exit 0" — never a nonzero exit, never stderr
# noise that could read as a blocking error.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 0

INPUT="$(cat)" || exit 0

# Pull tool_input.file_path out of the hook JSON with grep/sed rather than
# spawning a language interpreter — this runs before every Write/Edit in the
# session, so the common case (a path that matches no rule, or isn't even a
# file_path-bearing tool call) must stay cheap.
FILE_PATH=$(printf '%s' "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/^[^:]*:[[:space:]]*"//; s/"$//')

[[ -z "$FILE_PATH" ]] && exit 0

DOCS="$("$REPO/.agents/scripts/route-context.sh" "$FILE_PATH" 2>/dev/null)" || exit 0
[[ -z "$DOCS" ]] && exit 0

while IFS= read -r doc; do
  [[ -z "$doc" ]] && continue
  echo "=== required reading for $FILE_PATH: $doc ==="
  cat "$REPO/$doc" 2>/dev/null
done <<< "$DOCS"

exit 0
