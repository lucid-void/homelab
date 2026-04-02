---
template: home.html
hide:
  - navigation
  - toc
---

<div class="cards">

  <a href="stack/" class="doc-card" style="border-top-color:#c6a0f6;--card-accent:rgba(198,160,246,0.08);">
    <h3 style="color:#c6a0f6;">⎔ The Stack</h3>
    <p>Four physical hosts, seven VMs, and a Docker Swarm overlay — the hardware, network, and services that power everything.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">Proxmox</span>
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">Swarm</span>
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">ZFS</span>
      <span class="tag" style="background:rgba(198,160,246,0.12);color:#c6a0f6;">NFS</span>
    </div>
  </a>

  <a href="automation/" class="doc-card" style="border-top-color:#eed49f;--card-accent:rgba(238,212,159,0.08);">
    <h3 style="color:#eed49f;">⚙ Automation</h3>
    <p>Three-stage IaC pipeline: Packer images, OpenTofu provisioning, Ansible configuration. Gitea Actions CI on a dedicated runner.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">Packer</span>
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">OpenTofu</span>
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">Ansible</span>
      <span class="tag" style="background:rgba(238,212,159,0.12);color:#eed49f;">Gitea</span>
    </div>
  </a>

  <a href="operations/" class="doc-card" style="border-top-color:#a6da95;--card-accent:rgba(166,218,149,0.08);">
    <h3 style="color:#a6da95;">◈ Operations</h3>
    <p>Prometheus, Loki, and Grafana on a dedicated VM. ACME certificates, SSO with Authentik, and Gotify push alerting.</p>
    <div class="tags">
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Prometheus</span>
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Grafana</span>
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">Authentik</span>
      <span class="tag" style="background:rgba(166,218,149,0.12);color:#a6da95;">ACME</span>
    </div>
  </a>

</div>
