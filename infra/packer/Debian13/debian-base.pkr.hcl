# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

variable "proxmox_api_url" {
  type    = string
  default = "https://<proxmox-ip>:8006/api2/json"
}

variable "proxmox_api_token_id" {
  type      = string
  sensitive = false
  default   = "packer@pam"
}

variable "proxmox_api_token_secret" {
  type      = string
  sensitive = true
}

variable "proxmox_node" {
  type    = string
  default = "proxmox"
}

variable "proxmox_vm_storage" {
  type    = string
  default = "local-lvm"
}

variable "proxmox_iso_storage" {
  type    = string
  default = "local"
}

variable "proxmox_network_bridge" {
  type    = string
  default = "vmbr0"
}

variable "TZ" {
  type    = string
  default = "local"
}

variable "ssh_username" {
  type    = string
  default = "root"
}

variable "ssh_password" {
  type      = string
  sensitive = true
}

variable "proxmox_skip_tls_verify" {
  type      = bool
  default   = false
}

variable "ssh_public_key" {
  type    = string
  sensitive = true
}

variable "hashed_password" {
  type    = string
  sensitive = true
}

# ---------------------------------------------------------------------------
# Locals — template name with datestamp
# ---------------------------------------------------------------------------

locals {
  build_date    = formatdate("YYYY-MM-DD", timestamp())
  template_name = "debian-13-base"
}

# ---------------------------------------------------------------------------
# Source — proxmox-iso builder
# ---------------------------------------------------------------------------

