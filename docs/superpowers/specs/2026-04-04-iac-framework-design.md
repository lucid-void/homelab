---
title: IaC Framework Scaffold — Design
date: 2026-04-04
status: approved
---

# IaC Framework Scaffold

## Scope

Create the folder skeleton, top-level config files, README, and gitignored action plan for the homelab IaC repository. No implementation (Ansible roles, Terraform modules, Compose stacks) — that is left to the operator.

## Deliverables

| File | Status | Notes |
|---|---|---|
| Directory skeleton (`infra/`, `stacks/`, `.gitea/`) | scaffold | `.gitkeep` in leaf dirs |
| `justfile` | filled in | All targets from `iac-pipeline.md` |
| `.sops.yaml` | filled in | Placeholders for SSH + recovery keys |
| `.gitignore` update | filled in | Fix SOPS lines; add PLAN.md, tfplan, .terraform/ |
| `README.md` | filled in | Prerequisites, SOPS bootstrap, justfile reference |
| `PLAN.md` | filled in, gitignored | Phased implementation checklist for operator |

## Directory structure

```
infra/
├── packer/
├── terraform/
└── ansible/
    ├── inventory/
    ├── group_vars/all/
    ├── roles/
    │   ├── common/
    │   ├── docker/
    │   ├── truenas/
    │   ├── proxmox/
    │   └── pbs/
    └── playbooks/
stacks/
.gitea/workflows/
```

## Key decisions

- SOPS-encrypted files (`*.sops.yml`, `*.sops.tfvars`) **are committed** — they are encrypted and that is the intended workflow. The existing gitignore incorrectly ignored them; fixed.
- `*.sops.tfvars.dec` (sops temp decrypts) are gitignored.
- `*.tfplan` and `infra/terraform/.terraform/` are gitignored.
- `PLAN.md` is gitignored — it is the operator's personal implementation checklist.
- justfile uses `tofu plan -out=tfplan` + `tofu apply tfplan` (not bare apply) so the applied plan always matches the reviewed plan.
