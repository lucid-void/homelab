# Jellyfin Intel Quick Sync Hardware Transcoding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass the Proxmox host's Intel iGPU through to the cp-2 Talos VM and wire it into Jellyfin so transcodes use Quick Sync (VAAPI/QSV) instead of software x264.

**Architecture:** Three layers, each a separate git-tracked config surface: an OpenTofu `hostpci` block passes the whole iGPU (via the pre-created `IGPU` Proxmox Resource Mapping) to the cp-2 VM only; a `siderolabs/i915` Talos system extension on cp-2 only makes `/dev/dri/renderD128` appear inside that VM; Jellyfin's HelmRelease hostPath-mounts that device with a matching supplemental group. Every layer is applied through this repo's normal path (OpenTofu by hand, Talos via `talosctl`, Kubernetes via git+Flux) — nothing here is `kubectl apply`d directly.

**Tech Stack:** OpenTofu (`bpg/proxmox` provider), Talos Linux (`talhelper`, `talosctl`), FluxCD, bjw-s `app-template` Helm chart v3.7.3.

**Spec:** `docs/superpowers/specs/2026-09-25-jellyfin-quicksync-design.md`

## Global Constraints

- OpenTofu changes are **applied by hand** (`just plan` then `just apply`) — never scripted, never auto-approved.
- Kubernetes config changes go through **git + Flux only** — `kubectl` is diagnostics-only, never `kubectl apply` for a config change.
- All Talos/kubectl/helm/kubeconform tooling runs through `mise exec --` — it is not on `PATH`.
- Use `mapping = "IGPU"` for the `hostpci` block, never a raw PCI `id` — the Proxmox provider here authenticates with `api_token`, and `id` requires root username/password instead.
- `siderolabs/i915` goes on **cp-2's** schematic only — cp-1, cp-3, and llm-1 must not change.
- Jellyfin's `resources.limits` keeps **no CPU limit** — this does not change; QSV doesn't cover every codec path, so software fallback still needs headroom.
- Jellyfin's config PVC stays pinned to cp-2 — nothing in this plan moves it.
- After `.agents/scripts/validate-manifests.sh <file>` on every Kubernetes YAML edit before committing.

## Review Focus

