#!/usr/bin/env sh
# Applies the Zitadel OIDC/SAML configuration in main.tf and leaves a machine-
# readable record of what it had to change behind in /work for the report
# container to classify. Runs as the Job's initContainer, so a failure here stops
# the pod before anything is reported.
#
# Delivered as a kustomize configMapGenerator, so editing this file changes the
# generated ConfigMap's name hash -> the Job's pod spec changes -> the Flux
# Kustomization (force: true) recreates the Job and it re-runs at once.
set -eu

WORK=/work

# ConfigMap mounts are read-only and `tofu init` writes .terraform/ into the
# working directory, so everything is copied to the workspace emptyDir first.
# The lock file is a dotfile and is NOT matched by *.tf — copy it by name.
cp /tf-config/main.tf /tf-config/.terraform.lock.hcl /workspace/
cd /workspace

# Wait for Zitadel readiness via the internal service (avoids external DNS dependency).
# Unbounded on purpose — activeDeadlineSeconds on the Job is what bounds it.
echo "Waiting for Zitadel at http://zitadel.auth.svc.cluster.local:8080/debug/healthz ..."
until wget -qO- --timeout=5 http://zitadel.auth.svc.cluster.local:8080/debug/healthz > /dev/null 2>&1; do
  echo "  not ready, retrying in 10s..."
  sleep 10
done
echo "Zitadel is ready."

# -lockfile=readonly pins both providers to the exact versions and checksums in
# .terraform.lock.hcl. Without it `~> 3.0` re-resolves on every run — 24 registry
# fetches a day, any of which could silently pull a new minor into the cluster's
# identity provider. A provider that no longer matches the recorded hash now fails
# the init instead of installing.
tofu init -input=false -lockfile=readonly

# Plan to a file, then apply that exact plan. Splitting the two is what makes
# drift observable: the plan says what was wrong *before* the repair, which is
# unrecoverable once apply has run.
tofu plan -input=false -out=tfplan

# The JSON plan is parsed by report.sh (which has jq; this image does not).
# NOTE: it contains the real client secrets under .resource_changes[].change.after.
# It never leaves this pod's emptyDir, and report.sh reads only addresses and
# actions — never values, which would land in pod logs and the Gotify message.
tofu show -json tfplan > "${WORK}/plan.json"

tofu apply -input=false -auto-approve tfplan

echo "Apply complete: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
