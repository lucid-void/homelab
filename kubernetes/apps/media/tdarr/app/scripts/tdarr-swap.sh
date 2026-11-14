#!/bin/sh
# Verify a Tdarr encode, then swap it in and park the original in the trash.
#
#   tdarr-swap.sh <original> <output>
#   VERIFY_ONLY=1 tdarr-swap.sh <original> <output>   check only, move nothing
#
# Exit: 0 swapped | 10 verify failed, nothing changed | 20 swap refused or
#       failed (including a 4K, HDR or Dolby Vision source), original
#       untouched or restored | 2 usage.
# See design/decisions/tdarr.md. Every path is quoted: library names contain
# spaces and quotes.
set -u

FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"
MEDIA_ROOT="${MEDIA_ROOT:-/Media}"
TRASH_DIR="${TRASH_DIR:-$MEDIA_ROOT/.tdarr-trash}"
DURATION_TOLERANCE="${DURATION_TOLERANCE:-1}"
# Packet counts per stream may differ by this fraction (a dropped frame is a skip).
PACKET_TOLERANCE="${PACKET_TOLERANCE:-0.001}"
# Audio and video must start within this many seconds of each other, as in the source.
SYNC_TOLERANCE="${SYNC_TOLERANCE:-0.1}"
# Measured on real encodes: 0.98-0.99. This floor catches broken output, not taste.
SSIM_MIN="${SSIM_MIN:-0.95}"
SSIM_POINTS="${SSIM_POINTS:-5}"
SSIM_SECONDS="${SSIM_SECONDS:-10}"

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

# --- source guard -----------------------------------------------------------
# Never touch 4K, HDR or Dolby Vision, whatever the flow filters let through:
# those direct-play to the TV and are already HEVC. Only H.264 SDR up to 1080p.
src=$(probe "$orig" v:0 stream=codec_name,height,color_transfer | tr ',' ' ') \
  || swap_fail "ffprobe failed on original"
set -- $src
[ "${1:-}" = h264 ] || swap_fail "source codec is '${1:-}', only h264 is re-encoded"
[ "${2:-0}" -le 1088 ] 2>/dev/null || swap_fail "source is ${2:-?}p, 4K is never re-encoded"
case "${3:-}" in
  smpte2084 | arib-std-b67) swap_fail "source is HDR (${3}), never re-encoded" ;;
esac
if "$FFPROBE" -v error -select_streams v -show_entries stream_side_data=side_data_type -of csv=p=0 -- "$orig" \
  | grep -qi -e dovi -e "dolby vision"; then
  swap_fail "source has Dolby Vision, never re-encoded"
fi

# --- verify ---------------------------------------------------------------
codec=$(probe "$out" v:0 stream=codec_name) || verify_fail "ffprobe failed on output"
[ "$codec" = "hevc" ] || verify_fail "video codec is '$codec', want hevc"

dur_o=$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 -- "$orig") || verify_fail "ffprobe failed on original"
dur_n=$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 -- "$out") || verify_fail "ffprobe failed on output"
awk -v a="$dur_o" -v b="$dur_n" -v t="$DURATION_TOLERANCE" \
  'BEGIN { d = a - b; if (d < 0) d = -d; exit !(d <= t) }' \
  || verify_fail "duration $dur_n s vs original $dur_o s (tolerance $DURATION_TOLERANCE s)"

# mp4 tags untagged audio 'und'; the mkv output carries no tag. Same thing.
for kind in a s; do
  n_o=$(probe "$orig" "$kind" stream=index | wc -l)
  n_n=$(probe "$out" "$kind" stream=index | wc -l)
  [ "$n_o" -eq "$n_n" ] || verify_fail "'$kind' stream count $n_n, original has $n_o"
  l_o=$(probe "$orig" "$kind" stream_tags=language | sed 's/^und$//' | tr '\n' ',')
  l_n=$(probe "$out" "$kind" stream_tags=language | sed 's/^und$//' | tr '\n' ',')
  [ "$l_o" = "$l_n" ] || verify_fail "'$kind' languages '$l_n', original '$l_o'"
done

errs=$("$FFMPEG" -v error -xerror -i "$out" -f null - 2>&1 </dev/null)
[ -z "$errs" ] || verify_fail "decode errors: $(printf '%s' "$errs" | head -3 | tr '\n' ' ')"

