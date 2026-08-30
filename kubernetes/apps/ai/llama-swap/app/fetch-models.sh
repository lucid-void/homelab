#!/bin/bash
# Downloads GGUF weights onto the llama-models PVC. Idempotent: a file already
# present at its exact expected size is skipped, so a re-run after a partial
# failure only fetches what is missing.
#
# Runs on llm-1 because openebs-hostpath is node-local — this Job must write the
# same directory the llama-swap Deployment reads.
set -euo pipefail

MODELS_DIR=/models

# repo|filename|exact size in bytes
#
# Sizes are pinned deliberately. A truncated or redirected download otherwise
# leaves a plausible-looking file that llama.cpp rejects much later with an
# opaque GGUF parse error, at which point the cause is hours behind you.
#
# Qwen3.6-35B-A3B-Q8_0 is the chat/coding model — note the name has NO `UD-`
# prefix. `UD-Q8_0` does not exist in this repo; the unsloth-dynamic variant at
# this tier is `UD-Q8_K_XL.gguf` (38.4 GB), which is a valid alternative but not
# what is deployed. Getting this wrong 404s after the download has already run.
MODELS="
unsloth/Qwen3.6-35B-A3B-GGUF|Qwen3.6-35B-A3B-Q8_0.gguf|36903140320
nomic-ai/nomic-embed-text-v1.5-GGUF|nomic-embed-text-v1.5.Q8_0.gguf|146146432
"

fetch() {
  local repo="$1" file="$2" want="$3"
  local dest="${MODELS_DIR}/${file}"
  local part="${dest}.part"
  local url="https://huggingface.co/${repo}/resolve/main/${file}"

  if [ -f "$dest" ]; then
    local have
    have=$(stat -c %s "$dest")
    if [ "$have" = "$want" ]; then
      echo "== ${file}: present at ${have} bytes, skipping"
      return 0
    fi
    echo "!! ${file}: size ${have} != expected ${want}, refetching"
    rm -f "$dest"
  fi

  echo "== ${file}: fetching $(( want / 1024 / 1024 )) MiB from ${repo}"
  # -C - resumes an interrupted .part rather than restarting 34 GiB from zero.
  # --retry covers transient CDN errors; --fail turns an HTML error page into a
  # non-zero exit instead of a file full of HTML.
  curl -fL --retry 5 --retry-delay 10 --retry-connrefused \
    -C - -o "$part" "$url"

  local got
  got=$(stat -c %s "$part")
  if [ "$got" != "$want" ]; then
    echo "!! ${file}: downloaded ${got} bytes, expected ${want}" >&2
    exit 1
  fi

  # Rename only after the size checks out, so an interrupted run never leaves a
  # short file under the real name for llama.cpp to choke on.
  mv "$part" "$dest"
  chmod 0644 "$dest"
  echo "== ${file}: done"
}

echo "== free space before:"
df -h "$MODELS_DIR" | tail -1

# Herestring, not `echo | while`: a piped while runs in a subshell, so a failure
# inside fetch() would abort only that subshell and the exit status would depend
# on pipefail subtleties. This keeps the loop in the main shell where `set -e`
# means what it says.
while IFS='|' read -r repo file size; do
  [ -z "$repo" ] && continue
  fetch "$repo" "$file" "$size"
done <<< "$MODELS"

echo "== free space after:"
df -h "$MODELS_DIR" | tail -1
ls -la "$MODELS_DIR"
echo "== all models present"
