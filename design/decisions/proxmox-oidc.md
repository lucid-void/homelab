# Proxmox OIDC — decisions

Extracted verbatim from the `.claude/CLAUDE.md` key-decisions table. Do not
re-litigate without reason.

## Bare-metal OIDC wiring

Proxmox VE is **bare metal (172.16.20.3), not a k8s workload** — the Zitadel app +
`proxmox-oidc-secret` are still provisioned by `zitadel-bootstrap` Terraform, but the
secret lands in the **`auth` namespace** (no consumer pod; it's a retrieval mechanism).
Cross-ns RBAC role `zitadel-bootstrap-auth` in `bootstrap-rbac`. Redirect URI = Proxmox
web UI **base URL, no path** (`https://pve.blackcats.cc:8006` + `:443`);
`auth_method_type = BASIC` (proxmox-openid Rust crate uses `client_secret_basic`).
Credentials are entered into a Proxmox OIDC realm manually via `pveum` (see RUNBOOK)
— **never front Proxmox behind the cluster Gateway** (circular dependency: Gateway
runs on VMs this host hypervises).

