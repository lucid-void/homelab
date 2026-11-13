#!/usr/bin/env bash
# Claude Code PreToolUse hook (Write|Edit): on a manifest edit under
# kubernetes/, NAME the design doc(s) that govern that path as
# additionalContext. Testing showed this lands alongside the tool result, not
# before the model decides to call Write — so it can't prevent a first edit
# made without the doc; it's a corrective, landing in time for the next turn
# (and every edit after the first to that path in the session).
#
# The portable mechanism that actually gates the first edit is the AGENTS.md
# routing list, which every harness reads before deciding what to write;
# this hook is Claude Code's belt-and-braces backstop on top of it.
#
# POINTERS, NOT CONTENT — this hook used to inline the whole doc. That paid
# for every doc twice: the routing list had usually made the agent read it
# already, and the hook cannot know that. Docs here run 1-22 KB and one path
# can match three, so a single edit cost up to ~4,000 tokens of duplicate
# context. It now emits one line per doc (~30 tokens) and leaves the reading —
# and the choice of which section — to the agent.
#
# DELIVERY CONTRACT — verified against the installed Claude Code binary
# (~/.local/share/claude/versions/2.1.238). For PreToolUse the binary's own
# help text reads "Exit code 0 - stdout/stderr not shown": stdout is discarded,
# not even surfaced in transcript mode. The ONLY channel that reaches the model
# is a JSON object on stdout carrying
#   {"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":...}}
# which the binary's PreToolUse schema accepts and documents as "Text injected
# into model context". Printing plain text delivers nothing at all.
#
# DEDUPE — a doc is named at most once per session_id, so N edits under one
# service cost its pointers once, not N times.
#
# SAFETY — this runs before every Write and Edit. Every failure mode must be
# "produce nothing, exit 0": never a nonzero exit (2 would BLOCK the tool
# call), never stderr noise, and above all never malformed JSON — partial or
# unparseable output is worse than silence.
set -uo pipefail

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
# one degrades to "name them anyway, record nothing" rather than failing: a
# duplicate pointer is a small cost, a swallowed one is a correctness bug.
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

already_named() {
  [[ -n "$STATE_FILE" ]] || return 1
  grep -qxF "$1" "$STATE_FILE" 2>/dev/null
}

# --- build the payload ---------------------------------------------------
NAMED=()
while IFS= read -r doc; do
  [[ -z "$doc" ]] && continue
  [[ -f "$REPO/$doc" ]] || continue
  already_named "$doc" && continue
  NAMED+=("$doc")
done <<< "$DOCS"

# Everything already named this session: say nothing at all.
(( ${#NAMED[@]} == 0 )) && exit 0

PAYLOAD="$FILE_PATH is governed by the design doc(s) below. If you have not read one this session, read it before your next edit here — its Contents list first when it has one, then only the section you need:"
for doc in "${NAMED[@]}"; do
  PAYLOAD+=$'\n'"  $doc"
done

# --- emit ---------------------------------------------------------------
# The path can carry characters that need escaping, so the string MUST be
# encoded by a real JSON encoder. jq preferred, python3 as fallback; with
# neither, stay silent rather than hand-roll escaping.
OUT=""
if command -v jq >/dev/null 2>&1; then
  OUT=$(jq -n --arg c "$PAYLOAD" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",additionalContext:$c}}' 2>/dev/null) || OUT=""
elif command -v python3 >/dev/null 2>&1; then
  OUT=$(PAYLOAD="$PAYLOAD" python3 -c 'import json,os,sys
sys.stdout.write(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":os.environ["PAYLOAD"]}}))' 2>/dev/null) || OUT=""
fi

[[ -z "$OUT" ]] && exit 0

printf '%s\n' "$OUT"

if [[ -n "$STATE_FILE" ]]; then
  printf '%s\n' "${NAMED[@]}" >> "$STATE_FILE" 2>/dev/null
fi

exit 0