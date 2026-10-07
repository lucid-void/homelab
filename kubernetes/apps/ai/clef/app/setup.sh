#!/bin/sh
# Installs the pinned Python dependencies and the pinned Clef-flash weights onto the PVC.
# Idempotent: a re-run with nothing changed does no network work.
set -eu

VENV=/data/venv
WANT="$(cat /scripts/requirements-torch.txt /scripts/requirements.txt | sha256sum | cut -d' ' -f1)"
HAVE="$(cat "$VENV/.installed" 2>/dev/null || true)"

if [ "$HAVE" != "$WANT" ]; then
  echo "== installing python dependencies"
  rm -rf "$VENV"
  python -m venv "$VENV"
  # timeout on every network step: an unbounded install wedges the pod Running with empty logs.
  # torch and torchvision come from the CPU wheel index (the PyPI build is the CUDA one, ~2 GB larger).
  timeout 1800 "$VENV/bin/pip" install --no-cache-dir --timeout 30 \
    --index-url https://download.pytorch.org/whl/cpu -r /scripts/requirements-torch.txt
  timeout 900 "$VENV/bin/pip" install --no-cache-dir --timeout 30 -r /scripts/requirements.txt
  echo "$WANT" > "$VENV/.installed"
else
  echo "== python dependencies up to date"
fi

SNAP="/data/hf/hub/models--Cloudflare--clef-flash/snapshots/${CLEF_REVISION}"
if [ ! -f "$SNAP/.complete" ]; then
  echo "== fetching Cloudflare/clef-flash @ ${CLEF_REVISION} (about 19 GB)"
  timeout 3600 "$VENV/bin/python" -c "
import os
from huggingface_hub import snapshot_download
snapshot_download('Cloudflare/clef-flash', revision=os.environ['CLEF_REVISION'])"
  touch "$SNAP/.complete"
else
  echo "== weights present"
fi
echo "== ready"
