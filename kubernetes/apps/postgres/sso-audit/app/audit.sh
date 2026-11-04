#!/usr/bin/env bash
# Compares Keycloak with what each app actually does, and reports any difference to
# Gotify. Read-only: it connects to the replica and changes nothing.
#
# It reads Keycloak's tables directly because they answer without an admin credential.
# They are not a stable interface, so a schema change after an upgrade makes a query
# fail, and the ERR trap below reports that rather than passing silently.
set -euo pipefail

GOTIFY_URL="http://gotify.monitoring.svc.cluster.local/message"

notify() {
  local title="$1" message="$2" priority="$3"
  if [ -z "${GOTIFY_TOKEN:-}" ]; then
    echo "WARN: no GOTIFY_TOKEN; not notified" >&2
    return 0
  fi
  curl -sf -X POST "$GOTIFY_URL" \
    -H "X-Gotify-Key: ${GOTIFY_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg t "$title" --arg m "$message" --argjson p "$priority" \
          '{title: $t, message: $m, priority: $p}')" >/dev/null \
    || echo "WARN: Gotify notification failed" >&2
}

trap 'notify "SSO audit failed" "A query failed at line ${LINENO}. Check the sso-audit Job logs." 8' ERR

q() {
  psql -X -A -t -F $'\t' -v ON_ERROR_STOP=1 -d "$1" -c "$2"
}

ISSUES=()

