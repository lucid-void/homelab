#!/bin/sh
# Run inside the node pod, never against the live library:
#   kubectl exec -i -n media deploy/tdarr-node -- sh -s < kubernetes/apps/media/tdarr/tests/tdarr-swap.test.sh
set -u
SWAP="${SWAP:-/scripts/tdarr-swap.sh}"
FFMPEG="${FFMPEG:-ffmpeg}"
FFPROBE="${FFPROBE:-ffprobe}"
export MEDIA_ROOT="${MEDIA_ROOT:-/Media/.tdarr-test}"
export TRASH_DIR="$MEDIA_ROOT/.trash"
T="$MEDIA_ROOT/run-$$"
mkdir -p "$T" "$TRASH_DIR"
fail=0

ok()   { echo "ok   - $1"; }
bad()  { echo "FAIL - $1"; fail=1; }
check() { # check <name> <command...>
  n=$1; shift
  if "$@"; then ok "$n"; else bad "$n"; fi
}
exit_is() { # exit_is <name> <want> <command...>
  n=$1; w=$2; shift 2
  "$@" >/dev/null 2>&1; g=$?
  if [ "$g" -eq "$w" ]; then ok "$n"; else bad "$n (want $w, got $g)"; fi
}
says() { # says <name> <pattern> <command...>: exits 10 and the message matches
  n=$1; pat=$2; shift 2
  m=$("$@" 2>&1); g=$?
  if [ "$g" -eq 10 ] && printf '%s' "$m" | grep -q -- "$pat"; then ok "$n"; else bad "$n (exit $g: $m)"; fi
}
is_hevc() { [ "$("$FFPROBE" -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 -- "$1")" = hevc ]; }
not_old() { [ -z "$(find "$1" -mtime +1)" ]; }

mksrc() { # 6 s h264 + aac, big enough that hevc at crf 30 is smaller
  "$FFMPEG" -v error -y -f lavfi -i testsrc=d=6:s=640x360:r=24 -f lavfi -i sine=d=6 \
    -c:v libx264 -pix_fmt yuv420p -crf 12 -c:a aac "$1"
}
mkout() { # mkout <src> <out> [extra ffmpeg args...]
  s=$1; o=$2; shift 2
  "$FFMPEG" -v error -y -i "$s" -c:v libx265 -pix_fmt yuv420p10le -x265-params log-level=error -crf 30 -c:a copy "$@" "$o"
}

# 1. pass: mkv -> mkv, same name, original lands in trash with a fresh mtime
mksrc "$T/a.mkv"; mkout "$T/a.mkv" "$T/a.out.mkv"
touch -d '2020-01-01' "$T/a.mkv"
exit_is "pass mkv exits 0" 0 sh "$SWAP" "$T/a.mkv" "$T/a.out.mkv"
check "pass: file at original path is hevc" is_hevc "$T/a.mkv"
check "pass: original is in trash" test -f "$TRASH_DIR/run-$$/a.mkv"
check "pass: trashed original mtime refreshed (review focus 3)" not_old "$TRASH_DIR/run-$$/a.mkv"

# 2. pass: mp4 -> mkv changes the extension
mksrc "$T/b.mp4"; mkout "$T/b.mp4" "$T/b.out.mkv"
exit_is "pass mp4 exits 0" 0 sh "$SWAP" "$T/b.mp4" "$T/b.out.mkv"
check "mp4: result is b.mkv" test -f "$T/b.mkv"
check "mp4: b.mp4 gone from library path" test ! -e "$T/b.mp4"
check "mp4: original b.mp4 in trash" test -f "$TRASH_DIR/run-$$/b.mp4"

# 3. refuse: mp4 with an mkv already beside it (review focus 1)
mksrc "$T/c.mp4"; mkout "$T/c.mp4" "$T/c.out.mkv"; cp "$T/c.out.mkv" "$T/c.mkv"
exit_is "collision exits 20" 20 sh "$SWAP" "$T/c.mp4" "$T/c.out.mkv"
check "collision: c.mp4 untouched" test -f "$T/c.mp4"
check "collision: existing c.mkv untouched" cmp -s "$T/c.mkv" "$T/c.out.mkv"

# 4. reject: output still h264
mksrc "$T/d.mkv"; "$FFMPEG" -v error -y -i "$T/d.mkv" -c:v libx264 -pix_fmt yuv420p -crf 30 -c:a copy "$T/d.out.mkv"
exit_is "h264 output exits 10" 10 sh "$SWAP" "$T/d.mkv" "$T/d.out.mkv"
check "h264: original untouched" test -f "$T/d.mkv"

# 5. reject: duration short by 3 s
mksrc "$T/e.mkv"; mkout "$T/e.mkv" "$T/e.out.mkv" -t 3
exit_is "short output exits 10" 10 sh "$SWAP" "$T/e.mkv" "$T/e.out.mkv"

# 6. reject: audio dropped
mksrc "$T/f.mkv"; mkout "$T/f.mkv" "$T/f.out.mkv" -an
exit_is "missing audio exits 10" 10 sh "$SWAP" "$T/f.mkv" "$T/f.out.mkv"

