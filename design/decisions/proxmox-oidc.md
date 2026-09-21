# Proxmox OIDC

**Read before editing:** `kubernetes/apps/keycloak/clients/`

## Current state

Proxmox VE is bare metal (`172.16.20.3`), not a k8s workload. Its OIDC client is a
`KeycloakOIDCClient` CR like every other app, and its secret is the ordinary sealed
`keycloak/proxmox-client-secret` — nothing in-cluster consumes it, so retrieving it by
hand is the whole point. Until 2026-09-21 a second copy lived in `auth` as a
three-key `proxmox-oidc-secret` bundle; it was dropped with Zitadel rather than moved,
because it duplicated a credential that already sits beside every other client. The
other two keys are fixed facts, not secrets: the issuer is
`https://sso.blackcats.cc/realms/homelab` and the client id is `proxmox` (the CRD takes
it from `metadata.name`).
Redirect URI is the Proxmox web UI base URL with no path
(`https://pve.blackcats.cc:8006` + `:443`); `auth_method_type = BASIC` (the
`proxmox-openid` Rust crate uses `client_secret_basic`). Credentials are entered into a
Proxmox OIDC realm manually via `pveum` (see design/runbook.md).

## Rules

- **Never front Proxmox behind the cluster Gateway** — the Gateway runs on VMs that
  this host hypervises, so routing Proxmox's own management UI through it is circular:
  a Gateway outage would take down the only way to reach the hypervisor that runs the
  Gateway.
