terraform {
  # backend "pg" {
  #   conn_str = "postgres://user:pass@db.example.com/terraform_backend"
  # }

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.101.1"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "=5.18.0"
    }
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = "${var.proxmox_api_token_id}=${var.proxmox_api_token_secret}"
  insecure  = var.proxmox_skip_tls_verify
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
