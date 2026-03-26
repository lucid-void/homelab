---
tags:
  - diagrams
  - topology
  - architecture
---

# Topology Diagrams

Interactive diagrams showing the physical and logical layout of the homelab. Each diagram is a cleaned-up version of the visual design created during the architecture planning sessions.

## Physical Network

Physical hardware, switch connections, and IP addresses for all nodes on the `172.16.20.0/24` homelab VLAN.

<iframe
  src="assets/physical-topology.html"
  style="width:100%;height:520px;border:none;border-radius:6px;"
  title="Physical network topology">
</iframe>

---

## Docker Swarm Topology

Swarm node layout, manager/worker roles, and service placement across all compute nodes.

<iframe
  src="assets/swarm-topology.html"
  style="width:100%;height:520px;border:none;border-radius:6px;"
  title="Docker Swarm topology">
</iframe>

---

## Storage Layout

TrueNAS ZFS dataset tree, NFS export targets, MinIO S3 buckets, and backup flow.

<iframe
  src="assets/storage-layout.html"
  style="width:100%;height:560px;border:none;border-radius:6px;"
  title="Storage layout">
</iframe>

---

## Monitoring Topology

PLG stack placement, exporter deployment across hosts, and alerting flow to Gotify.

<iframe
  src="assets/monitoring-topology.html"
  style="width:100%;height:520px;border:none;border-radius:6px;"
  title="Monitoring topology">
</iframe>
