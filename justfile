# Homelab — Task Runner

set dotenv-load := false

_default:
    @just --list

# Initialize IAC backends
init:
    cd infra/packer && packer init config.pkr.hcl
    cd ..
    cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu init -upgrade'
    cd ..
    cd infra/ansible && ansible-galaxy collection install -r requirements.yml

# ── Packer ────────────────────────────────────────────────────────────────────

build-template:
    sops exec-file --input-type binary --filename tmp-file.hcl infra/packer/credentials.sops.pkr.hcl 'packer build --var-file={} infra/packer/Debian13/debian-base.pkr.hcl'

# Validate Packer template (no build)
validate-template:
    packer validate infra/packer/

# ── OpenTofu ──────────────────────────────────────────────────────────────────


# Plan — outputs tfplan for review before apply
plan:
    cd infra/terraform && sops exec-file --input-type binary --filename secrets.tfvars secrets.sops.tfvars 'tofu plan --var-file={} -out=tfplan'

# Show the last plan in human-readable form
show:
    cd infra/terraform && tofu show tfplan

# Apply the reviewed plan (never re-evaluates — applies exactly what was planned)
apply:
    cd infra/terraform && sops exec-file --input-type binary --filename secrets.tfvars secrets.sops.tfvars 'tofu apply --var-file={} tfplan'

# Destroy all managed resources (DANGEROUS — prompts for confirmation)
destroy:
    cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu destroy'

# ── Ansible ───────────────────────────────────────────────────────────────────

# Refresh the Proxmox dynamic inventory cache (needed after adding/removing VMs)
inventory-refresh:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-inventory --list > /dev/null

# Full configuration run — all hosts, all roles, deploy stacks
configure:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-playbook playbooks/site.yml

# Configure a single host
# Usage: just configure-host host=services
configure-host host:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-playbook playbooks/site.yml --limit {{ host }}

# Configure VMs only (skip physical hosts)
configure-vms:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-playbook playbooks/vms.yml

# Provision TLS certificates (first-time ACME setup)
certs:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-playbook playbooks/certs.yml

# Dry-run — show what would change without applying
check:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-playbook playbooks/site.yml --check --diff

# ── Stack deployment ──────────────────────────────────────────────────────────

# Redeploy a single Swarm stack
# Usage: just deploy-stack stack=media
deploy-stack stack:
    cd infra/ansible && \
    PROXMOX_TOKEN_SECRET="$(sops -d --extract '["proxmox_api_token"]' inventory/group_vars/all/secrets.sops.yml | cut -d= -f2-)" \
    ansible-playbook playbooks/site.yml --tags stack_{{ stack }}

# ── Linting ───────────────────────────────────────────────────────────────────

# Run all linters (Packer, TFLint, ansible-lint)
lint: validate-template lint-tf lint-ansible

lint-tf:
    cd infra/terraform && tflint

lint-ansible:
    ansible-lint infra/ansible/

# ── SOPS helpers ──────────────────────────────────────────────────────────────

# Edit Ansible secrets
edit-ansible-secrets:
    sops infra/ansible/inventory/group_vars/all/secrets.sops.yml

# Edit Packer secrets
edit-packer-secrets:
    sops --input-type binary infra/packer/credentials.sops.pkr.hcl

# Edit Terraform secrets
edit-tf-secrets:
    sops infra/terraform/secrets.sops.tfvars

# Verify secrets are decryptable
verify-secrets:
    sops -d infra/ansible/inventory/group_vars/all/secrets.sops.yml > /dev/null && echo "ansible secrets OK"
    sops -d infra/terraform/secrets.sops.tfvars > /dev/null && echo "terraform secrets OK"
    sops --input-type binary -d infra/packer/credentials.sops.pkr.hcl > /dev/null && echo "packer secrets OK"
