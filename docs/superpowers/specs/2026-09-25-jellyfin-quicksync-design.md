# Jellyfin Intel Quick Sync hardware transcoding — design

Date: 2026-09-25
Status: approved, not yet implemented

## Problem

Jellyfin transcoding is CPU-only today (`design/decisions/jellyfin.md`). No GPU
device plugin exists in the cluster and no Talos VM gets iGPU passthrough from
the Proxmox host, so every transcode runs in software on the Arrow Lake
P-cores. This is the same state Plex is in (`design/decisions/plex.md`).

The Proxmox host's Ultra 5 235HX (Arrow Lake-S die, HX package) carries a
working Intel iGPU (`8086:7d67`, Xe-LPG), already claimed by `i915` on the
host, confirmed alone in its own IOMMU group (group 0 — no other devices
entangled). That's enough to pass the whole device through to one VM for
Quick Sync (QSV) hardware transcoding.

## Goals

1. Jellyfin transcodes use Quick Sync (VAAPI/QSV) instead of software x264.
2. No change to which node Jellyfin runs on — it stays pinned to cp-2
   (its config PVC already lives there and cannot move without a full
   library re-scan; see `design/decisions/jellyfin.md`).
3. Change lands through git + Flux/Talhelper/OpenTofu, per repo convention —
   no ad-hoc `kubectl apply` or manual Proxmox VM edits outside Terraform.

## Non-goals

- Plex hardware transcoding. Same missing capability, deliberately out of
  scope — Plex's config PVC is pinned to cp-1, a different node, and the
  iGPU can only be passed through to one VM. Revisit only if Plex needs to
  move off cp-1 for other reasons.
- SR-IOV / multi-VM GPU sharing. Investigated and rejected — SR-IOV virtual
  functions for this class of iGPU need an out-of-tree DKMS driver, which
  Talos's immutable OS model cannot install. Full single-VM passthrough of
  the physical device uses the in-tree `i915` driver instead and doesn't
  hit that wall.
- Moving cp-2's control-plane/etcd role off the node to reduce blast radius
  of the reset-bug risk (see Risks). Accepted as-is for this change.

## Architecture

Three layers, each already a distinct trust boundary in this repo:

```
Proxmox host (172.16.20.3)
  Intel iGPU 0000:00:02.0 ── Resource Mapping "IGPU" (already created)
        │
        │ hostpci passthrough (OpenTofu, applied by hand)
        ▼
cp-2 VM (172.16.20.12) — Talos, OVMF/UEFI, q35
  siderolabs/i915 extension → /dev/dri/renderD128 appears
        │
        │ hostPath mount (Flux-managed HelmRelease)
        ▼
Jellyfin pod (media namespace, pinned to cp-2)
  VAAPI/QSV enabled in Jellyfin's transcoding settings
```

Only cp-2 gets the extension and the device — cp-1, cp-3 and llm-1 are
untouched. The iGPU has exactly one owner VM; that's cp-2 today because
Jellyfin already lives there, not a new constraint this change introduces.

## Components

### 1. Proxmox / OpenTofu

`infra/terraform/kubernetes.tf`, `k8s_nodes.cp-2` gets a `hostpci` block on
the `proxmox_virtual_environment_vm` resource:

```hcl
hostpci {
  device  = "hostpci0"
  mapping = "IGPU"   # Resource Mapping, not a raw PCI id
  pcie    = true      # q35 + OVMF, matches the rest of the VM
  xvga    = false      # not needed as primary display — only /dev/dri matters
}
```

`mapping` (not `id`) is required: the `id` field needs root username/password
on the Proxmox provider, and this repo's provider auth is `api_token`
(`infra/terraform/provider.tf`) — switching auth for one resource is a
needless credential-scope regression. The `IGPU` Resource Mapping already
exists (created by hand in the Proxmox UI, Datacenter → Resource Mappings).

Applied by hand, same as every other OpenTofu change here — not something
Flux touches.

### 2. Talos

`kubernetes/talos/talconfig.yaml`, cp-2's node block only:

```yaml
schematic:
  customization:
    systemExtensions:
      officialExtensions:
        - siderolabs/lldpd
        - siderolabs/netbird
        - siderolabs/qemu-guest-agent
        - siderolabs/i915
```

