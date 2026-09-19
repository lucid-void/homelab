#!/usr/bin/env bash
# Claude Code PreToolUse hook (Write|Edit): on a manifest edit under
# kubernetes/, inject the design doc(s) that govern that path as
# additionalContext. Testing showed this lands alongside the tool result, not
# before the model decides to call Write — so it can't prevent a first edit
# made without the doc; it's a corrective, landing in time for the next turn
# (and every edit after the first to that path in the session).
#
# The portable mechanism that actually gates the first edit is the AGENTS.md
# routing table, which every harness reads before deciding what to write;
# this hook is Claude Code's belt-and-braces backstop on top of it.
#
# DELIVERY CONTRACT — verified against the installed Claude Code binary
# (~/.local/share/claude/versions/2.1.238). For PreToolUse the binary's own
# help text reads "Exit code 0 - stdout/stderr not shown": stdout is discarded,
# not even surfaced in transcript mode. The ONLY channel that reaches the model
# is a JSON object on stdout carrying
#   {"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":...}}
# which the binary's PreToolUse schema accepts and documents as "Text injected
# into model context". Printing the doc as plain text — which this hook did
# until 2026-09-19 — delivers nothing at all.
#
# BUDGET — the docs this routes to are 5-9 KB each and a path can match three
# of them, so a naive injector costs ~2,000 tokens per Write/Edit and, being
# stateless, re-pays that on every edit of the same file. Two guards:
#   1. per-session dedupe: a doc is inlined at most once per session_id, so N
#      edits to one manifest cost the docs once, not N times;
#   2. a per-injection byte cap: past MAX_INLINE_BYTES the remaining docs are
#      named by path instead of inlined, and stay unrecorded so a later edit
#      can still inline them.
#
# SAFETY — this runs before every Write and Edit. Every failure mode must be
# "produce nothing, exit 0": never a nonzero exit (2 would BLOCK the tool
# call), never stderr noise, and above all never malformed JSON — partial or
# unparseable output is worse than silence.
set -uo pipefail

# Budget for one injection's inlined doc bytes (~3,000 tokens). The first
# matched doc is always inlined even if it alone exceeds this.
MAX_INLINE_BYTES=12000

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)" || exit 0
[[ -n "$REPO" ]] || exit 0

INPUT="$(cat)" || exit 0

# Pull tool_input.file_path out of the hook JSON with grep/sed rather than
# spawning a language interpreter — this runs before every Write/Edit in the
# session, so the common case (a path that matches no rule, or isn't even a
# file_path-bearing tool call) must stay cheap. Everything expensive below,
# jq included, happens only after a rule has actually matched.
FILE_PATH=$(printf '%s' "$INPUT" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/^[^:]*:[[:space:]]*"//; s/"$//')

[[ -z "$FILE_PATH" ]] && exit 0

DOCS="$("$REPO/.agents/scripts/route-context.sh" "$FILE_PATH" 2>/dev/null)" || exit 0
[[ -z "$DOCS" ]] && exit 0

# --- per-session dedupe state -------------------------------------------
# session_id comes from the hook's own stdin JSON. A missing or unparseable
# one degrades to "inject anyway, record nothing" rather than failing: a
# duplicate injection is a cost, a swallowed one is a correctness bug.
SESSION_ID=$(printf '%s' "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/^[^:]*:[[:space:]]*"//; s/"$//')
SESSION_ID="${SESSION_ID//[^A-Za-z0-9_-]/}"

STATE_FILE=""
if [[ -n "$SESSION_ID" ]]; then
  STATE_DIR="${TMPDIR:-/tmp}/claude-inject-context-$(id -u 2>/dev/null || echo 0)"
  if mkdir -p "$STATE_DIR" 2>/dev/null; then
    chmod 700 "$STATE_DIR" 2>/dev/null
    STATE_FILE="$STATE_DIR/$SESSION_ID"
    { : >> "$STATE_FILE"; } 2>/dev/null || STATE_FILE=""
  fi
fi

already_injected() {
  [[ -n "$STATE_FILE" ]] || return 1
  grep -qxF "$1" "$STATE_FILE" 2>/dev/null
}

# --- build the payload ---------------------------------------------------
PAYLOAD="$(mktemp "${TMPDIR:-/tmp}/inject-context.XXXXXX" 2>/dev/null)" || exit 0
trap 'rm -f "$PAYLOAD"' EXIT

INLINED=()
DEFERRED=()
BYTES=0

while IFS= read -r doc; do
  [[ -z "$doc" ]] && continue
  [[ -f "$REPO/$doc" ]] || continue
  already_injected "$doc" && continue

  SIZE=$(wc -c < "$REPO/$doc" 2>/dev/null) || SIZE=0
  if (( ${#INLINED[@]} > 0 && BYTES + SIZE > MAX_INLINE_BYTES )); then
    DEFERRED+=("$doc")
    continue
  fi

  {
    printf '=== required reading before editing %s: %s ===\n' "$FILE_PATH" "$doc"
    cat "$REPO/$doc"
    printf '\n'
  } >> "$PAYLOAD" 2>/dev/null || exit 0

  INLINED+=("$doc")
  BYTES=$(( BYTES + SIZE ))
done <<< "$DOCS"

# Everything already seen this session: say nothing at all.
if (( ${#INLINED[@]} == 0 && ${#DEFERRED[@]} == 0 )); then
  exit 0
fi

if (( ${#DEFERRED[@]} > 0 )); then
  {
    printf 'Also governs %s, not inlined here (injection byte budget) — read if relevant:\n' "$FILE_PATH"
    printf '  %s\n' "${DEFERRED[@]}"
  } >> "$PAYLOAD" 2>/dev/null || exit 0
fi

[[ -s "$PAYLOAD" ]] || exit 0

# --- emit ---------------------------------------------------------------
# The docs carry backticks, double quotes, backslashes and newlines, so the
# string MUST be escaped by a real JSON encoder. jq preferred, python3 as
# fallback; with neither, stay silent rather than hand-roll escaping.
OUT=""
if command -v jq >/dev/null 2>&1; then
  OUT=$(jq -n --rawfile c "$PAYLOAD" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:$c}}' 2>/dev/null) || OUT=""
elif command -v python3 >/dev/null 2>&1; then
  OUT=$(PAYLOAD="$PAYLOAD" python3 -c 'import json,os,sys
with open(os.environ["PAYLOAD"], encoding="utf-8", errors="replace") as f:
    c = f.read()
sys.stdout.write(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":c}}))' 2>/dev/null) || OUT=""
fi

[[ -z "$OUT" ]] && exit 0

printf '%s\n' "$OUT"

# Record only what was actually inlined. A deferred doc stays unrecorded so
# the next edit that routes to it can still inline it.
if [[ -n "$STATE_FILE" ]] && (( ${#INLINED[@]} > 0 )); then
  printf '%s\n' "${INLINED[@]}" >> "$STATE_FILE" 2>/dev/null
fi

exit 0
