# Proxmox OIDC

**Read before editing:** `kubernetes/apps/auth/`, `infra/terraform/`

## Current state

Proxmox VE is bare metal (`172.16.20.3`), not a k8s workload. The Zitadel app and
`proxmox-oidc-secret` are still provisioned by `zitadel-bootstrap` Terraform, but the
secret lands in the `auth` namespace with no consumer pod — it is a retrieval
mechanism only. Cross-namespace RBAC role `zitadel-bootstrap-auth` lives in
`bootstrap-rbac`. Redirect URI is the Proxmox web UI base URL with no path
(`https://pve.blackcats.cc:8006` + `:443`); `auth_method_type = BASIC` (the
`proxmox-openid` Rust crate uses `client_secret_basic`). Credentials are entered into a
Proxmox OIDC realm manually via `pveum` (see design/runbook.md).

## Rules

- **Never front Proxmox behind the cluster Gateway** — the Gateway runs on VMs that
  this host hypervises, so routing Proxmox's own management UI through it is circular:
  a Gateway outage would take down the only way to reach the hypervisor that runs the
  Gateway.
