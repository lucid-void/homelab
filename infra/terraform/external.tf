locals {
  external = {
    pi = {
      ip_last    = 1
      dns_records= []
    }
    synology = {
      ip_last    = 2
      dns_records= ["nas"]
    }
    pve = {
      ip_last    = 3
      dns_records= []
    }
    dgx = {
      ip_last    = 4
      dns_records= ["ollama", "vllm", "openwebui", "langfuse", "qdrant", "searxng"]
    }
    
  }
}

resource "cloudflare_dns_record" "external_hosts" {
  for_each = local.external

  zone_id      = var.cloudflare_zone_id
  name    = "${each.key}.${local.domain}"
  type      = "A"
  ttl       = 3600
  content = "172.16.20.${each.value.ip_last}"
}

resource "cloudflare_dns_record" "external_services" {
  for_each = {
    for pair in flatten([
      for vm_key, vm in local.external : [
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