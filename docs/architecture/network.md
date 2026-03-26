---
tags:
  - architecture
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

## DNS — Technitium

**Primary:** Raspberry Pi (`.1`) — all DNS zones and blocklists configured here.

**Secondary:** DNS VM (`.11`) — zones replicate automatically via Technitium's built-in zone transfer.

- Single configuration point; the secondary is always in sync
- Technitium REST API enables Ansible to manage zones declaratively
- Ad blocking built in (equivalent to Pi-hole)
- UDM SE DHCP hands out `.1` as primary DNS, `.11` as secondary

## NTP — chrony

chrony runs on both the Pi (`.1`) and DNS VM (`.11`), upstream to `pool.ntp.org`.