# --- playability: skips, drift, client compatibility ------------------------
# pktstat <file> <stream>: "<packets> <first pts> <largest gap> <gaps over 0.5 s>"
pktstat() {
  "$FFPROBE" -v error -select_streams "$2" -show_entries packet=pts_time -of csv=p=0 -- "$1" \
    | sort -g | awk 'BEGIN { m = 0; big = 0; first = 0; seen = 0 }
        { n++ }
        $1 ~ /^-?[0-9.]+$/ { if (seen) { g = $1 - p; if (g > m) m = g; if (g > 0.5) big++ } else first = $1; seen = 1; p = $1 }
        END { printf "%d %s %s %d\n", n, first, m, big }'
}
close_enough() { # close_enough <a> <b> <tolerance fraction>: a within b*tol (min 2)
  awk -v a="$1" -v b="$2" -v t="$3" 'BEGIN { d = a - b; if (d < 0) d = -d; l = b * t; if (l < 2) l = 2; exit !(d <= l) }'
}
n_a=$(probe "$orig" a stream=index | wc -l)
first_v_o=0; first_v_n=0
for st in v:0 $(i=0; while [ "$i" -lt "$n_a" ]; do echo "a:$i"; i=$((i + 1)); done); do
  set -- $(pktstat "$orig" "$st"); p_o=$1; f_o=$2; g_o=$4
  set -- $(pktstat "$out" "$st"); p_n=$1; f_n=$2; g_n=$4
  close_enough "$p_n" "$p_o" "$PACKET_TOLERANCE" || verify_fail "$st has $p_n packets, original $p_o (frames or audio dropped)"
  [ "$g_n" -le "$g_o" ] || verify_fail "$st has $g_n timestamp gaps over 0.5 s, original $g_o (a skip)"
  if [ "$st" = v:0 ]; then
    first_v_o=$f_o; first_v_n=$f_n
  else
    awk -v vo="$first_v_o" -v ao="$f_o" -v vn="$first_v_n" -v an="$f_n" -v t="$SYNC_TOLERANCE" \
      'BEGIN { d = (an - vn) - (ao - vo); if (d < 0) d = -d; exit !(d <= t) }' \
      || verify_fail "$st starts out of sync with video (source offset $(awk -v v="$first_v_o" -v a="$f_o" 'BEGIN{print a - v}') s, output $(awk -v v="$first_v_n" -v a="$f_n" 'BEGIN{print a - v}') s)"
  fi
done

# Direct play on the TV and iPad needs HEVC Main or Main 10, 4:2:0, level 5.1 or lower.
set -- $(probe "$out" v:0 stream=profile,pix_fmt,level | tr ',' ' ')
case "$*" in
  "Main yuv420p "* | "Main 10 yuv420p10le "* | "Main 10 yuv420p "*) ;;
  *) verify_fail "video profile '$*' is not Main/Main 10 4:2:0" ;;
esac
lvl=$(probe "$out" v:0 stream=level)
[ "$lvl" -le 153 ] 2>/dev/null || verify_fail "HEVC level $lvl is above 5.1"

# Compare short segments spread across the file, seeking into both. This also
# proves the output can be seeked, and catches wrong picture, drift and damage.
i=0
while [ "$i" -lt "$SSIM_POINTS" ]; do
  at=$(awk -v d="$dur_o" -v i="$i" -v n="$SSIM_POINTS" -v w="$SSIM_SECONDS" \
    'BEGIN { if (w > d / 3) w = d / 3; t = d * (i + 0.5) / n - w / 2; if (t < 0) t = 0; printf "%.3f %.3f", t, w }')
  set -- $at
  ssim=$("$FFMPEG" -nostdin -v info -ss "$1" -t "$2" -i "$orig" -ss "$1" -t "$2" -i "$out" \
    -lavfi '[0:v:0]setpts=PTS-STARTPTS,format=yuv420p10le[a];[1:v:0]setpts=PTS-STARTPTS,format=yuv420p10le[b];[a][b]ssim' \
    -f null - 2>&1 | sed -n 's/.*SSIM Y:\([0-9.]*\).*/\1/p' | tail -1)
  [ -n "$ssim" ] || verify_fail "could not measure similarity at ${1}s"
  awk -v s="$ssim" -v m="$SSIM_MIN" 'BEGIN { exit !(s >= m) }' \
    || verify_fail "picture at ${1}s differs from the original (SSIM $ssim, need $SSIM_MIN)"
  i=$((i + 1))
done

sz_o=$(stat -c %s "$orig")
sz_n=$(stat -c %s "$out")
[ "$sz_n" -lt "$sz_o" ] || verify_fail "output $sz_n B is not smaller than original $sz_o B"
say "verified: $sz_o -> $sz_n bytes, no drops or gaps, ssim ok at $SSIM_POINTS points"
[ "${VERIFY_ONLY:-}" = 1 ] && exit 0

# --- swap -----------------------------------------------------------------
case "$orig" in
  "$MEDIA_ROOT"/*) rel=${orig#"$MEDIA_ROOT"/} ;;
  *) swap_fail "original is not under $MEDIA_ROOT" ;;
esac
# The output keeps the source container in the flow, so dest is normally the
# original path. A different extension still swaps, as long as nothing is there.
dest="${orig%.*}.${out##*.}"
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
