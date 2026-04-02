---
tags:
  - automation
  - gitea
  - ci-cd
  - git
---

# CI/CD — Gitea

Local git server and CI/CD runtime for homelab infrastructure pipelines. GitHub remains the primary remote and source of truth for GitHub Pages. Gitea mirrors the repository on a schedule and runs pipelines on every sync — keeping all homelab automation on the homelab network where it has direct access to all infrastructure.

### Mirror & CI Flow

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant GH as GitHub
    participant GHA as GitHub Actions
    participant GT as Gitea (.13)
    participant LXC as Runner LXC (.17)
    participant Infra as Homelab Infra

    Dev->>GH: git push
    GH->>GHA: Trigger docs build
    GHA-->>GH: Publish to GitHub Pages

    Note over GT: Every 10 minutes
    GT->>GH: Mirror pull (read-only)
    GT->>LXC: Trigger Gitea Actions
    LXC->>LXC: lint (packer, tflint, ansible-lint)
    LXC->>Infra: tofu plan (manual dispatch only)
    LXC->>Infra: ansible-playbook (manual dispatch only)
```

## Components

| Component | Where | Detail |
|---|---|---|
| Gitea app | Services VM (.13) — Swarm service | `gitea.blackcats.cc` via Traefik · TLS auto |
| Gitea data | `tank/media/gitea` NFS -> `/mnt/media/gitea` | Application data — repos, attachments, LFS |
| Gitea DB | Shared Postgres on TrueNAS (.2) | Dedicated `gitea` database, TCP to `172.16.20.2` |
| Mirror | Gitea built-in | Polls GitHub every 10 min · read-only · GitHub stays primary |
| Actions runner | Dedicated Debian LXC at `.17` | `act_runner` binary — not a Swarm service |

`tank/media/gitea` follows the NFS-export tier pattern: `recordsize=128K · compression=zstd · atime=off`.

!!! info "Runner LXC access requirements"
    The runner LXC requires broad network access: SSH to all homelab hosts for Ansible, the Proxmox API for OpenTofu dynamic inventory, and TrueNAS MinIO for OpenTofu state. The SOPS age private key is deployed to the runner by Ansible, allowing it to decrypt secrets at pipeline runtime.

## Pipelines

Workflow files live in `.gitea/workflows/` (GitHub Actions-compatible syntax).

| Trigger | Job | What it does |
|---|---|---|
| Push to `main` | `lint` | `packer validate`, `tflint`, `ansible-lint` |
| Manual dispatch | `plan` | `tofu plan -out=tfplan` — output reviewed before any apply |
| Manual dispatch | `configure` | `ansible-playbook site.yml` — full configuration run |

!!! danger "Apply is never automated"
    `tofu apply` is always run manually after a plan has been reviewed. This matches the `just plan -> just show -> just apply` discipline and guards against accidental destroys in CI.
