#!/usr/bin/env bash
# Extract every hard fact from the agent-facing docs into a sorted, deduped checklist.
#
# Usage: .agents/scripts/extract-facts.sh [path...]
#   No args: scans AGENTS.md, .claude/CLAUDE.md and design/**/*.md, whichever exist.
# Output:  one fact per line on stdout, sorted -u.
#
# Each pattern runs as its OWN grep pass. A single multi-pattern grep consumes
# characters left-to-right and never rescans them, so a broad pattern starves a
# narrow one that overlaps it (measured: 253 facts combined vs 982 separated with
# multi-`-e` form; alternation-joined single pattern yields ~2400).
set -euo pipefail
export LC_ALL=C

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  mapfile -t FILES < <(
    { ls AGENTS.md .claude/CLAUDE.md 2>/dev/null
      find design -name '*.md' 2>/dev/null
    } | LC_ALL=C sort -u
  )
fi
[[ ${#FILES[@]} -eq 0 ]] && { echo "extract-facts: no input files" >&2; exit 1; }

PATTERNS=(
  '\b[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?\b'                       # IPv4 / CIDR
  '\b(kubernetes|infra|design|\.github|\.agents|\.claude)/[A-Za-z0-9_./*-]+' # repo paths
  '/(volume2|var|etc|opt|config|data|media|home|tmp|usr)/[A-Za-z0-9_./-]+'   # absolute paths
  '\b[A-Z][A-Z0-9]*(_[A-Z0-9]+){1,}\b'                                  # ENV_VAR names
  '--[a-z][a-z0-9-]{2,}'                                                # CLI flags
  '\b[a-z0-9.-]+\.(io|sh|dev|cc|com|org|net)/[A-Za-z0-9_./:-]+'         # registries / URLs
  '\b[a-z0-9.-]+\.(io|com|sh)/[a-z0-9._-]+'                             # annotation keys
  '\b(kubectl|flux|talosctl|talhelper|kubeseal|helm|kubeconform|restic|rclone|tofu|mise|promtool|curl|jq|kubent|shred)\b +[a-z][a-z-]*' # command verbs
  '\bv?[0-9]+\.[0-9]+(\.[0-9]+)?([.-][A-Za-z0-9]+)*\b'                  # versions
  '`[^`]{2,60}`'                                                        # anything the author backticked
)

for p in "${PATTERNS[@]}"; do
  cat "${FILES[@]}" 2>/dev/null | grep -ohE -e "$p" || true
done \
  | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//; s/[.,;:)]+$//' \
  | { grep -vE '^[[:space:]]*$' || true; } \
  | LC_ALL=C sort -u
