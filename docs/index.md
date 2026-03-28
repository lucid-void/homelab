---
template: home.html
hide:
  - navigation
  - toc
---

<div class="cards">

  <a href="architecture/" class="doc-card" style="border-top-color:#c6a0f6;--card-accent:rgba(198,160,246,0.08);">
    <h3 style="color:#c6a0f6;">⎔ Architecture</h3>
    <p>UDM SE, 10GbE core switch, Proxmox hypervisor, and DGX Spark — four physical hosts driving the entire stack.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">Proxmox</span>
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">Swarm</span>
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">VLANs</span>
    </div>
  </a>

  <a href="storage/" class="doc-card" style="border-top-color:#8aadf4;--card-accent:rgba(138,173,244,0.08);">
    <h3 style="color:#8aadf4;">⬡ Storage</h3>
    <p>TrueNAS ZFS pool with tiered datasets, NFS exports to compute nodes, and co-located database engines.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(138,173,244,0.12);color:#8aadf4;">ZFS</span>
      <span class="tag" style="background:rgba(138,173,244,0.12);color:#8aadf4;">NFS</span>
      <span class="tag" style="background:rgba(138,173,244,0.12);color:#8aadf4;">MinIO</span>
    </div>
  </a>

  <a href="iac/" class="doc-card" style="border-top-color:#eed49f;--card-accent:rgba(238,212,159,0.08);">
    <h3 style="color:#eed49f;">⚙ IaC Pipeline</h3>
    <p>Three-stage pipeline: Packer base images, OpenTofu VM provisioning, Ansible configuration and stack deployment.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">Packer</span>
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">OpenTofu</span>
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">Ansible</span>
    </div>
  </a>

  <a href="certificates/" class="doc-card" style="border-top-color:#7dc4e4;--card-accent:rgba(125,196,228,0.08);">
    <h3 style="color:#7dc4e4;">🔒 Certificates</h3>
    <p>Let's Encrypt DNS-01 via Cloudflare for every service — Traefik, Proxmox, PBS, and TrueNAS all auto-renew.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(125,196,228,0.12);color:#7dc4e4;">ACME</span>
      <span class="tag" style="background:rgba(125,196,228,0.12);color:#7dc4e4;">DNS-01</span>
      <span class="tag" style="background:rgba(125,196,228,0.12);color:#7dc4e4;">Cloudflare</span>
    </div>
  </a>

  <a href="monitoring/" class="doc-card" style="border-top-color:#a6da95;--card-accent:rgba(166,218,149,0.08);">
    <h3 style="color:#a6da95;">◈ Monitoring</h3>
    <p>Dedicated VM running Prometheus, Loki, and Grafana with full exporter coverage and Gotify push alerting.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Prometheus</span>
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Loki</span>
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Grafana</span>
    </div>
  </a>

  <a href="gitea/" class="doc-card" style="border-top-color:#8bd5ca;--card-accent:rgba(139,213,202,0.08);">
    <h3 style="color:#8bd5ca;">⎇ Gitea</h3>
    <p>Self-hosted Git with GitHub mirror sync every 10 min, Actions CI on a dedicated LXC runner, shared Postgres.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(139,213,202,0.12);color:#8bd5ca;">Git</span>
      <span class="tag" style="background:rgba(139,213,202,0.12);color:#8bd5ca;">CI/CD</span>
      <span class="tag" style="background:rgba(139,213,202,0.12);color:#8bd5ca;">Actions</span>
    </div>
  </a>

  <a href="sso/" class="doc-card" style="border-top-color:#b7bdf8;--card-accent:rgba(183,189,248,0.08);">
    <h3 style="color:#b7bdf8;">🔑 SSO</h3>
    <p>Authentik as identity provider, Authelia as Traefik forward-auth middleware — single user store, OIDC everywhere.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(183,189,248,0.12);color:#b7bdf8;">Authentik</span>
      <span class="tag" style="background:rgba(183,189,248,0.12);color:#b7bdf8;">OIDC</span>
      <span class="tag" style="background:rgba(183,189,248,0.12);color:#b7bdf8;">Authelia</span>
    </div>
  </a>

</div>
