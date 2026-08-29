#!/usr/bin/env bash
# Provisions one Gotify application token per notifying workload and writes each
# into a Secret in the consuming namespace. Idempotent, and re-run on a schedule
# (see job.yml) so lost tokens self-heal instead of failing silently at 03:00.
#
# This script is delivered as a kustomize configMapGenerator, so editing it
# changes the generated ConfigMap's name hash -> the Job's pod spec changes ->
# the Flux Kustomization (force: true) recreates the Job and it re-runs at once.
set -euo pipefail

GOTIFY_URL="http://gotify.monitoring.svc.cluster.local"
STATE_NS="monitoring"
STATE_CM="gotify-bootstrap-state"
SELF_SECRET="gotify-bootstrap-secret"

# Every non-reuse outcome lands here: an app that had to be created, a token that
# had to be rotated, a client that had to be rebuilt.
DRIFT=()

# ---------------------------------------------------------------------------
# Run classification
#
# A run that finds drift means something broke on its own: Gotify's DB was
# reset, or a Secret was deleted. A run that finds drift *because we just added
# an entry below* is not drift, it is the change landing. The two are told apart
# by whether this script's content matches what the last run recorded — the
# token list lives in this file, so any change to it moves the hash.
# ---------------------------------------------------------------------------
SCRIPT_HASH="$(sha256sum "${BASH_SOURCE[0]}" | cut -c1-16)"
LAST_HASH="$(kubectl get configmap "$STATE_CM" -n "$STATE_NS" \
  -o jsonpath='{.data.scriptHash}' 2>/dev/null || true)"

if [ -z "$LAST_HASH" ]; then
  RUN_KIND="initial"          # first run ever, or after a state wipe
elif [ "$LAST_HASH" != "$SCRIPT_HASH" ]; then
  RUN_KIND="changed"          # this file was edited; changes below are expected
else
  RUN_KIND="steady"           # scheduled re-run of unchanged config
fi
echo "Run kind: ${RUN_KIND} (script ${SCRIPT_HASH}, last seen ${LAST_HASH:-none})"

echo "Waiting for Gotify..."
until curl -sf "${GOTIFY_URL}/health" >/dev/null 2>&1; do sleep 5; done
echo "Gotify is ready."

# Fail fast on bad admin credentials. Otherwise every authenticated call 401s and
# each token silently resolves to empty — the failure mode that knocked out all
# notifications for days.
if ! curl -sf -u "admin:${GOTIFY_ADMIN_PASS}" "${GOTIFY_URL}/current/user" >/dev/null; then
  echo "FATAL: admin auth to Gotify failed — gotify-admin-secret does not match the Gotify admin password" >&2
  exit 1
fi
echo "Admin auth OK."