source "proxmox-iso" "debian_base" {
  # Proxmox connection
  proxmox_url              = var.proxmox_api_url
  username                 = var.proxmox_api_token_id
  token                    = var.proxmox_api_token_secret
  insecure_skip_tls_verify = var.proxmox_skip_tls_verify
  node                     = var.proxmox_node
  vm_id = 3000

  # Template identity
  vm_name              = local.template_name
  template_description = "Debian 13 (Trixie) cloud-init base image — built by Packer on ${local.build_date}"


  # ISO — Debian Trixie netinst (official mirror)
  boot_iso {
    type              = "scsi"
    # iso_file         = "${var.proxmox_iso_storage}:iso/e9cb1d6c0f6a0c4e81818e6fd169abd3d4d7b84c.iso"
    iso_url           = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.4.0-amd64-netinst.iso"
    iso_checksum      = "sha256:0b813535dd76f2ea96eff908c65e8521512c92a0631fd41c95756ffd7d4896dc"
    unmount           = true
    iso_storage_pool  = var.proxmox_iso_storage
  }

  # Base VM specs (template defaults — Ansible/Tofu resize per-VM after clone)
  cpu_type    = "host"
  cores       = 4
  memory      = 4096
  os          = "l26"
  qemu_agent  = true

  # Boot disk — raw format on virtio-scsi-single for best IOPS on ZFS zvols
  scsi_controller = "virtio-scsi-single"
  disks {
    type         = "scsi"
    disk_size    = "40G"
    storage_pool = var.proxmox_vm_storage
    format       = "raw"
    discard      = true
    ssd          = true
    io_thread    = true
    cache_mode   = "none"
  }

  # Network — virtio, MTU 9000 (jumbo frames on vmbr0)
  network_adapters {
    model    = "virtio"
    bridge   = var.proxmox_network_bridge
    mtu      = 9000
    firewall = false
  }

  # Cloud-init drive (Proxmox native — ScSI bus, ide2 for cloudinit)
  cloud_init              = true
  cloud_init_storage_pool = var.proxmox_vm_storage

  # Unattended install
  boot_command = [
    "<esc><wait>auto url=http://{{ .HTTPIP }}:{{ .HTTPPort }}/preseed.cfg<enter>"
  ]
  boot_wait = "10s"

  # http_directory = "http"
  http_content ={
    "/preseed.cfg" = templatefile("http/preseed.cfg.tpl", { 
      user = var.ssh_username, 
      password = var.ssh_password, 
      TZ=var.TZ 
    })
    # "/meta-data"  = file("http/meta-data")
    # "/user-data" = templatefile("http/user-data.tpl", { user = var.ssh_username, password = var.hashed_password, TZ=var.TZ, packages=var.packages, shell=var.shell})
  }

  # Packer SSHes in as the temporary bootstrap user created by preseed.
  # The shell provisioner below locks this account and creates void/docker.
  ssh_username         = var.ssh_username
  ssh_password         = var.ssh_password
  ssh_timeout          = "30m"
  ssh_handshake_attempts = 15
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build {
  name    = "debian-base"
  sources = ["source.proxmox-iso.debian_base"]

  # 1. Wait for cloud-init to fully settle before we start provisioning
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [ "cloud-init status --wait || true" ]
  }

  # 2. System hardening + package baseline
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [
      "set -euo pipefail",

      # Ensure apt is current
      "export DEBIAN_FRONTEND=noninteractive",
      "apt-get update -qq",
      "apt-get upgrade -y -qq",

      # Core packages baked into the image
      "apt-get install -y -qq curl wget gnupg2 ca-certificates apt-transport-https lsb-release python3 python3-pip cloud-guest-utils nftables chrony rsync vim htop jq less sudo git zsh resolvconf",

      # Remove dhcpcd — conflicts with cloud-init network management and stomps resolv.conf
      "apt-get remove -y dhcpcd dhcpcd5 || true",

      # ifupdown in Debian 13 does not export $IF_DNS_NAMESERVERS so the
      # 000resolvconf hook never populates resolv.conf from cloud-init's
      # dns-nameservers directive. Write DNS into resolvconf's base config
      # instead — it is merged unconditionally on every resolvconf update,
      # so DNS works both during the Packer build and on every clone boot.
      "echo 'nameserver 172.16.20.254' > /etc/resolvconf/resolv.conf.d/base",
      "resolvconf -u",
    ]
  }

  # 3. Users and groups
  # iacuser (UID 1000): bootstrap user for Packer/Ansible; SSH key auth only
  # labops (GID 2200): shared group owning all data paths; void and docker are members
  # void (UID/GID 2201): lab operator account — SSH key auth, sudo, labops member
  # docker (UID/GID 2202): container runtime — no login shell, labops member
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [
      "set -euo pipefail",

      # labops group — GID 2200
      "groupadd --gid 2200 labops",

      # void group — GID 2201 (primary group for void user)
      "groupadd --gid 2201 void",

      # docker group — GID 2202 (primary group for docker user)
      "groupadd --gid 2202 docker",

      # void user — UID 2201, sudo + labops, SSH key auth only
      "useradd --uid 2201 --gid 2201 --groups sudo,labops,docker --create-home --shell $(which zsh) --comment 'Lab operator' void",
      "echo 'void:${var.hashed_password}' | chpasswd -e",

      # docker user — UID 2202, primary group docker, no login shell, labops member
      "useradd --uid 2202 --gid 2202 --groups labops --no-create-home --shell /usr/sbin/nologin --comment 'Docker daemon user' docker",

      # Add IaC admin user (ssh_username) to labops
      "usermod -aG labops ${var.ssh_username}",

      # void's SSH directory with authorized keys from github/gitlab
      "mkdir -p /home/void/.ssh",
      "chmod 700 /home/void/.ssh",
      "curl -s https://github.com/lucid-void.keys >> /home/void/.ssh/authorized_keys",
      "chmod 600 /home/void/.ssh/authorized_keys",
      "chown -R void:void /home/void/.ssh",

      # Dotfiles — cloned into void's home at image build time
      "git clone https://github.com/lucid-void/dotfiles /home/void/.dotfiles",
      "chown -R void:void /home/void/.dotfiles",
    ]
  }

  # 4. Directory structure
  # /opt/compose/ — Docker Compose stacks (Ansible-managed, owned by void)
  # /opt/volumes/ — Local bind-mount data (container-written, owned by docker)
  # Subdirectories created per-service by Ansible; base trees set here.
  # umask 0002 ensures new files inherit labops group via setgid.
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [
      "set -euo pipefail",

      "install -d -m 2775 -o void   -g labops /opt/compose",
      "install -d -m 2775 -o docker -g labops /opt/volumes",

      # umask 0002 — new files group-writable; required so void and docker
      # can read/write each other's files under labops-owned paths
      "printf 'umask 0002\\n' > /etc/profile.d/labops.sh",
      "chmod 644 /etc/profile.d/labops.sh",
    ]
  }


  # 5. cloud-init configuration — Proxmox-compatible
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [
      "set -euo pipefail",

      # Clear any installation-time cloud-init state so each clone boot
      # re-runs user-data, meta-data, and network-config from the Proxmox cloud-init drive.
      "cloud-init clean --logs",

      # Datasource: NoCloud (Proxmox injects via virtual CD-ROM / IDE drive, auto-detected)
      "printf 'datasource_list: [ NoCloud, ConfigDrive ]\\n' > /etc/cloud/cloud.cfg.d/99_proxmox.cfg",
    ]
  }

  # 6. SSH hardening + ssh key for iacuser
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [
      "set -euo pipefail",
      "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config",
      "sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config",
      "sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config",
      "sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config",

      # ssh keys for iacuser
      "mkdir -p /home/${var.ssh_username}/.ssh",
      "echo '${var.ssh_public_key}' > /home/${var.ssh_username}/.ssh/authorized_keys",
      "chmod 600 /home/${var.ssh_username}/.ssh/authorized_keys",
      "chown -R ${var.ssh_username}:${var.ssh_username} /home/${var.ssh_username}/.ssh",
    ]
  }

  # 7. Final cleanup — zero free space for template compression
  provisioner "shell" {
    execute_command = "sudo sh -c '{{ .Vars }} {{ .Path }}'"
    inline = [
      "set -euo pipefail",

      # Package cleanup
      "apt-get autoremove -y -qq",
      "apt-get clean",
      "rm -rf /var/lib/apt/lists/*",

      # Reset machine-id — each clone must generate its own unique ID
      "rm -f /etc/machine-id /var/lib/dbus/machine-id",
      "truncate -s 0 /etc/machine-id",

      "sync",
      # Zero-fill free space — improves template compression on thin-provisioned storage
      "dd if=/dev/zero of=/zero_fill bs=1M || true",
      "rm -f /zero_fill",
      "sync",
    ]
  }
}
