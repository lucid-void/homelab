---
template: home.html
hide:
  - navigation
  - toc
---

<div class="cards">

  <a href="architecture/" class="doc-card" style="border-top-color:#c6a0f6;">
    <h3 style="color:#c6a0f6;">⎔ Architecture</h3>
    <p>Hardware inventory, VLAN layout, Proxmox VM design, and Docker Swarm topology.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">Proxmox</span>
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">Swarm</span>
    </div>
  </a>

  <a href="storage/" class="doc-card" style="border-top-color:#8aadf4;">
    <h3 style="color:#8aadf4;">⬡ Storage</h3>
    <p>TrueNAS ZFS dataset tree, NFS exports, MinIO S3 backend, and backup strategy.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(138,173,244,0.12);color:#8aadf4;">ZFS</span>
      <span class="tag" style="background:rgba(138,173,244,0.12);color:#8aadf4;">NFS</span>
    </div>
  </a>

  <a href="iac/" class="doc-card" style="border-top-color:#eed49f;">
    <h3 style="color:#eed49f;">⚙ IaC Pipeline</h3>
    <p>Three-stage pipeline: Packer templates, OpenTofu provisioning, Ansible configuration.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">OpenTofu</span>
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">Ansible</span>
    </div>
  </a>

  <a href="certificates/" class="doc-card" style="border-top-color:#7dc4e4;">
    <h3 style="color:#7dc4e4;">🔒 Certificates</h3>
    <p>Let's Encrypt DNS-01 via Cloudflare for Traefik, Proxmox, PBS, and TrueNAS.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(125,196,228,0.12);color:#7dc4e4;">TLS</span>
      <span class="tag" style="background:rgba(125,196,228,0.12);color:#7dc4e4;">ACME</span>
    </div>
  </a>

  <a href="monitoring/" class="doc-card" style="border-top-color:#a6da95;">
    <h3 style="color:#a6da95;">◈ Monitoring</h3>
    <p>Prometheus + Loki + Grafana stack, full exporter set, and Gotify alerting.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">PLG</span>
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Alerting</span>
    </div>
  </a>

  <a href="gitea/" class="doc-card" style="border-top-color:#8bd5ca;">
    <h3 style="color:#8bd5ca;">⎇ Gitea</h3>
    <p>Self-hosted Git service on the Swarm, GitHub mirror sync, Actions CI, and shared Postgres.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(139,213,202,0.12);color:#8bd5ca;">Git</span>
      <span class="tag" style="background:rgba(139,213,202,0.12);color:#8bd5ca;">CI</span>
    </div>
  </a>

  <a href="sso/" class="doc-card" style="border-top-color:#b7bdf8;">
    <h3 style="color:#b7bdf8;">🔑 SSO</h3>
    <p>Authentik as the identity provider with Authelia forward-auth middleware for Traefik.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(183,189,248,0.12);color:#b7bdf8;">Authentik</span>
      <span class="tag" style="background:rgba(183,189,248,0.12);color:#b7bdf8;">OIDC</span>
    </div>
  </a>

</div>