# 7. reject: probes fine but corrupt mid-stream (review focus 4)
mksrc "$T/g.mkv"; mkout "$T/g.mkv" "$T/g.out.mkv"
sz=$(stat -c %s "$T/g.out.mkv")
dd if=/dev/urandom of="$T/g.out.mkv" bs=1 seek=$((sz / 2)) count=4096 conv=notrunc 2>/dev/null
exit_is "corrupt output exits 10" 10 sh "$SWAP" "$T/g.mkv" "$T/g.out.mkv"
check "corrupt: original untouched" test -f "$T/g.mkv"

# 8. reject: output larger than the source (lossless hevc)
mksrc "$T/h.mkv"
"$FFMPEG" -v error -y -i "$T/h.mkv" -c:v libx265 -x265-params lossless=1:log-level=error -c:a copy "$T/h.out.mkv"
exit_is "larger output exits 10" 10 sh "$SWAP" "$T/h.mkv" "$T/h.out.mkv"
check "larger: original untouched" test -f "$T/h.mkv"

# 9. reject: zero-byte output
mksrc "$T/i.mkv"; : > "$T/i.out.mkv"
exit_is "empty output exits 10" 10 sh "$SWAP" "$T/i.mkv" "$T/i.out.mkv"

# 10. awkward names survive (review focus 5)
D="$T/it's a \$dir"; mkdir -p "$D"
mksrc "$D/-weird name \"1\".mkv"; mkout "$D/-weird name \"1\".mkv" "$D/out file.mkv"
exit_is "awkward names exit 0" 0 sh "$SWAP" "$D/-weird name \"1\".mkv" "$D/out file.mkv"
check "awkward: result is hevc" is_hevc "$D/-weird name \"1\".mkv"

# 11. refuse: original outside MEDIA_ROOT
O=$(mktemp -d)
mksrc "$O/j.mkv"; mkout "$O/j.mkv" "$O/j.out.mkv"
exit_is "outside MEDIA_ROOT exits 20" 20 sh "$SWAP" "$O/j.mkv" "$O/j.out.mkv"
rm -rf "$O"

# 12. reject: video frames dropped but audio intact, so the container duration still matches
mksrc "$T/k.mkv"
"$FFMPEG" -v error -y -i "$T/k.mkv" -vf "select='not(between(t,2,3))',setpts=N/24/TB" -fps_mode passthrough \
  -c:v libx265 -pix_fmt yuv420p10le -x265-params log-level=error -crf 30 -c:a copy "$T/k.out.mkv"
says "dropped frames rejected" "packets" sh "$SWAP" "$T/k.mkv" "$T/k.out.mkv"
check "dropped: original untouched" test -f "$T/k.mkv"

# 13. reject: same frame count, but a 0.8 s hole in the timestamps (a visible freeze)
mksrc "$T/l.mkv"
"$FFMPEG" -v error -y -i "$T/l.mkv" -vf "setpts='if(gte(N,72),PTS+0.8/TB,PTS)'" -fps_mode passthrough \
  -c:v libx265 -pix_fmt yuv420p10le -x265-params log-level=error -crf 30 -c:a copy "$T/l.out.mkv"
says "timestamp gap rejected" "gap" sh "$SWAP" "$T/l.mkv" "$T/l.out.mkv"

# 14. reject: audio shifted against video
mksrc "$T/m.mkv"
"$FFMPEG" -v error -y -i "$T/m.mkv" -itsoffset 0.6 -i "$T/m.mkv" -map 0:v -map 1:a \
  -c:v libx265 -pix_fmt yuv420p10le -x265-params log-level=error -crf 30 -c:a copy "$T/m.out.mkv"
says "A/V offset rejected" "out of sync" sh "$SWAP" "$T/m.mkv" "$T/m.out.mkv"

# 15. reject: wrong picture (heavy blur, everything else identical)
mksrc "$T/n.mkv"; mkout "$T/n.mkv" "$T/n.out.mkv" -vf gblur=sigma=12
says "wrong picture rejected" "SSIM" sh "$SWAP" "$T/n.mkv" "$T/n.out.mkv"

# 16. reject: a profile a TV will not direct-play (4:4:4)
mksrc "$T/o.mkv"; mkout "$T/o.mkv" "$T/o.out.mkv" -pix_fmt yuv444p10le
says "4:4:4 profile rejected" "profile" sh "$SWAP" "$T/o.mkv" "$T/o.out.mkv"

# 17. VERIFY_ONLY checks without moving anything
mksrc "$T/p.mkv"; mkout "$T/p.mkv" "$T/p.out.mkv"
exit_is "verify-only passes" 0 env VERIFY_ONLY=1 sh "$SWAP" "$T/p.mkv" "$T/p.out.mkv"
check "verify-only: nothing moved" test -f "$T/p.out.mkv" -a -f "$T/p.mkv"

# 18. usage
exit_is "no args exits 2" 2 sh "$SWAP"

rm -rf "$T" "$TRASH_DIR/run-$$"
if [ "$fail" -eq 0 ]; then echo "ALL PASSED"; else echo "SOME FAILED"; exit 1; fi
