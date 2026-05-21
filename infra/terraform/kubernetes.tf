
# ---------------------------------------------------------------------------
# Local values — Kubernetes nodes
# ---------------------------------------------------------------------------
#
# No cloud-init: Talos does not use cloud-init for IP addressing.
# Static IPs are set in two places that must agree:
#   1. UDM SE — static DHCP lease per MAC address (first-boot reachability)
#   2. kubernetes/talos/talconfig.yaml — Talos machine config (permanent config)
#
# MAC addresses are fixed so DHCP reservations survive VM rebuilds.
# The BC:24:11 prefix is Proxmox's registered OUI.
# ---------------------------------------------------------------------------

locals {
  k8s_nodes = {
    k8s-cp-1 = {
      vm_id       = 2020
      ip_last     = 20
      vcpus       = 4
      memory      = 16384
      disk_gb     = 20
      mac_address = "BC:24:11:00:20:00"
      tags        = ["k8s_cp"]
      dns_records = []
    }
    k8s-cp-2 = {
      vm_id       = 2021
      ip_last     = 21
      vcpus       = 4
      memory      = 16384
      disk_gb     = 20
      mac_address = "BC:24:11:00:21:00"
      tags        = ["k8s_cp"]
      dns_records = []
    }
    k8s-cp-3 = {
      vm_id       = 2022
      ip_last     = 22
      vcpus       = 4
      memory      = 16384
      disk_gb     = 20
      mac_address = "BC:24:11:00:22:00"
      tags        = ["k8s_cp"]
      dns_records = []
    }
  }
}

# ---------------------------------------------------------------------------
# VMs — cloned from Talos Packer template (VM 9001)
# ---------------------------------------------------------------------------

resource "proxmox_virtual_environment_vm" "k8s_nodes" {
  for_each = local.k8s_nodes

  vm_id     = each.value.vm_id
  name      = each.key
  node_name = local.node_name
  tags      = each.value.tags

  clone {
    vm_id   = var.talos_template_vm_id
    full    = true
    retries = 3
  }

  cpu {
    cores = each.value.vcpus
    type  = "host"
  }

  memory {
    dedicated = each.value.memory
  }

  disk {
    datastore_id = var.proxmox_vm_storage
    interface    = "scsi0"
    size         = each.value.disk_gb
    file_format  = "raw"
    discard      = "on"
    iothread     = true
    cache        = "none"
  }

  network_device {
    bridge      = local.bridge
    model       = "virtio"
    mtu         = 9000
    mac_address = each.value.mac_address
  }

  # No initialization block — Talos does not use cloud-init.
  # IP addressing: DHCP reservation on UDM SE (first boot) +
  # static config in kubernetes/talos/talconfig.yaml (applied via talhelper).

  agent {
    enabled = true
  }

  lifecycle {
    ignore_changes = [
      clone,
    ]
  }
}

# ---------------------------------------------------------------------------
# DNS — A records for node hostnames + VIP
# Service records (Traefik ingress) are added once the cluster is live
# ---------------------------------------------------------------------------

resource "cloudflare_dns_record" "k8s_hosts" {
  for_each = local.k8s_nodes

  zone_id = var.cloudflare_zone_id
  name    = each.key
  type    = "A"
  ttl     = 3600
  content = "172.16.20.${each.value.ip_last}"
}

# VIP — floats between control planes via leader election
resource "cloudflare_dns_record" "k8s_vip" {
  zone_id = var.cloudflare_zone_id
  name    = "k8s"
  type    = "A"
  ttl     = 3600
  content = "172.16.20.19"
}
