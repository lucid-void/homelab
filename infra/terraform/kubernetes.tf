
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
    #   disk (a 35 GB mmap over NFS is a slow cold start).
    #
    # 70 GiB here (71680 MiB), and the control planes sit at 30 GB each, for a
    # total of 160 GB — the full amount available, confirmed against the host.
    # The earlier "154 GB is 4 GB OVER the 150 GB budget" warning is resolved:
    # 150 was the conservative figure, 160 is the real one.
    #
    # The node holds ONE model — Qwen3.6-35B-A3B Q8_0 (34.4 GiB), which is
    # effectively lossless. The extra 6 GB over the original 64 GB buys KV
    # cache, i.e. context window, NOT a second model:
    #
    #   weights 34.4 + prompt cache 8.0 + KV ~24 @ 128K + overhead ~2.5
    #   = ~69 GiB against 68.2 GiB allocatable -- does NOT fit.
    #
    # The 8 GiB is llama-server's cross-request prompt cache: cache_ram_mib
    # defaults to 8192 MiB and is on without being asked for. It was missed in
    # the first budget, which is why 128K no longer closes and --ctx-size is
    # still 32768. The resize to 70 GiB is still right -- it is what makes the
    # 34.4 GiB model plus that cache comfortable -- but the context target has
    # to be re-derived from a measured KV figure, not linear scaling.
    #
    # There is deliberately no separate vision model. Qwen3.6-35B-A3B is itself
    # multimodal (image-text-to-text; the unsloth GGUF repo ships an 0.8 GiB
    # mmproj-F16.gguf), so vision — if ever wanted for Paperless or Immich — is
    # one extra file and one --mmproj flag, not another 17 GiB resident. The
    # old plan here paired Q4 (~22 GB) with a distinct Qwen3.8-27B (~18.8 GB);
    # that pairing is obsolete and was never deployed.
    #
    # The KV figure above is scaled from an estimate, not measured on this
    # model. Boot llama-server once at --ctx-size 32768, read "KV self size"
    # from its startup log, and scale from that before committing to 131072.
    # There is no memory limit on the pod (mmap page-cache accounting), so an
    # over-large context takes the node, not the container.
    #
    # Control planes stay at 8 vCPU for now. 3x8 + 8 = 32 vCPU on 14 physical
    # cores is a 2.3x commit and inference is the workload most hurt by it, but
    # cutting them is a change to three RUNNING nodes — deferred until the
    # benchmark in design/llm-deployment.md shows contention is real.
    #
    # Does NOT fit Flash-Next IQ3_XXS (82 GB) — that needs option A (90 GB
    # here, 20 GB per control plane).
    llm-1 = {
      vm_id       = 2014
      ip_last     = 14
      vcpus       = 8
      memory      = 71680
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