upsert_secret() {
  local ns="$1" name="$2" key="$3" value="$4"
  kubectl create secret generic "$name" \
    --namespace="$ns" \
    --from-literal="${key}=${value}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

# Reads a token back out of the destination Secret. Empty if the Secret or key
# does not exist yet. `prefix` strips the non-token part of composite values
# (see flux-gotify below).
stored_token() {
  local ns="$1" name="$2" key="$3" prefix="$4" raw
  raw=$(kubectl get secret "$name" -n "$ns" -o "jsonpath={.data.${key}}" 2>/dev/null \
    | base64 -d 2>/dev/null) || raw=""
  printf '%s' "${raw#"$prefix"}"
}

# Gotify 3 only ever discloses a token at creation or rotation — GET /application
# blanks the field (v2 returned it on every read). So the v2 "look it up each run"
# approach would no longer match an existing app and would create a duplicate
# application every run.
#
# Instead the destination Secret is the source of truth: if the app still exists
# in Gotify *and* we already hold its token, reuse it. Otherwise mint a new one —
# POST for a missing app, or PUT /application/{id}/security to rotate one whose
# token we lost. Rotation requires an elevated session; HTTP Basic auth satisfies
# that (auth.RequireElevatedClient accepts basic auth), so no /client/{id}/elevate
# dance is needed.
ensure_app_token() {
  local name="$1" desc="$2" ns="$3" secret="$4" key="$5" prefix="${6:-}"
  local id stored token

  id=$(curl -sf -u "admin:${GOTIFY_ADMIN_PASS}" "${GOTIFY_URL}/application" \
    | jq -r --arg n "$name" 'map(select(.name == $n)) | first | .id // empty') || true
  stored=$(stored_token "$ns" "$secret" "$key" "$prefix")

  if [ -n "$id" ] && [ -n "$stored" ]; then
    token="$stored"
    echo "  ${name}: reused (app ${id})"
  elif [ -n "$id" ]; then
    token=$(curl -sf -X PUT -u "admin:${GOTIFY_ADMIN_PASS}" \
      -H "Content-Type: application/json" \
      -d '{"regenerateToken":true}' \
      "${GOTIFY_URL}/application/${id}/security" \
      | jq -r '.regenerateToken.token // empty') || true
    echo "  ${name}: rotated (app ${id})"
    DRIFT+=("${name}: token rotated — ${ns}/${secret} was missing key ${key}")
  else
    token=$(curl -sf -X POST -u "admin:${GOTIFY_ADMIN_PASS}" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg n "$name" --arg d "$desc" '{name: $n, description: $d}')" \
      "${GOTIFY_URL}/application" | jq -r '.token // empty') || true
    echo "  ${name}: created"
    DRIFT+=("${name}: application created — it did not exist in Gotify")
  fi

  if [ -z "$token" ]; then
    echo "FATAL: could not obtain app token for '${name}'" >&2
    exit 1
  fi
  upsert_secret "$ns" "$secret" "$key" "${prefix}${token}"
}

# Same idea for the streaming client token, except Gotify 3 has no client
# rotation endpoint — PUT /client/{id} returns only the public token prefix.
# Losing a client token therefore means delete + recreate.
# `expiresAfterInactivitySeconds: 0` opts out of the new automatic inactive-client
# cleanup.
ensure_client_token() {
  local name="$1" ns="$2" secret="$3" key="$4"
  local id stored token

  id=$(curl -sf -u "admin:${GOTIFY_ADMIN_PASS}" "${GOTIFY_URL}/client" \
    | jq -r --arg n "$name" 'map(select(.name == $n)) | first | .id // empty') || true
  stored=$(stored_token "$ns" "$secret" "$key" "")

  if [ -n "$id" ] && [ -n "$stored" ]; then
    token="$stored"
    echo "  ${name} client: reused (client ${id})"
  else
    if [ -n "$id" ]; then
      curl -sf -X DELETE -u "admin:${GOTIFY_ADMIN_PASS}" \
        "${GOTIFY_URL}/client/${id}" >/dev/null || true
      echo "  ${name} client: token lost — recreating (was client ${id})"
      DRIFT+=("${name} client: recreated — ${ns}/${secret} was missing key ${key}")
    else
      echo "  ${name} client: created"
      DRIFT+=("${name} client: created — it did not exist in Gotify")
    fi
    token=$(curl -sf -X POST -u "admin:${GOTIFY_ADMIN_PASS}" \
      -H "Content-Type: application/json" \
      -d "$(jq -nc --arg n "$name" '{name: $n, expiresAfterInactivitySeconds: 0}')" \
      "${GOTIFY_URL}/client" | jq -r '.token // empty') || true
  fi

  if [ -z "$token" ]; then
    echo "FATAL: could not obtain client token for '${name}'" >&2
    exit 1
  fi
  upsert_secret "$ns" "$secret" "$key" "$token"
}

# Always the in-cluster Service, never gotify.blackcats.cc: the public hostname
# needs DNS + egress out to the Gateway and back, which is exactly what is broken
# in the failure modes worth reporting. Never silent on failure either — a
# swallowed notification is how a four-day outage stayed invisible.
notify_gotify() {
  local title="$1" msg="$2" prio="$3" token
  token=$(stored_token "$STATE_NS" "$SELF_SECRET" GOTIFY_TOKEN "")
  if [ -z "$token" ]; then
    echo "WARN: no self token in ${STATE_NS}/${SELF_SECRET}; drift not notified" >&2
    return 0
  fi
  curl -sf -X POST "${GOTIFY_URL}/message" \
    -H "X-Gotify-Key: ${token}" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg t "$title" --arg m "$msg" --argjson p "$prio" \
          '{title: $t, message: $m, priority: $p}')" >/dev/null \
    || echo "WARN: drift notification to Gotify failed" >&2
}

