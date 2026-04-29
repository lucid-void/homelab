---
tags:
  - stack
  - network
  - dns
  - vlan
---

# Network & DNS

## VLAN Structure

| VLAN | Purpose |
|---|---|
| Clients | Workstations, laptops, phones |
| IoT | Smart home devices, isolated from clients |
| Homelab | All servers — `172.16.20.0/24` |

All inter-VLAN routing and firewall rules are managed on the UDM SE.

<iframe
  src="../network-topology.html"
  style="width:100%;border:none;border-radius:6px;"
  title="Network topology">
</iframe>

## DNS — UDM SE

DNS is handled by the **UDM SE at `.254`**. DHCP hands out `.254` as the DNS resolver for all homelab clients and VMs.

The UDM SE provides:
- **Local overrides** for all `*.blackcats.cc` internal records (A records → `172.16.20.x`)
- **Ad blocking / DNS filtering** via UniFi's built-in DNS filtering feature
- **Upstream forwarding** to `1.1.1.1` / `1.0.0.1` for external names

Cloudflare holds the public-facing DNS records (same `*.blackcats.cc` → internal IPs) solely for DNS-01 ACME certificate issuance. No traffic is proxied through Cloudflare. There is no separate DNS server VM.

**Split-horizon DNS:** All public DNS records point to internal `172.16.20.x` addresses. External resolvers return the same IPs — unreachable without local network or VPN access. No separate internal/external zone management needed.

## NTP

VMs and hosts use public NTP via `pool.ntp.org`. No local NTP server is required.

## Host Firewall — nftables

The Services VM (`.13`) has an `nftables` policy (managed by Ansible) that restricts the Swarm manager port (`2377`) to Swarm workers only (`.4`, `.10`, `.11`, `.12`, `.14`, `.15`).

A **default-deny inbound policy** with an explicit allowlist (SSH, `node_exporter`, Promtail, plus per-host overrides) is **planned** for all Proxmox VMs and LXCs. Not yet implemented.

## Internet Exposure

The homelab has **no inbound internet exposure**. Cloudflare is used solely to obtain valid TLS certificates via DNS-01 ACME challenge.

| Topic | Detail |
|---|---|
| Cloudflare proxy | Disabled — DNS-only mode (grey cloud) on all records |
| DNS A records | Resolve to internal `172.16.20.x` addresses |
| Port forwarding | None on the UDM SE |
| Cloudflare Tunnels | Not deployed; could be added in future |

**External access:**

| Method | Use case |
|---|---|
| Local network | Normal path for all homelab services |
| Netbird | Primary remote access VPN — full VLAN reachability |
| ZeroTier | Gaming with friends (Game VM, `.14`) |
