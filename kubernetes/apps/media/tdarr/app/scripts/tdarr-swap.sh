#!/bin/sh
# Verify a Tdarr encode, then swap it in and park the original in the trash.
#
#   tdarr-swap.sh <original> <output>
#
# Exit: 0 swapped | 10 verify failed, nothing changed | 20 swap refused or
#       failed, original restored | 2 usage.
# See design/decisions/tdarr.md. Every path is quoted: library names contain
# spaces and quotes.
set -u

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"
MEDIA_ROOT="${MEDIA_ROOT:-/Media}"
TRASH_DIR="${TRASH_DIR:-$MEDIA_ROOT/.tdarr-trash}"
DURATION_TOLERANCE="${DURATION_TOLERANCE:-1}"

say() { printf 'swap: %s\n' "$*"; }
verify_fail() { say "VERIFY FAILED: $*" >&2; exit 10; }
swap_fail() { say "SWAP REFUSED: $*" >&2; exit 20; }

[ $# -eq 2 ] || { echo "usage: $0 <original> <output>" >&2; exit 2; }
orig=$1
out=$2

[ -f "$orig" ] || swap_fail "original missing: $orig"
[ -f "$out" ] || verify_fail "output missing: $out"
[ -s "$out" ] || verify_fail "output is empty"

probe() { # probe <file> <select> <entries>
  "$FFPROBE" -v error -select_streams "$2" -show_entries "$3" -of csv=p=0 -- "$1"
}

# --- verify ---------------------------------------------------------------
codec=$(probe "$out" v:0 stream=codec_name) || verify_fail "ffprobe failed on output"
[ "$codec" = "hevc" ] || verify_fail "video codec is '$codec', want hevc"

dur_o=$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 -- "$orig") || verify_fail "ffprobe failed on original"
dur_n=$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 -- "$out") || verify_fail "ffprobe failed on output"
awk -v a="$dur_o" -v b="$dur_n" -v t="$DURATION_TOLERANCE" \
  'BEGIN { d = a - b; if (d < 0) d = -d; exit !(d <= t) }' \
  || verify_fail "duration $dur_n s vs original $dur_o s (tolerance $DURATION_TOLERANCE s)"

for kind in a s; do
  n_o=$(probe "$orig" "$kind" stream=index | wc -l)
  n_n=$(probe "$out" "$kind" stream=index | wc -l)
  [ "$n_o" -eq "$n_n" ] || verify_fail "'$kind' stream count $n_n, original has $n_o"
  l_o=$(probe "$orig" "$kind" stream_tags=language | tr '\n' ',')
  l_n=$(probe "$out" "$kind" stream_tags=language | tr '\n' ',')
  [ "$l_o" = "$l_n" ] || verify_fail "'$kind' languages '$l_n', original '$l_o'"
done

errs=$("$FFMPEG" -v error -i "$out" -f null - 2>&1 </dev/null)
[ -z "$errs" ] || verify_fail "decode errors: $(printf '%s' "$errs" | head -3 | tr '\n' ' ')"

sz_o=$(stat -c %s "$orig")
sz_n=$(stat -c %s "$out")
[ "$sz_n" -lt "$sz_o" ] || verify_fail "output $sz_n B is not smaller than original $sz_o B"
say "verified: $sz_o -> $sz_n bytes"

# --- swap -----------------------------------------------------------------
case "$orig" in
  "$MEDIA_ROOT"/*) rel=${orig#"$MEDIA_ROOT"/} ;;
  *) swap_fail "original is not under $MEDIA_ROOT" ;;
esac
dest="${orig%.*}.mkv"
if [ "$dest" != "$orig" ] && [ -e "$dest" ]; then
  swap_fail "$dest already exists"
fi

trash="$TRASH_DIR/$rel"
[ -e "$trash" ] && trash="$trash.$(date +%s)"
mkdir -p -- "$(dirname -- "$trash")" || swap_fail "cannot create trash directory"

# Same filesystem for all three, or mv becomes a long copy with a window where
# the file is half-written.
dev_o=$(stat -c %d -- "$(dirname -- "$orig")")
dev_n=$(stat -c %d -- "$(dirname -- "$out")")
dev_t=$(stat -c %d -- "$(dirname -- "$trash")")
if [ "$dev_o" != "$dev_n" ] || [ "$dev_o" != "$dev_t" ]; then
  swap_fail "output, original and trash are not on one filesystem"
fi

mv -- "$orig" "$trash" || swap_fail "could not move original to trash"
# mv keeps the original's mtime; the purge job ages trash by mtime.
touch -- "$trash"
if ! mv -- "$out" "$dest"; then
  mv -- "$trash" "$orig"
  swap_fail "could not move output into place; original restored"
fi
say "swapped: $dest (original kept at $trash)"