# Names only — never token values, these land in pod logs.
echo "Provisioning tokens:"

# First, so a drift report later in this same run has a token to send with.
ensure_app_token "gotify-bootstrap" "Gotify token provisioning drift"  monitoring  "$SELF_SECRET"       GOTIFY_TOKEN

ensure_app_token "etcd-snapshot"    "etcd snapshot backup notifications"    kube-system gotify-secret        GOTIFY_TOKEN
ensure_app_token "homebox-backup"   "Homebox backup job notifications"      homebox     gotify-secret        GOTIFY_TOKEN
ensure_app_token "postgres-backup"  "Postgres backup job notifications"     postgres    gotify-secret        GOTIFY_TOKEN
ensure_app_token "immich-backup"    "Immich backup job notifications"       immich      gotify-secret        GOTIFY_TOKEN
ensure_app_token "paperless-backup" "Paperless backup job notifications"    paperless   gotify-secret        GOTIFY_TOKEN
ensure_app_token "gitea-backup"     "Gitea backup job notifications"        gitea       gotify-secret        GOTIFY_TOKEN
ensure_app_token "joplin-backup"    "Joplin backup job notifications"       joplin      gotify-secret        GOTIFY_TOKEN
ensure_app_token "obsidian-backup"  "Obsidian CouchDB backup notifications" obsidian    gotify-secret        GOTIFY_TOKEN
ensure_app_token "minecraft-backup" "Minecraft backup job notifications"    media       gotify-secret        GOTIFY_TOKEN
ensure_app_token "minecraft-events" "Minecraft player join/leave events"    media       minecraft-events-gotify-secret GOTIFY_TOKEN
ensure_app_token "security-scanner" "Trivy weekly security report"          security    gotify-secret        GOTIFY_TOKEN
ensure_app_token "falco"            "Falco runtime security alerts"         security    falco-gotify-secret  GOTIFY_TOKEN
ensure_app_token "gatus"            "Gatus health monitoring alerts"        monitoring  gatus-gotify-secret  GOTIFY_TOKEN
ensure_app_token "alertmanager"     "AlertManager firing rule notifications" monitoring gotify-secret        GOTIFY_TOKEN

# Flux notification-controller Provider (flux-notifications) auths to Gotify with
# this header. Lives in `monitoring` (the Provider's own namespace — secretRef
# resolves there). Key is `headers` (not GOTIFY_TOKEN) because the generic webhook
# provider reads the token from an HTTP header, formatted as YAML `Header: value`.
ensure_app_token "flux" "Flux GitOps reconciliation failures" \
  monitoring flux-gotify headers "X-Gotify-Key: "

ensure_client_token "admin" monitoring gotify-client-secret CLIENT_TOKEN

# ---------------------------------------------------------------------------
# Drift report
#
# Nothing here fails the Job: every entry has already been repaired by the time
# it is printed. The point is to say so out loud, because the repair is otherwise
# indistinguishable from a normal run — and ttlSecondsAfterFinished deletes this
# pod's logs, so the Gotify message is the durable record, not this output.
# ---------------------------------------------------------------------------
if [ "${#DRIFT[@]}" -eq 0 ]; then
  echo "No drift: every token matched the stored state."
elif [ "$RUN_KIND" != "steady" ]; then
  echo "Changes below are expected for a '${RUN_KIND}' run, not reported as drift:"
  printf '  %s\n' "${DRIFT[@]}"
else
  echo "DRIFT DETECTED (${#DRIFT[@]}) — repaired, but something removed this state:"
  printf '  DRIFT: %s\n' "${DRIFT[@]}"
  notify_gotify "⚠ Gotify token drift repaired" \
    "$(printf 'gotify-bootstrap found %s token(s) missing on an unchanged config and re-provisioned them:\n\n' "${#DRIFT[@]}"
       printf '• %s\n' "${DRIFT[@]}"
       printf '\nUntil this run, the affected workloads were posting with a dead token.\n')" \
    8
fi

kubectl create configmap "$STATE_CM" -n "$STATE_NS" \
  --from-literal=scriptHash="$SCRIPT_HASH" \
  --from-literal=lastRun="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --from-literal=lastRunKind="$RUN_KIND" \
  --from-literal=lastRunDrift="${#DRIFT[@]}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "Bootstrap complete."
