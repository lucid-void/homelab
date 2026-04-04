# Homelab IaC

Infrastructure-as-Code for a personal homelab. Packer builds Debian VM templates, OpenTofu provisions VMs and DNS records, Ansible configures hosts and deploys Docker Swarm services.

---

## Prerequisites

Install the following tools before running any `just` targets:

| Tool | Purpose |
|---|---|
| [just](https://github.com/casey/just) | Task runner — entry point for all operations |
| [packer](https://developer.hashicorp.com/packer/downloads) | Builds the Debian base VM template |
| [opentofu](https://opentofu.org/docs/intro/install/) | Provisions VMs and DNS records |
| [ansible](https://docs.ansible.com/ansible/latest/installation_guide/) | Configures hosts and deploys stacks |
| [sops](https://github.com/getsops/sops/releases) | Encrypts/decrypts secret files |
| [age](https://github.com/FiloSottile/age) | Encryption backend used by SOPS |
| [tflint](https://github.com/terraform-linters/tflint) | Terraform linter |
| [ansible-lint](https://ansible-lint.readthedocs.io/) | Ansible linter |

Install required Ansible collections:

```bash
ansible-galaxy collection install community.general community.docker
```

---

## Repository layout

```
Homelab/
├── infra/
│   ├── packer/                    # Debian base VM template
│   ├── terraform/                 # VM provisioning + DNS records (OpenTofu)
│   └── ansible/
│       ├── inventory/             # physical.yml (static) + proxmox.yml (dynamic)
│       ├── group_vars/all/        # vars.yml + secrets.sops.yml
│       ├── roles/                 # common, docker, truenas, proxmox, pbs
│       └── playbooks/             # site.yml, vms.yml, certs.yml
├── stacks/                        # Docker Compose stacks (one dir per service group)
├── .gitea/workflows/              # Gitea Actions CI pipelines
├── .sops.yaml                     # SOPS encryption rules (commit this)
├── justfile                       # task runner entry point
└── README.md
```

---

## First-time SOPS setup

Secrets are encrypted with [SOPS](https://github.com/getsops/sops) using your SSH ed25519 key as the age recipient. A separate recovery key provides a second decryption path in case your primary key is lost.

### 1. Get your SSH public key

```bash
cat ~/.ssh/id_ed25519.pub
# ssh-ed25519 AAAA... user@host
```

If you don't have an ed25519 key yet, generate one:

```bash
ssh-keygen -t ed25519 -C "homelab"
```

### 2. Generate a recovery age key

```bash
age-keygen -o ~/recovery.key
# Public key: age1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Store `recovery.key` somewhere safe and offline — this is your break-glass key:

- Copy it to `tank/backups/keys/` on TrueNAS (ZFS-encrypted, never NFS-exported)
- Print a paper copy and store it offline
- Delete the local file after storing it: `shred -u ~/recovery.key`

### 3. Edit `.sops.yaml`

Replace the two placeholders with your actual keys:

```yaml
creation_rules:
  - path_regex: .*\.sops\.ya?ml$
    age: >-
      ssh-ed25519 AAAA...youractualkey user@host,
      age1yourrecoverykeypublickey
  - path_regex: .*\.sops\.tfvars$
    age: >-
      ssh-ed25519 AAAA...youractualkey user@host,
      age1yourrecoverykeypublickey
```

Commit `.sops.yaml` — it contains only public keys, no secrets.

### 4. Create your first encrypted secret file

```bash
# Ansible secrets
sops infra/ansible/group_vars/all/secrets.sops.yml

# Terraform secrets
sops infra/terraform/secrets.sops.tfvars
```

SOPS opens your editor. Add your secrets, save, and close — the file is encrypted on disk.

Credentials to populate are documented in `design/iac-pipeline.md` (§ Secret Management).

### 5. Verify decryption

```bash
sops -d infra/ansible/group_vars/all/secrets.sops.yml
```

If SOPS can't find your private key automatically, point it explicitly:

```bash
export SOPS_AGE_SSH_KEY_FILE=~/.ssh/id_ed25519
sops -d infra/ansible/group_vars/all/secrets.sops.yml
```

---

## Bootstrap sequence

Run these in order on a fresh environment. See `PLAN.md` (gitignored) for the full checklist.

### Step 1 — Build the VM template

Builds a Debian base image in Proxmox. Run once per Debian major release.

```bash
just build-template
```

Template name format: `debian-12-base-YYYYMMDD`

### Step 2 — Provision VMs and DNS

Always review the plan before applying. The `apply` target applies the saved plan file exactly — it does not re-evaluate.

```bash
just plan    # generate and save plan to tfplan
just show    # inspect the plan in human-readable form
just apply   # apply exactly what was reviewed
```

### Step 3 — Configure hosts

Runs Ansible against all hosts: installs Docker, joins Swarm workers, configures NFS mounts, and deploys stacks.

```bash
just configure
```

To run against a single host first (recommended for initial testing):

```bash
just configure-host host=services
```

### Step 4 — Provision certificates

First-time ACME setup: registers domains, configures the Cloudflare DNS plugin on Proxmox, PBS, TrueNAS, and Traefik. Each service then owns its own renewal cycle.

```bash
just certs
```

### Step 5 — Deploy stacks

Individual stacks can be redeployed without a full `configure` run:

```bash
just deploy-stack stack=traefik
just deploy-stack stack=media
just deploy-stack stack=monitoring
# etc.
```

---

## Justfile reference

| Target | Description |
|---|---|
| `just build-template` | Build Debian base VM template in Proxmox via Packer |
| `just plan` | Run `tofu plan -out=tfplan` (wrapped in sops for secrets) |
| `just show` | Show the saved plan in human-readable form |
| `just apply` | Apply the saved plan (no re-evaluation — applies exactly what was reviewed) |
| `just configure` | Full Ansible run — all hosts, all roles |
| `just configure-host host=<name>` | Ansible run limited to a single host |
| `just deploy-stack stack=<name>` | Redeploy a single Docker Swarm stack |
| `just certs` | First-time ACME certificate provisioning |
| `just lint` | Validate Packer, Terraform, and Ansible files |

---

## Credential files

Two encrypted files hold all secrets. Create them with `sops <path>` after completing the SOPS setup above.

### `infra/ansible/group_vars/all/secrets.sops.yml`

Ansible secrets, including:

- Cloudflare API tokens (one per consumer: Traefik, Proxmox, PBS, TrueNAS)
- Database passwords (Postgres per-database, MariaDB)
- Proxmox API token
- SSH keys for service accounts
- Valkey passwords (Authentik, Authelia)
- OIDC client secret (Authelia → Authentik)
- Gotify app tokens
- UDM SE read-only account (unifi-poller)
- TrueNAS REST API key
- Authentik SECRET_KEY
- Gitea secrets (SECRET_KEY, INTERNAL_TOKEN)
- rclone crypt password + salt

### `infra/terraform/secrets.sops.tfvars`

Terraform/OpenTofu secrets:

- Proxmox API credentials (URL, user, token ID, token secret)
- MinIO access key and secret key (for Tofu state backend)

Full credential rotation procedures are documented in `design/iac-pipeline.md`.

---

## CI (Gitea Actions)

Workflows live in `.gitea/workflows/`. The repository is mirrored from GitHub to a self-hosted Gitea instance on a 10-minute schedule. Gitea Actions runs on every sync using a dedicated Debian LXC runner at `172.16.20.17`.

| Workflow | Trigger | What it does |
|---|---|---|
| `lint.yml` | Push to `main` | `packer validate`, `tflint`, `ansible-lint` |
| `plan.yml` | Manual dispatch | `tofu plan -out=tfplan` |
| `configure.yml` | Manual dispatch | `ansible-playbook site.yml` |
| `drift.yml` | Weekly schedule | `tofu plan` + `ansible --check --diff` → Gotify notification |

**Apply is never automated.** `tofu apply` is always run manually after reviewing the plan.
