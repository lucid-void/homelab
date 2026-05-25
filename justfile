# Homelab — Task Runner

set dotenv-load := false

_default:
    @just --list

# Initialize IAC backends
init:
    cd infra/packer && packer init config.pkr.hcl
    cd ..
    cd infra/terraform && sops exec-env secrets.sops.tfvars 'tofu init -upgrade'

# ── Packer ────────────────────────────────────────────────────────────────────

build-template:
    sops exec-file --input-type binary --filename tmp-file.hcl infra/packer/credentials.sops.pkr.hcl 'packer build --var-file={} infra/packer/Debian13/debian-base.pkr.hcl'

# Build Talos base template in Proxmox
# Update talos_version + talos_schematic_id variables in Talos/talos-base.pkr.hcl before running
build-talos-template:
    sops exec-file --input-type binary --filename tmp-file.hcl infra/packer/credentials.sops.pkr.hcl 'packer build --var-file={} infra/packer/Talos/talos-base.pkr.hcl'

# Validate Packer templates (no build)
validate-template:
    packer validate infra/packer/Debian13/debian-base.pkr.hcl
    packer validate infra/packer/Talos/talos-base.pkr.hcl

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

# ── Linting ───────────────────────────────────────────────────────────────────

# Run all linters (Packer, TFLint)
lint: validate-template lint-tf

lint-tf:
    cd infra/terraform && tflint

# ── SOPS helpers ──────────────────────────────────────────────────────────────

# Edit Packer secrets
edit-packer-secrets:
    sops --input-type binary infra/packer/credentials.sops.pkr.hcl

# Edit Terraform secrets
edit-tf-secrets:
    sops infra/terraform/secrets.sops.tfvars

# Verify secrets are decryptable
verify-secrets:
    sops -d infra/terraform/secrets.sops.tfvars > /dev/null && echo "terraform secrets OK"
    sops --input-type binary -d infra/packer/credentials.sops.pkr.hcl > /dev/null && echo "packer secrets OK"