# NULL is coalesced to '-' because bash collapses consecutive tabs when splitting.
gates=$(q keycloak "
  WITH realm_id AS (SELECT id FROM realm WHERE name = 'homelab')
  SELECT c.client_id,
         COALESCE(b.flow_id, '-'),
         COALESCE((SELECT e.priority::text FROM authentication_execution e
                   JOIN authentication_flow s ON s.id = e.auth_flow_id
                   WHERE e.flow_id = b.flow_id AND s.alias = c.client_id || '-authenticate'), '-'),
         COALESCE((SELECT e.priority::text FROM authentication_execution e
                   JOIN authentication_flow s ON s.id = e.auth_flow_id
                   WHERE e.flow_id = b.flow_id AND s.alias = c.client_id || '-gate'), '-'),
         COALESCE((SELECT v.value FROM authenticator_config ac
                   JOIN authenticator_config_entry v ON v.authenticator_id = ac.id
                   WHERE ac.realm_id = c.realm_id AND ac.alias = c.client_id || '-entitlement'
                     AND v.name = 'condUserRole'), '-'),
         COALESCE((SELECT v.value FROM authenticator_config ac
                   JOIN authenticator_config_entry v ON v.authenticator_id = ac.id
                   WHERE ac.realm_id = c.realm_id AND ac.alias = c.client_id || '-entitlement'
                     AND v.name = 'negate'), '-')
  FROM client c
  LEFT JOIN client_auth_flow_bindings b ON b.client_id = c.id AND b.binding_name = 'browser'
  WHERE c.realm_id = (SELECT id FROM realm_id)
    AND c.client_id NOT IN ('account', 'account-console', 'admin-cli', 'broker',
                            'realm-management', 'security-admin-console')
  ORDER BY 1")

while IFS=$'\t' read -r client flow auth_priority gate_priority role negate; do
  [ -n "$client" ] || continue
  if [ "$flow" = "-" ]; then
    ISSUES+=("${client}: no login gate, so any realm user can log in")
    continue
  fi
  if [ "$auth_priority" = "-" ] || [ "$gate_priority" = "-" ] \
    || [ "$auth_priority" -ge "$gate_priority" ]; then
    ISSUES+=("${client}: login gate order is not guaranteed (login ${auth_priority}, gate ${gate_priority})")
  fi
  if [ "$negate" != "true" ] || [[ "$role" != "${client}."* ]]; then
    ISSUES+=("${client}: login gate checks the wrong thing (role ${role}, negate ${negate})")
  fi
done <<<"$gates"

claims=$(q keycloak "
  SELECT v.value FROM protocol_mapper pm
  JOIN protocol_mapper_config v ON v.protocol_mapper_id = pm.id
  JOIN client c ON c.id = pm.client_id
  JOIN realm r ON r.id = c.realm_id
  WHERE r.name = 'homelab' AND v.name = 'claim.name'")

for client in gitea grafana immich jellyfin kavita openwebui paperless proxmox romm; do
  grep -qx "${client}_roles" <<<"$claims" \
    || ISSUES+=("${client}: no ${client}_roles mapper, so the app cannot see its role")
done

realm_users=$(q keycloak "
  SELECT lower(u.email) FROM user_entity u
  JOIN realm r ON r.id = u.realm_id
  WHERE r.name = 'homelab' AND u.email IS NOT NULL AND u.service_account_client_link IS NULL")

keycloak_admins=$(q keycloak "
  SELECT DISTINCT c.client_id, lower(u.email)
  FROM user_entity u
  JOIN realm r ON r.id = u.realm_id
  JOIN (
    SELECT m.user_id, gm.role_id
    FROM user_group_membership m
    JOIN keycloak_group g ON g.id = m.group_id
    JOIN group_role_mapping gm ON gm.group_id IN (g.id, g.parent_group)
    UNION
    SELECT user_id, role_id FROM user_role_mapping
  ) held ON held.user_id = u.id
  JOIN keycloak_role kr ON kr.id = held.role_id AND kr.name = 'admin'
  JOIN client c ON c.id = kr.client
  WHERE r.name = 'homelab' AND u.email IS NOT NULL")

compare_app() {
  local app="$1" rows="$2" email is_admin expected
  while IFS=$'\t' read -r email is_admin; do
    [ -n "$email" ] || continue
    # A local account, such as a break-glass admin, is not Keycloak's to manage.
    grep -qxF "$email" <<<"$realm_users" || continue
    expected=f
    grep -qxF "${app}"$'\t'"${email}" <<<"$keycloak_admins" && expected=t
    if [ "$expected" = t ] && [ "$is_admin" != t ]; then
      ISSUES+=("${app}: ${email} has admin in Keycloak but not in the app, until their next login")
    elif [ "$expected" = f ] && [ "$is_admin" = t ]; then
      ISSUES+=("${app}: ${email} is admin in the app but not in Keycloak, until their next login")
    fi
  done <<<"$rows"
}

compare_app gitea "$(q gitea \
  "SELECT lower(email), is_admin FROM \"user\" WHERE type = 0 AND login_type = 6")"
compare_app immich "$(q immich \
  "SELECT lower(email), \"isAdmin\" FROM \"user\" WHERE \"deletedAt\" IS NULL AND \"oauthId\" <> ''")"
compare_app openwebui "$(q openwebui \
  "SELECT lower(email), role = 'admin' FROM \"user\" WHERE oauth IS NOT NULL")"
compare_app paperless "$(q paperless \
  "SELECT lower(u.email), u.is_superuser FROM auth_user u
   JOIN socialaccount_socialaccount s ON s.user_id = u.id")"
compare_app romm "$(q romm \
  "SELECT lower(email), role::text = 'admin' FROM users WHERE email IS NOT NULL")"

pending=$(q openwebui "SELECT lower(email) FROM \"user\" WHERE role = 'pending'")
while IFS= read -r email; do
  [ -n "$email" ] && ISSUES+=("openwebui: ${email} is stuck at pending")
done <<<"$pending"

if [ "${#ISSUES[@]}" -eq 0 ]; then
  echo "No mismatches."
  exit 0
fi

echo "Found ${#ISSUES[@]} mismatch(es):"
printf '  %s\n' "${ISSUES[@]}"
notify "SSO audit: ${#ISSUES[@]} mismatch(es)" "$(printf '• %s\n' "${ISSUES[@]}")" 5
