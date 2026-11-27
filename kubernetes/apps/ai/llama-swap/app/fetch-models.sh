#!/bin/bash
# Downloads GGUF weights onto the llama-models PVC. Idempotent: a file already
# present at its exact expected size is skipped, so a re-run after a partial
# failure only fetches what is missing.
#
# Runs on llm-1 because openebs-hostpath is node-local — this Job must write the
# same directory the llama-swap Deployment reads.
set -euo pipefail

MODELS_DIR=/models

# repo|path in repo|exact size in bytes|local filename (optional)
#
# Sizes are pinned deliberately. A truncated or redirected download otherwise
# leaves a plausible-looking file that llama.cpp rejects much later with an
# opaque GGUF parse error, at which point the cause is hours behind you.
#
# Inkling 2 Mini comes from unsloth's `Mainline_Support/` folder, NOT the repo
# root. The root files use Unsloth's own looped layout, which mainline
# llama.cpp (what the llama-swap image ships) refuses. The mainline files carry a
# `.gguf_file` extension on purpose, so they are saved under a `.gguf` local name.
#
# The drafter MUST be the Mainline_Support one too. The root
# `dspark-Inkling-2-Mini-Q8_0.gguf` is in the older Unsloth format, and mainline
# rejects it at load with `tensor 'markov_w1.weight' has wrong shape`.
#
# Files dropped from this list are NOT deleted from the PVC. The old
# Qwen3.6-35B-A3B-Q8_0.gguf (34.4 GiB) stays on disk as the rollback until it is
# removed by hand.
MODELS="
unsloth/Inkling-2-Mini-GGUF|Mainline_Support/Inkling-2-Mini-UD-Q4_K_M.gguf_file|16610544608|Inkling-2-Mini-UD-Q4_K_M.gguf
unsloth/Inkling-2-Mini-GGUF|Mainline_Support/dspark-Inkling-2-Mini-Q8_0.gguf_file|503889952|dspark-Inkling-2-Mini-Q8_0.gguf
nomic-ai/nomic-embed-text-v1.5-GGUF|nomic-embed-text-v1.5.Q8_0.gguf|146146432
"

fetch() {
  local repo="$1" file="$2" want="$3" name="$${4:-$2}"
  local dest="$${MODELS_DIR}/$${name}"
  local part="$${dest}.part"
  local url="https://huggingface.co/$${repo}/resolve/main/$${file}"

  if [ -f "$dest" ]; then
    local have
    have=$(stat -c %s "$dest")
    if [ "$have" = "$want" ]; then
      echo "== $${file}: present at $${have} bytes, skipping"
      return 0
    fi
    echo "!! $${file}: size $${have} != expected $${want}, refetching"
    rm -f "$dest"
  fi

  echo "== $${file}: fetching $(( want / 1024 / 1024 )) MiB from $${repo}"
  # -C - resumes an interrupted .part rather than restarting 34 GiB from zero.
  # --retry covers transient CDN errors; --fail turns an HTML error page into a
  # non-zero exit instead of a file full of HTML.
  curl -fL --retry 5 --retry-delay 10 --retry-connrefused \
    -C - -o "$part" "$url"

  local got
  got=$(stat -c %s "$part")
  if [ "$got" != "$want" ]; then
    echo "!! $${file}: downloaded $${got} bytes, expected $${want}" >&2
    exit 1
  fi

  # Rename only after the size checks out, so an interrupted run never leaves a
  # short file under the real name for llama.cpp to choke on.
  mv "$part" "$dest"
  chmod 0644 "$dest"
  echo "== $${file}: done"
}

echo "== free space before:"
df -h "$MODELS_DIR" | tail -1

# Herestring, not `echo | while`: a piped while runs in a subshell, so a failure
# inside fetch() would abort only that subshell and the exit status would depend
# on pipefail subtleties. This keeps the loop in the main shell where `set -e`
# means what it says.
while IFS='|' read -r repo file size name; do
  [ -z "$repo" ] && continue
  fetch "$repo" "$file" "$size" "$name"
done <<< "$MODELS"

echo "== free space after:"
df -h "$MODELS_DIR" | tail -1
ls -la "$MODELS_DIR"
echo "== all models present"
