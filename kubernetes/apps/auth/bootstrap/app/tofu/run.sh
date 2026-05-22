#!/usr/bin/env bash
set -euo pipefail

# Copy tf files to a writable workspace (ConfigMap mounts are read-only)
cp /tf-config/*.tf /workspace/
cd /workspace

# Wait for Zitadel readiness via the internal service (avoids external DNS dependency)
echo "Waiting for Zitadel at http://zitadel.auth.svc.cluster.local:8080/debug/healthz ..."
until wget -qO- --timeout=5 http://zitadel.auth.svc.cluster.local:8080/debug/healthz > /dev/null 2>&1; do
  echo "  not ready, retrying in 10s..."
  sleep 10
done
echo "Zitadel is ready."

tofu init -input=false
tofu apply -input=false -auto-approve

echo "Bootstrap complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
