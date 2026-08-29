
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
  node_name  = var.proxmox_node
  bridge     = var.proxmox_network_bridge
  gateway    = "172.16.20.254"
  dns_server = "172.16.20.254"
  domain     = "blackcats.cc"
  
  k8s_nodes = {
    cp-1 = {
      vm_id       = 2020
      ip_last     = 11
      vcpus       = 8
      memory      = 30720
      disk_gb     = 100
      mac_address = "BC:24:11:01:20:00"
      tags        = ["k8s_cp"]
      dns_records = []
    }
    cp-2 = {
      vm_id       = 2021
      ip_last     = 12
      vcpus       = 8
      memory      = 30720
      disk_gb     = 100
      mac_address = "BC:24:11:01:21:00"
      tags        = ["k8s_cp"]
      dns_records = []
    }
    cp-3 = {
      vm_id       = 2022
      ip_last     = 13
      vcpus       = 8
      memory      = 30720
      disk_gb     = 100
      mac_address = "BC:24:11:01:22:00"
      tags        = ["k8s_cp"]
      dns_records = []
    }

    # Dedicated LLM inference worker. NOT a control plane, NOT an etcd member,
    # and tainted `workload=llm:NoSchedule` in talconfig.yaml so nothing else
    # lands on it. See design/llm-inference.md for the full rationale; the two
    # constraints that force a separate VM rather than more RAM on a CP:
    #
    #   etcd — a large mmap'd model evicts etcd's page cache, and etcd is
    #   fsync-latency-sensitive, so the resulting memory pressure causes
    #   leader-election churn. Losing a second member during that churn is
    #   quorum loss. Inference must not run on an etcd member.
    #
    #   disk — 100 GB with kubelet image GC at 70% leaves ~70 GB usable, which
    #   does not fit model weights alongside Talos and containerd. 250 GB here
    #   because weights live on an openebs-hostpath PVC on this node's local
    #   disk (a 22 GB mmap over NFS is a slow cold start).
    #
    # 48 GB is the Phase 3 *evaluation* allocation, deliberately below every
    # option in the design's RAM plan (A/B/C give llm-1 90/78/64 GB, all of
    # which require cutting the control planes to 20-28 GB). Phase 3 calls for
    # standing this node up at a modest size first and settling the
    # quantization question before committing; 48 GB holds Qwen3.6-35B-A3B Q4
    # (~22 GB) plus KV cache and OS with room to spare, and touches no control
    # plane, so it carries no etcd risk and needs no rolling reboot of the
    # running cluster. Total commit is 144 GB against a 150 GB budget.
    #
    # It does NOT fit Flash-Next IQ3_XXS (82 GB) — that comparison needs the
    # option-A split. Rebalancing later is a `memory` change plus a VM reboot,
    # not a rebuild.
    llm-1 = {
      vm_id       = 2023
      ip_last     = 14
      vcpus       = 8
      memory      = 65536
      disk_gb     = 250
      mac_address = "BC:24:11:01:23:00"
      tags        = ["k8s_worker"]
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
    timeout = "1m"
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
  content = "172.16.20.10"
}
