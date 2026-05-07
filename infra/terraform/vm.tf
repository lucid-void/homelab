
# ---------------------------------------------------------------------------
# Local values
# ---------------------------------------------------------------------------

locals {
  node_name  = var.proxmox_node
  bridge     = var.proxmox_network_bridge
  gateway    = "172.16.20.254"
  dns_server = "172.16.20.254"
  domain     = "blackcats.cc"

  vms = {
    db = {
      vm_id      = 2010
      ip_last    = 10
      vcpus      = 2
      memory     = 4096
      disk_gb    = 40
      tags       = ["iac", "swarm_worker"]
      dns_records= []
      boot_order = 2
      up_delay   = 0
    }
    monitoring = {
      vm_id      = 2011
      ip_last    = 16
      vcpus      = 2
      memory     = 4096
      disk_gb    = 40
      tags       = ["iac", "swarm_worker"]
      dns_records= []
      boot_order = 7
      up_delay   = 15
    }
    media = {
      vm_id      = 2012
      ip_last    = 12
      vcpus      = 4
      memory     = 4096
      disk_gb    = 64
      tags       = ["iac", "swarm_worker"]
      dns_records= [] #sabnzbd, flaresolver, plex
      boot_order = 4
      up_delay   = 15
    }
    services = {
      vm_id      = 2013
      ip_last    = 13
      vcpus      = 4
      memory     = 16384
      disk_gb    = 40
      tags       = ["iac", "swarm_manager"]
      dns_records= [
        # services vm (.13)
        "traefik", "paperless", "immich", "photos", "homebox", "tools", "rss", "gitea", "zitadel", "authelia", "auth",
        # db vm (.10)
        "pgadmin", "postgres", "mariadb", "adminer",
        # media vm (.12)
        "sonarr", "radarr", "nzb", "seerr", "prowlarr", "tautulli",
        # monitoring vm (.16)
        "grafana", "prometheus", "loki", "cadvisor", "unifi-poller", "gotify", "status",
        # game vm (.14)
        "satisfactory", "crafty",
      ]
      boot_order = 3
      up_delay   = 30
    }
    game = {
      vm_id      = 2014
      ip_last    = 14
      vcpus      = 4
      memory     = 6192
      disk_gb    = 40
      tags       = ["iac", "swarm_worker"]
      dns_records= []
      boot_order = 5
      up_delay   = 0
    }
    lab = {
      vm_id      = 2015
      ip_last    = 15
      vcpus      = 2
      memory     = 4096
      disk_gb    = 40
      tags       = ["iac", "swarm_worker"]
      dns_records= []
      boot_order = 6
      up_delay   = 0
    }
    runner = {
      vm_id      = 2017
      ip_last    = 17
      vcpus      = 2
      memory     = 4096
      disk_gb    = 60
      tags       = ["iac", "runner"]
      dns_records= []
      boot_order = 8
      up_delay   = 0
    }
  }
}

# ---------------------------------------------------------------------------
# VMs — cloned from Packer base template
# ---------------------------------------------------------------------------

resource "proxmox_virtual_environment_vm" "nodes" { #TODO update template according to the used proxmox provider
  for_each = local.vms

  vm_id     = each.value.vm_id
  name      = each.key
  node_name = local.node_name
  tags      = each.value.tags


  clone {
    vm_id   = var.packer_template_vm_id
    full    = true
    retries = 3
  }

  cpu {
    cores = each.value.vcpus
    type  = "host"
  }

  memory {
    dedicated    = each.value.memory
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
    bridge  = local.bridge
    model   = "virtio"
    mtu     = 9000
  }

  # Cloud-init: static IP, gateway, DNS
  initialization {
    datastore_id = var.proxmox_vm_storage

    ip_config {
      ipv4 {
        address = "172.16.20.${each.value.ip_last}/24"
        gateway = local.gateway
      }
    }

    dns {
      servers = [local.dns_server]
    }
  }

  # Startup ordering
  startup {
    order      = each.value.boot_order
    up_delay   = each.value.up_delay
    down_delay = 0
  }

  agent {
    enabled = true
  }

  lifecycle {
    ignore_changes = [
      # Prevent Tofu from touching the template after initial clone
      clone,
    ]
  }
}



# ---------------------------------------------------------------------------
# DNS records for VM's
# ---------------------------------------------------------------------------

resource "cloudflare_dns_record" "vm_hosts" {
  for_each = local.vms
  zone_id      = var.cloudflare_zone_id
  name    = "${each.key}"
  type      = "A"
  ttl       = 3600
  content = "172.16.20.${each.value.ip_last}"
}

resource "cloudflare_dns_record" "vm_services" {
  for_each = {
    for pair in flatten([
      for vm_key, vm in local.vms : [
        for record in vm.dns_records : {
          key    = record
          name   = record
          target = "${vm_key}.${local.domain}"
          vm_key = vm_key
        }
      ]
    ]) : pair.key => pair if pair.key != pair.vm_key
  }
  zone_id      = var.cloudflare_zone_id
  name    = each.value.name
  type    = "CNAME"
  ttl     = 3600
  content = each.value.target
}