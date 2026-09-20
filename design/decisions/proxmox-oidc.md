# Proxmox OIDC

**Read before editing:** `kubernetes/apps/keycloak/clients/`, `kubernetes/apps/auth/`

## Current state

Proxmox VE is bare metal (`172.16.20.3`), not a k8s workload. Its OIDC client is a
`KeycloakOIDCClient` CR like every other app, and `auth/proxmox-oidc-secret` is now a
SealedSecret carrying `ISSUER_URL`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`. It
lands in the `auth` namespace with no consumer pod — it is a retrieval mechanism only,
and the one reason `auth` outlives Zitadel's retirement unless it is moved.
Redirect URI is the Proxmox web UI base URL with no path
(`https://pve.blackcats.cc:8006` + `:443`); `auth_method_type = BASIC` (the
`proxmox-openid` Rust crate uses `client_secret_basic`). Credentials are entered into a
Proxmox OIDC realm manually via `pveum` (see design/runbook.md).

## Rules

- **Never front Proxmox behind the cluster Gateway** — the Gateway runs on VMs that
  this host hypervises, so routing Proxmox's own management UI through it is circular:
  a Gateway outage would take down the only way to reach the hypervisor that runs the
  Gateway.
