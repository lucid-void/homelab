# Homelab IaC — task runner
# See README.md for usage and prerequisites.

# ── Packer ─────────────────────────────────────────────────────────────────

# Build the Debian base VM template in Proxmox
build-template:
    packer build infra/packer/debian-base.pkr.hcl

# ── OpenTofu ────────────────────────────────────────────────────────────────

# Generate a plan and write it to tfplan (review before applying)
plan:
    cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu plan -out=tfplan'

# Show the saved plan in human-readable form
show:
    cd infra/terraform && tofu show tfplan

# Apply exactly the reviewed plan (no re-evaluation)
apply:
    cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu apply tfplan'

# ── Ansible ─────────────────────────────────────────────────────────────────

# Full configuration run — all hosts, all roles
configure:
    ansible-playbook -i infra/ansible/inventory/ infra/ansible/playbooks/site.yml

# Configure a single host (e.g. just configure host=services)
configure-host host:
    ansible-playbook -i infra/ansible/inventory/ infra/ansible/playbooks/site.yml --limit {{ host }}

# Redeploy a single Swarm stack (e.g. just deploy-stack stack=media)
deploy-stack stack:
    ansible-playbook -i infra/ansible/inventory/ infra/ansible/playbooks/site.yml --tags stack_{{ stack }}

# First-time ACME certificate provisioning (registers domains, configures Cloudflare plugin)
certs:
    ansible-playbook -i infra/ansible/inventory/ infra/ansible/playbooks/certs.yml

# ── Lint ─────────────────────────────────────────────────────────────────────

# Validate all IaC files (Packer, Terraform, Ansible)
lint:
    packer validate infra/packer/
    cd infra/terraform && tflint
    ansible-lint infra/ansible/
