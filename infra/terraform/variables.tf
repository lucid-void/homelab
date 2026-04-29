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

variable "proxmox_skip_tls_verify" {
  type      = bool
  default   = false
}

variable "packer_template_vm_id" {
  type        = number
  description = "VM ID of the Packer base template in Proxmox"
}

variable "cloudflare_api_token" {
  type = string
}

variable "cloudflare_zone_id" {
  type = string
}