- A Talos extension change requires a `talosctl upgrade` (new installer image), not just `apply-config` — that reboots cp-2, which restarts every pod pinned there (Jellyfin included). An operator who only reads "add an extension" could reasonably expect a config-only, no-reboot change. → covered in Task 2 (pre-check what's on cp-2, expect the interruption).
- Enabling hardware transcoding could regress the software-fallback path (subtitle burn-in and any codec QSV doesn't cover) if it's never exercised after the change. → covered in Task 5 (explicit software-path transcode after HW is enabled).
- Quick Sync iGPUs cap simultaneous hardware encode sessions (commonly around 2–3 on consumer parts); a second/third concurrent transcode may silently fail rather than falling back to software. → covered in Task 5 (concurrent-transcode check).
- The render-group gid is hardcoded from a live read (spec's own risk); nothing re-checks it after a future Talos upgrade touches cp-2, so it can drift silently. → covered in Task 4 (the verify command is written into `design/decisions/jellyfin.md` as a standing post-upgrade check, not just run once here).
- If the hostPath device target is momentarily absent (e.g., mid-reboot, or before Task 2/3 land), the pod's failure mode isn't previously documented — an operator debugging a stuck Jellyfin pod later needs to recognize this symptom instead of treating it as a mystery. → covered in Task 4 (observe and record the failure mode before the device exists).

---

### Task 1: Proxmox — OpenTofu iGPU passthrough to cp-2

**Files:**
- Modify: `infra/terraform/kubernetes.tf:22-118` (the `k8s_nodes` local map and the `proxmox_virtual_environment_vm` resource, `infra/terraform/kubernetes.tf:125-179`)

**Interfaces:**
- Produces: cp-2's Proxmox VM (`vm_id = 2021`) gains PCI device `hostpci0` bound to the `IGPU` resource mapping. No other node changes.

- [ ] **Step 1: Add a `hostpci` field to every node in the `k8s_nodes` map**

Edit the map so each node carries an explicit (mostly empty) `hostpci` list — only `cp-2` gets an entry:

```hcl
    cp-1 = {
      vm_id       = 2020
      ip_last     = 11
      vcpus       = 8
      memory      = 30720
      disk_gb     = 100
      mac_address = "BC:24:11:01:20:00"
      tags        = ["k8s_cp"]
      dns_records = []
      hostpci     = []
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
      # Intel iGPU passthrough for Jellyfin Quick Sync — see
      # design/decisions/jellyfin.md. "IGPU" is a Proxmox Resource Mapping
      # (Datacenter > Resource Mappings), not a raw PCI id: `id` requires
      # root user/pass on the provider, and this provider authenticates
      # with api_token.
      hostpci     = ["IGPU"]
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
      hostpci     = []
    }
```

And on `llm-1` (keep its existing comment block above untouched, just add the field to the map literal itself):

```hcl
    llm-1 = {
      vm_id       = 2014
      ip_last     = 14
      vcpus       = 8
      memory      = 71680
      disk_gb     = 250
      mac_address = "BC:24:11:01:23:00"
      tags        = ["k8s_worker"]
      dns_records = []
      hostpci     = []
    }
```

- [ ] **Step 2: Add a `dynamic "hostpci"` block to the VM resource**

In `resource "proxmox_virtual_environment_vm" "k8s_nodes"`, add this block after the existing `network_device` block and before the `agent` block:

```hcl
  dynamic "hostpci" {
    for_each = { for idx, mapping in each.value.hostpci : idx => mapping }
    content {
      device  = "hostpci${hostpci.key}"
      mapping = hostpci.value
      pcie    = true   # matches this VM's q35 machine type (OVMF/UEFI)
      xvga    = false  # not needed as primary display — only /dev/dri matters
    }
  }
```

- [ ] **Step 3: Validate and plan**

```bash
cd infra/terraform && tofu validate
just plan
just show
```

Expected: `tofu validate` reports no errors. `just show` shows exactly one resource change — `proxmox_virtual_environment_vm.k8s_nodes["cp-2"]` gaining a `hostpci` block — and no changes to cp-1, cp-3, or llm-1.

- [ ] **Step 4: Apply by hand**

```bash
just apply
```

Confirm the prompt shows the same single-resource change from Step 3 before typing `yes`.

- [ ] **Step 5: Verify the device is enumerated inside the cp-2 VM**

```bash
mise exec -- talosctl dmesg --nodes 172.16.20.12 --talosconfig kubernetes/talos/clusterconfig/talosconfig | grep -i "0000:00:02.0"
```

Expected: at least one kernel log line showing PCI device `0000:00:02.0` being enumerated at boot (device is visible to the VM's kernel even though no driver claims it yet — that's Task 2).

- [ ] **Step 6: Commit**

```bash
git add infra/terraform/kubernetes.tf
git commit -m "feat(infra): pass Intel iGPU through to cp-2 for Jellyfin Quick Sync"
```

---

### Task 2: Talos — add the `siderolabs/i915` extension to cp-2

**Files:**
- Modify: `kubernetes/talos/talconfig.yaml:108-115` (cp-2's `schematic.customization.systemExtensions.officialExtensions`)
- Modify: `design/architecture.md` (Talos Extensions table)

**Interfaces:**
- Consumes: cp-2 VM already has the iGPU passed through (Task 1).
- Produces: `/dev/dri/renderD128` exists inside cp-2 after this task, for Task 4 to mount.

- [ ] **Step 1: Check what else is pinned to cp-2 before rebooting it**

A Talos extension change requires a full node upgrade (new installer image), which reboots the node — every pod on cp-2 restarts, not just Jellyfin.

```bash
mise exec -- kubectl get pods -A --field-selector spec.nodeName=cp-2 -o wide
```

Note the output. This is expected to include Jellyfin and whatever else Kubernetes has scheduled there (etcd/control-plane static pods restart as part of any node reboot regardless of extensions). Nothing to fix here — just confirms the blast radius before proceeding, and gives you the list to sanity-check against once the node rejoins.

- [ ] **Step 2: Add the extension to cp-2 only**

In `kubernetes/talos/talconfig.yaml`, cp-2's node block:

```yaml
    schematic:
      customization:
        systemExtensions:
          officialExtensions:
            - siderolabs/lldpd
            - siderolabs/netbird
            # - siderolabs/nut-client
            - siderolabs/qemu-guest-agent
            - siderolabs/i915
```

Do **not** add this to cp-1, cp-3, or llm-1's blocks.

- [ ] **Step 3: Regenerate node configs**

```bash
mise exec -- talhelper genconfig
```

- [ ] **Step 4: Get cp-2's new installer image URL**

```bash
grep "metal-installer" kubernetes/talos/clusterconfig/homelab-k8s-cp-2.yaml
```

Expected: a line like `image: factory.talos.dev/metal-installer/<new-schematic-id>:v1.13.2`. Note the schematic id — the schematic changed because the extension list changed. Use `installer` (not `metal-installer`) in the next step's `--image` flag, per this repo's established upgrade procedure.

- [ ] **Step 5: Upgrade cp-2 to the new image**

```bash
mise exec -- talosctl upgrade \
  --nodes 172.16.20.12 \
  --image factory.talos.dev/installer/<schematic-id>:v1.13.2 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --drain=false
```

Wait for the node to reboot and rejoin:

```bash
mise exec -- kubectl get nodes --watch
```

Expected: `cp-2` goes `NotReady` then back to `Ready`.

- [ ] **Step 6: Verify `/dev/dri` exists on cp-2**

```bash
mise exec -- talosctl list /dev/dri --nodes 172.16.20.12 --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

Expected: `renderD128` (and typically `card0`) listed.

- [ ] **Step 7: Update the extensions table doc**

In `design/architecture.md`, extend the Talos Extensions table:

```markdown
| Extension | Purpose |
|---|---|
| `siderolabs/qemu-guest-agent` | Proxmox VM management (clean shutdown, snapshots) |
| `siderolabs/lldpd` | LLDP neighbour discovery for switch port mapping |
| `siderolabs/netbird` | WireGuard mesh VPN — remote access without port forwarding |
| `siderolabs/nut-client` | UPS monitoring (disabled — no UPS yet) |
| `siderolabs/i915` | Intel iGPU driver — **cp-2 only**, for Jellyfin Quick Sync (`design/decisions/jellyfin.md`) |
```

- [ ] **Step 8: Commit**

```bash
git add kubernetes/talos/talconfig.yaml kubernetes/talos/clusterconfig design/architecture.md
git commit -m "feat(talos): add i915 extension to cp-2 for Jellyfin Quick Sync"
```

---

### Task 3: Reset-bug maintenance-window test

**Files:** none (operational verification only — no code change)

**Interfaces:**
- Consumes: `/dev/dri` exists on cp-2 (Task 2).
- Produces: a pass/fail answer on whether cp-2 can be rebooted alone without wedging the iGPU, gating whether Task 4/5 are safe to rely on in production.

Known Intel iGPU passthrough failure mode: rebooting the **VM** (not the host) can leave the device stuck — `/dev/dri` never reappears, kernel logs show `MMIO access returns 0xFFFFFFFF` — until the Proxmox **host** itself reboots. Since cp-2 is a control-plane/etcd member, confirm this now, deliberately, in a maintenance window, rather than discovering it during an unrelated future cp-2 reboot.

- [ ] **Step 1: Reboot cp-2 alone (not the Proxmox host)**

```bash
mise exec -- talosctl reboot --nodes 172.16.20.12 --talosconfig kubernetes/talos/clusterconfig/talosconfig
mise exec -- kubectl get nodes --watch
```

Expected: `cp-2` goes `NotReady` then `Ready` again.

- [ ] **Step 2: Confirm `/dev/dri` survived the reboot**

```bash
mise exec -- talosctl list /dev/dri --nodes 172.16.20.12 --talosconfig kubernetes/talos/clusterconfig/talosconfig
mise exec -- talosctl dmesg --nodes 172.16.20.12 --talosconfig kubernetes/talos/clusterconfig/talosconfig | grep -i "0000:00:02.0"
```

Expected: `renderD128` still listed, and no `MMIO access returns 0xFFFFFFFF` or device-initialization-failure lines in dmesg.

- [ ] **Step 3: Branch on the result**

If Step 2 passes: proceed to Task 4. This risk is now measured, not theoretical — no further action needed, but keep in mind any *future* cp-2 reboot (Talos upgrades, maintenance) carries the same small risk and should be watched the same way.

If Step 2 fails (device stuck): **stop here.** Do not proceed to Task 4/5. This means the design's assumption doesn't hold on this hardware, and the spec's mitigation (accept host-only reboots for cp-2) needs to become the plan — reboot the Proxmox host to clear the stuck device, then bring this finding back before continuing, since it changes how every future cp-2 maintenance window has to be run.

---

### Task 4: Kubernetes — mount `/dev/dri` into Jellyfin

**Files:**
- Modify: `kubernetes/apps/media/jellyfin/app/helmrelease.yml`
- Modify: `design/decisions/jellyfin.md`

**Interfaces:**
- Consumes: `/dev/dri/renderD128` exists on cp-2 and survives a reboot (Tasks 2–3).
- Produces: Jellyfin container can read `/dev/dri/renderD128`. Task 5 turns this into an actual hardware transcode.

- [ ] **Step 1: Add the hostPath mount without a supplemental group yet**

In `kubernetes/apps/media/jellyfin/app/helmrelease.yml`, under `values.persistence`, add:

```yaml
      # Intel iGPU render node, passed through to cp-2 only (Task 1/2 of
      # design/decisions/jellyfin.md's Quick Sync work). hostPath, not a PVC —
      # this is a device node, not storage.
      render:
        type: hostPath
        hostPath: /dev/dri
        hostPathType: Directory
        globalMounts:
          - path: /dev/dri
```

- [ ] **Step 2: Validate the manifest**

```bash
.agents/scripts/validate-manifests.sh kubernetes/apps/media/jellyfin/app/helmrelease.yml
```

Expected: exits 0.

- [ ] **Step 3: Commit and let Flux reconcile**

```bash
git add kubernetes/apps/media/jellyfin/app/helmrelease.yml
git commit -m "feat(jellyfin): mount /dev/dri for Quick Sync (gid pending)"
mise exec -- flux reconcile kustomization jellyfin -n flux-system --with-source
```

(Substitute the actual Flux Kustomization name for Jellyfin if it differs — check with `mise exec -- flux get kustomizations -A | grep jellyfin` first if unsure.)

- [ ] **Step 4: Observe the pod's behavior with the device present but no group access**

```bash
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=jellyfin -o wide
mise exec -- kubectl describe pod -n media -l app.kubernetes.io/name=jellyfin
```

Expected: pod starts fine (hostPath type `Directory` just needs the directory to exist, which it does) — this confirms and documents the failure mode for later: if `/dev/dri` were *absent* on the node (e.g., Task 2 not yet applied), the symptom would be the pod stuck in `ContainerCreating` with a `FailedMount` event citing the hostPath path, not a crash loop. Worth knowing before this is the thing being debugged at 2am.

- [ ] **Step 5: Read the actual gid of the render device from inside the container**

```bash
mise exec -- kubectl exec -n media deploy/jellyfin -- stat -c '%g' /dev/dri/renderD128
```

Note the number returned — this is the real host-side render group gid, not a guess.

- [ ] **Step 6: Add the supplemental group using the number from Step 5**

In `kubernetes/apps/media/jellyfin/app/helmrelease.yml`, under `values.controllers.app.pod` (alongside the existing `nodeSelector`):

```yaml
        pod:
          # Pinned to cp-2 because plex-config-local is on cp-1. The whole point of
          # this deployment is surviving the loss of the node Plex's library sits on,
          # and openebs-hostpath would otherwise happily put both on the same disk.
          nodeSelector:
            kubernetes.io/hostname: cp-2
          # Render group gid on cp-2, read live via `stat -c '%g' /dev/dri/renderD128`
          # inside the container — do not guess this number, and re-check it after
          # any Talos upgrade that touches cp-2 (the gid is not guaranteed stable
          # across i915 extension or Talos version changes).
          securityContext:
            supplementalGroups: [<gid-from-step-5>]
```

- [ ] **Step 7: Validate, commit, reconcile**

```bash
.agents/scripts/validate-manifests.sh kubernetes/apps/media/jellyfin/app/helmrelease.yml
git add kubernetes/apps/media/jellyfin/app/helmrelease.yml
git commit -m "feat(jellyfin): grant render-group access for Quick Sync"
mise exec -- flux reconcile kustomization jellyfin -n flux-system --with-source
```

- [ ] **Step 8: Confirm read access from inside the container as the app user**

```bash
mise exec -- kubectl exec -n media deploy/jellyfin -- sh -c 'id; test -r /dev/dri/renderD128 && echo READABLE || echo DENIED'
```

Expected: `READABLE`. `id` output should show PUID 2202 plus the render gid in the supplemental groups list.

- [ ] **Step 9: Update the stale "CPU-only" comment in the HelmRelease itself**

In `kubernetes/apps/media/jellyfin/app/helmrelease.yml`, the `resources.limits` block currently reads:

```yaml
              limits:
                # No GPU device plugin exists in this cluster, so every transcode is
                # software on the Arrow Lake P-cores. Deliberately no CPU limit:
                # throttling a transcode produces buffering, not a clean failure.
                memory: 6Gi
```

Replace the comment (keep `memory: 6Gi` as-is):

```yaml
              limits:
                # Quick Sync (VAAPI) handles most transcodes now — see the `render`
                # persistence entry above and design/decisions/jellyfin.md. Still no
                # CPU limit: QSV doesn't cover every codec path, software fallback
                # runs on the Arrow Lake P-cores, and throttling it produces
                # buffering, not a clean failure.
                memory: 6Gi
```

- [ ] **Step 10: Update `design/decisions/jellyfin.md`**

Replace this line:

```markdown
Transcoding is CPU-only: no GPU device plugin exists, and the Talos VMs get no iGPU
passthrough from the Proxmox host.
```

with:

```markdown
Transcoding uses Intel Quick Sync (VAAPI/QSV) via `/dev/dri`, hostPath-mounted from
cp-2's passed-through iGPU (`design/architecture.md`'s Talos Extensions table,
`infra/terraform/kubernetes.tf`'s `hostpci` block on cp-2). Software transcoding is
still the fallback for codecs QSV doesn't cover — the container keeps no CPU limit
for that reason.

The render-group gid in the HelmRelease's `supplementalGroups` was read live off
cp-2 (`stat -c '%g' /dev/dri/renderD128` inside the pod), not guessed. **Re-run that
check after any Talos upgrade touching cp-2** — nothing pins this gid stable across
an `i915` extension or Talos version change, and a silent mismatch means transcodes
quietly stop using hardware instead of failing loudly.
```

Also update the `Rules` section's CPU-limit rule to note the QSV fallback reasoning stays valid (no wording change needed if it already says "software" generically — check it reads correctly against the new context).

- [ ] **Step 11: Commit the docs update**

```bash
git add kubernetes/apps/media/jellyfin/app/helmrelease.yml design/decisions/jellyfin.md
git commit -m "docs(jellyfin): document Quick Sync transcoding setup"
```

---

### Task 5: Enable Quick Sync in Jellyfin and verify end to end

**Files:** none (Jellyfin UI config is runtime state on the config PVC, not git — same as the SSO plugin settings documented in `design/decisions/jellyfin.md`)

**Interfaces:**
- Consumes: `/dev/dri/renderD128` readable inside the Jellyfin container (Task 4).
- Produces: working hardware-accelerated transcoding, confirmed end to end, with software fallback confirmed intact.

- [ ] **Step 1: Enable VAAPI in Jellyfin's dashboard**

Navigate to `https://jellyfin.blackcats.cc` → Dashboard → Playback. Set:
- Hardware acceleration: `Video Acceleration API (VAAPI)`
- VA API Device: `/dev/dri/renderD128`
- Enable hardware decoding for the codecs you expect to see (H.264, HEVC, etc.)
- Enable hardware encoding

Save.

- [ ] **Step 2: Force a hardware transcode and confirm it's using the iGPU**

Play a file on a client/profile that won't direct-play (e.g., force a resolution/bitrate change, or use a client codec Jellyfin must transcode). While it's playing:

```bash
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=jellyfin -o wide   # confirm on cp-2
```

On the Proxmox host, during the same transcode:

```bash
intel_gpu_top
```

Expected: the Jellyfin dashboard's active-transcode panel shows `(hw)` next to the video codec, and `intel_gpu_top` shows non-zero engine activity (Video/VideoEnhance) for the duration of the transcode.

- [ ] **Step 3: Compare CPU usage against a software transcode baseline**

```bash
mise exec -- kubectl top pod -n media -l app.kubernetes.io/name=jellyfin
```

Sample this during the Step 2 hardware transcode, then temporarily force software transcoding (Dashboard → Playback → set Hardware acceleration back to `None`) and repeat the same file/profile, sampling again. Expected: hardware-transcode CPU usage is substantially lower than the software baseline. Re-enable VAAPI afterward.

- [ ] **Step 4: Confirm the software fallback path still works**

With VAAPI re-enabled, force a transcode Jellyfin is expected to route through software regardless (e.g., a subtitle burn-in scenario, or a codec not checked in Step 1's hardware-decode list). Expected: it still completes and plays — confirms enabling QSV didn't break the CPU fallback path that `resources.limits` (no CPU cap) exists to support.

- [ ] **Step 5: Check concurrent-transcode behavior**

Start two simultaneous transcodes that both require hardware transcoding (two different clients/files, both forced off direct-play). Expected: both either succeed via hardware, or the second visibly falls back to software (check its dashboard entry for `(hw)` vs no tag) rather than failing outright. Note whichever happens — Quick Sync iGPUs commonly cap concurrent hardware sessions (often 2–3 on consumer parts), so a clean fallback is the acceptable outcome, a hard failure is not. If it's a hard failure, that's worth a follow-up note in `design/decisions/jellyfin.md`, not a blocker for this plan.

- [ ] **Step 6: Final verification pass**

```bash
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=jellyfin -o wide
curl -sf https://jellyfin.blackcats.cc/health && echo
```

Expected: pod `Running` on cp-2, health check returns success. This closes out the spec's Testing/Verification section.