Regenerate with `talhelper genconfig` and apply to cp-2 alone via
`talosctl apply-config` (or the repo's normal Talos apply path) — not a
cluster-wide schematic change.

### 3. Kubernetes (Jellyfin HelmRelease)

`kubernetes/apps/media/jellyfin/app/helmrelease.yml`:

- Add a `hostPath` persistence entry mounting `/dev/dri` into the container
  (bjw-s app-template `type: hostPath`), read-write, alongside the existing
  `config`/`transcodes`/`media` entries.
- Add `pod.securityContext.supplementalGroups` with the host's render-group
  gid. **Do not guess this number** — read it off cp-2 once `/dev/dri`
  exists (`stat -c '%g' /dev/dri/renderD128` inside the node or a debug
  pod) and hardcode the confirmed value, matching how PUID/PGID are already
  pinned explicit integers in this file rather than left to chart defaults.
- Enable VAAPI/QSV in Jellyfin's own transcoding settings (Dashboard →
  Playback) — this is runtime config on the config PVC, like the SSO
  plugin settings, not something git can express. Document the setting in
  `design/decisions/jellyfin.md` the same way the SSO plugin's UI-only
  quirks are documented there now.
- Update the resources-block comment (`helmrelease.yml:86-88`) and
  `design/decisions/jellyfin.md`'s "Transcoding is CPU-only" line — both
  currently assert no GPU exists; both go stale the moment this lands
  (repo's docs-sync rule: reality wins, update the one file the routing
  table points to).
- No change to `resources.limits` — still no CPU limit, per the existing
  rule; a QSV-capable transcode can still fall back to software for codecs
  QSV doesn't cover (e.g. certain subtitle burn-in paths), so the no-limit
  rule still applies.

## Risks

- **iGPU passthrough reset bug.** Known Intel iGPU passthrough failure
  mode on Proxmox/QEMU: a VM reboot (not a host reboot) can leave the
  device stuck — `MMIO access returns 0xFFFFFFFF`, `/dev/dri` never
  reappears — until the **Proxmox host** itself reboots. Because cp-2 is a
  control-plane/etcd member, a stuck-device recovery means bouncing the
  host, which bounces cp-2's etcd member alongside it (tolerable — 2 of 3
  still up — but real, coordinated downtime, not a Jellyfin-only blip).
  Mitigation: test a solo `cp-2` reboot (not the host) in a maintenance
  window before relying on this in production, and confirm `/dev/dri`
  survives it cleanly. If it doesn't, this design needs to be revisited
  (e.g. accept host-only reboots for cp-2 maintenance going forward) before
  calling it done.
- **Render-group gid drift.** If the `i915` extension or Talos version ever
  changes the render group's gid, the hardcoded `supplementalGroups` value
  silently stops matching and transcoding falls back to failing, not
  software — Jellyfin's VAAPI probe simply won't find a usable device.
  Not automatable without a hostPath-only workaround the repo has already
  chosen not to build a device plugin for; catch it via the verification
  steps below after any Talos upgrade that touches cp-2.

## Testing / Verification

1. After the OpenTofu apply: `lspci -nnk` **inside** the cp-2 VM shows
   `8086:7d67` bound to `i915` (confirms passthrough succeeded before
   touching Talos config).
2. After the Talos extension applies: `mise exec -- kubectl debug` (or
   `talosctl -n 172.16.20.12 ls /dev/dri`) shows `renderD128`.
3. After the HelmRelease change: exec into the Jellyfin pod, confirm
   `/dev/dri/renderD128` is visible and the container's process can open it
   (no permission-denied) — this is the supplemental-group check.
4. In the Jellyfin dashboard, start a transcode of a file whose target
   format isn't a direct-play match, and confirm the active-transcode
   panel shows `(hw)` rather than software. Cross-check with
   `intel_gpu_top` on the Proxmox host during the transcode — it should
   show engine activity while the transcode runs.
5. Confirm CPU usage during that same transcode stays low on cp-2 relative
   to a software transcode baseline — the practical proof this was worth
   doing.

## Rollback

Each layer rolls back independently and in reverse order if something
breaks partway:

- Kubernetes: revert the HelmRelease change (git revert, Flux reconciles).
  Jellyfin falls back to software transcoding immediately, no pod-restart
  side effects beyond the normal HelmRelease reconcile.
- Talos: remove `siderolabs/i915` from cp-2's schematic, regenerate, and
  re-apply to cp-2. `/dev/dri` disappears; any pod still requesting it will
  fail to mount and needs the Kubernetes-layer revert first.
- Proxmox: remove the `hostpci` block from `kubernetes.tf` and apply.
  Requires a cp-2 VM stop/start (hot-unplug of passed-through PCI devices
  isn't reliable) — coordinate with a maintenance window, same as the
  reset-bug test above.
