# ---------------------------------------------------------------------------
# Variables — Proxmox connection (same as Debian template, from credentials.sops.pkr.hcl)
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

variable "proxmox_skip_tls_verify" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------------------
# Variables — Talos image
# Update both when upgrading Talos. Schematic ID encodes the extension set
# chosen at factory.talos.dev — regenerate if extensions change.
# ---------------------------------------------------------------------------

variable "talos_schematic_id" {
  type    = string
  default = "ef013c714202a52bc6501ca5cefc6814491c9fd42ac6cf67be46031d31b2e79c"
}

variable "talos_version" {
  type    = string
  default = "v1.13.2"
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  build_date    = formatdate("YYYY-MM-DD", timestamp())
  template_name = "talos-${var.talos_version}"
}

# ---------------------------------------------------------------------------
# Source — proxmox-iso builder, communicator = none
#
# Packer creates a VM, attaches the Talos metal ISO, and converts it to a
# Proxmox template. No provisioning happens — Talos has no SSH or shell.
#
# VMs cloned from this template boot the ISO into Talos maintenance mode.
# talhelper apply then sends the machine config, which triggers the Talos
# installer to write the OS to disk. After the first reboot the VM runs
# Talos from disk; the ISO remains attached but the on-disk bootloader
# takes precedence.
# ---------------------------------------------------------------------------

source "proxmox-iso" "talos_base" {
  # Proxmox connection
  proxmox_url              = var.proxmox_api_url
  username                 = var.proxmox_api_token_id
  token                    = var.proxmox_api_token_secret
  insecure_skip_tls_verify = var.proxmox_skip_tls_verify
  node                     = var.proxmox_node
  vm_id                    = 9001

  # Template identity
  vm_name              = local.template_name
  template_description = "Talos Linux ${var.talos_version} — schematic: ${var.talos_schematic_id} — built ${local.build_date}"

  # Talos metal ISO — Proxmox downloads it from factory.talos.dev
  # unmount = false: ISO stays attached in the template so cloned VMs can boot it
  boot_iso {
    type             = "scsi"
    iso_url          = "https://factory.talos.dev/image/${var.talos_schematic_id}/${var.talos_version}/metal-amd64.iso"
    iso_checksum     = "none"
    iso_storage_pool = var.proxmox_iso_storage
    unmount          = false
  }

  # No communicator — Talos has no SSH or shell
  communicator = "none"
  boot_wait    = "5s"

  # Hardware
  cpu_type   = "host"
  cores      = 2
  memory     = 4096
  os         = "l26"
  qemu_agent = true

  # UEFI — Talos supports both BIOS and UEFI; UEFI preferred for k8s nodes
  bios = "ovmf"
  efi_config {
    efi_storage_pool  = var.proxmox_vm_storage
    efi_type          = "4m"
    pre_enrolled_keys = false
  }

  # Boot disk — empty at template creation; Talos installer writes here on first boot
  scsi_controller = "virtio-scsi-single"
  disks {
    type         = "scsi"
    disk_size    = "20G"
    storage_pool = var.proxmox_vm_storage
    format       = "raw"
    discard      = true
    ssd          = true
    io_thread    = true
    cache_mode   = "none"
  }

  # Network
  network_adapters {
    model    = "virtio"
    bridge   = var.proxmox_network_bridge
    mtu      = 9000
    firewall = false
  }
}

# ---------------------------------------------------------------------------
# Build — no provisioners; template captures hardware spec + ISO attachment
# ---------------------------------------------------------------------------

build {
  name    = "talos-base"
  sources = ["source.proxmox-iso.talos_base"]
}
