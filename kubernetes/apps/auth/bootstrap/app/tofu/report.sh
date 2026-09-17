#!/usr/bin/env bash
# Classifies what the tofu initContainer just did and says so out loud.
#
# A tofu apply that changes nothing is the normal case. A tofu apply that
# *changes something on an unchanged config* means an OIDC app was deleted or
# Zitadel's database was reset — and because main.tf owns every client_id and
# client_secret in the cluster, that silently reissues credentials for eight
# applications and rewrites seven Secrets across six namespaces. Consumers with
# a Reloader annotation restart onto the new values; the rest keep authenticating
# with credentials that no longer exist.
#
# Until now that was invisible: ttlSecondsAfterFinished deletes this pod an hour
# later, so the Gotify message is the durable record, not this output.
set -euo pipefail

STATE_NS="auth"
STATE_CM="zitadel-bootstrap-state"
WORK="/work"
GOTIFY_URL="http://gotify.monitoring.svc.cluster.local"

PLAN="${WORK}/plan.json"
if [ ! -s "$PLAN" ]; then
  echo "FATAL: ${PLAN} missing or empty — the tofu initContainer did not produce a plan" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Run classification
#
# Same rule as gotify-bootstrap: a change is only drift if we did not just ask
# for it. The desired state lives entirely in these three files, so any edit to
# them moves the hash and the resulting changes are the edit landing, not drift.
# The lock file is included because a provider bump can legitimately re-plan.
# ---------------------------------------------------------------------------
CONFIG_HASH="$(cat /tf-config/main.tf /tf-config/run.sh /tf-config/.terraform.lock.hcl \
  | sha256sum | cut -c1-16)"
LAST_HASH="$(kubectl get configmap "$STATE_CM" -n "$STATE_NS" \
  -o jsonpath='{.data.configHash}' 2>/dev/null || true)"

if [ -z "$LAST_HASH" ]; then
  RUN_KIND="initial"          # first run ever, or after a state wipe
elif [ "$LAST_HASH" != "$CONFIG_HASH" ]; then
  RUN_KIND="changed"          # main.tf/run.sh/lock was edited; changes are expected
else
  RUN_KIND="steady"           # scheduled re-run of unchanged config
fi
echo "Run kind: ${RUN_KIND} (config ${CONFIG_HASH}, last seen ${LAST_HASH:-none})"

# Addresses and actions only. The JSON plan carries the real client secrets under
# .change.after, and everything below is printed to stdout and posted to Gotify.
# "no-op" is the steady state; "read" is a data source refresh, not a change.
mapfile -t CHANGES < <(jq -r '
  .resource_changes[]?
  | select(.change.actions != ["no-op"] and .change.actions != ["read"])
  | "\(.change.actions | join("+")) \(.address)"' "$PLAN")

notify_gotify() {
  local title="$1" msg="$2" prio="$3"
  # Always the in-cluster Service, never zitadel/gotify public hostnames: the
  # external path needs DNS + egress out to the Gateway and back, which is
  # exactly what is broken in the failure modes worth reporting.
  if [ -z "${GOTIFY_TOKEN:-}" ]; then
    echo "WARN: no GOTIFY_TOKEN (auth/gotify-secret absent); drift not notified" >&2
    return 0
  fi
  curl -sf -X POST "${GOTIFY_URL}/message" \
    -H "X-Gotify-Key: ${GOTIFY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg t "$title" --arg m "$msg" --argjson p "$prio" \
          '{title: $t, message: $m, priority: $p}')" >/dev/null \
    || echo "WARN: drift notification to Gotify failed" >&2
}

# Nothing here fails the Job: tofu already applied every change before this ran.
# The point is to say that it had to.
if [ "${#CHANGES[@]}" -eq 0 ]; then
  echo "No drift: Zitadel matched the declared configuration."
elif [ "$RUN_KIND" != "steady" ]; then
  echo "Changes below are expected for a '${RUN_KIND}' run, not reported as drift:"
  printf '  %s\n' "${CHANGES[@]}"
else
  echo "DRIFT DETECTED (${#CHANGES[@]}) — applied, but something changed Zitadel out of band:"
  printf '  DRIFT: %s\n' "${CHANGES[@]}"
  notify_gotify "⚠ Zitadel OIDC drift repaired" \
    "$(printf 'zitadel-bootstrap re-applied %s resource(s) against an unchanged config:\n\n' "${#CHANGES[@]}"
       printf '• %s\n' "${CHANGES[@]}"
       printf '\nAny recreated application has a NEW client_id/client_secret. Consumers\n'
       printf 'without a Reloader annotation are still using the old ones — check the\n'
       printf 'affected apps can still log in via Zitadel.\n')" \
    8
fi

kubectl create configmap "$STATE_CM" -n "$STATE_NS" \
  --from-literal=configHash="$CONFIG_HASH" \
  --from-literal=lastRun="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --from-literal=lastRunKind="$RUN_KIND" \
  --from-literal=lastRunChanges="${#CHANGES[@]}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "Report complete."
