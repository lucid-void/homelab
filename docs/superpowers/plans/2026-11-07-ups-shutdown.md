# UPS Shutdown and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** after more than 2 min without mains power, shut the homelab down in groups (Talos cluster → Synology + Proxmox → Pi + UPS output), and boot everything back in order when power returns.

**Architecture:** The Pi (`nut-server`, 172.16.20.7) serves the Eaton UPS under two NUT names: `ups` (real `usbhid-ups` driver) and `ups-cluster` (a `dummy-ups` repeater). An `upssched` timer runs `/usr/local/sbin/ups-shutdown`. That script raises FSD on `ups-cluster` (Talos nodes power off through `siderolabs/nut-client`), waits for those clients to disconnect, then runs `upsmon -c fsd`. The standard NUT primary flow then shuts down the DSM + Proxmox secondaries, then the Pi, and finally sends killpower.

**Tech Stack:** NUT 2.8.1 (Debian 13 on the Pi), Talos `siderolabs/nut-client` extension via talhelper + SOPS, Synology DSM UPS client, Proxmox `nut-client`, nftables.

**Spec:** `docs/superpowers/specs/2026-11-07-ups-shutdown-design.md`

## Global Constraints

- Real UPS NUT name is exactly **`ups`** (DSM hardcodes it); group-1 name is exactly **`ups-cluster`**.
- DSM login is exactly **`monuser` / `secret`**, role secondary — not changeable.
- Port 3493 on the Pi is reachable only from 127.0.0.1/::1, 172.16.20.2, .3, .11, .12, .13, .14.
- Timer: **120 s** on battery before the shutdown starts; cancelled by ONLINE before it fires.
- Group-1 wait: at most **360 s**, then continue anyway. `HOSTSYNC 240`.
- Once the script has fired, it **finishes** even if power returns.
- Talos `SHUTDOWNCMD` is exactly `"/sbin/poweroff"`; secret in `kubernetes/talos/talenv.sops.yaml` as `NUT_UPSMON_PASSWORD`, never plain text.
- Talos upgrades: one node at a time, `--drain=false`, llm-1 first.
- Pi and Proxmox config is hand-managed, not in git. The repo gets only talconfig + docs.
- Never record deployed version numbers in `design/` files.
- Proxmox steps and DSM/BIOS steps are done **by the owner** (no Proxmox SSH from agent sessions).
- Each merge to `main` needs the owner's explicit go-ahead.

## Review Focus

1. **Power returns between 0 and 120 s** → nothing shuts down. Task 2 Step 4 tests the cancel.
2. **Power returns after the script fired** → the lock file stops a second run, and the sequence completes. Task 2 Step 5.
3. **A Talos node never disconnects (hung)** → the script moves on after 360 s and doesn't wait forever. Task 2 Step 6 (dry run with a fake client held open).
4. **FSD on `ups-cluster` must not shut the Pi down or touch the real `ups`** → Task 1 Step 3.
5. **The repeater must pass `OB`/`LB` through**, so Talos nodes still go down on a native low battery → Task 1 Step 4 (mirror) + Task 6 Step 2 (`OB` seen on `ups-cluster`).

---

### Task 1: Group names on the Pi (spike + rename)

**Files (Pi):**
- Modify: `/etc/nut/ups.conf` (rename `[eaton]` → `[ups]`, add `[ups-cluster]`)
- Modify: `/etc/nut/upsmon.conf` (`MONITOR ups@localhost …`)
- Backup: `/etc/nut.bak-2026-11-07/`

**Interfaces:**
- Produces: NUT UPS names `ups` and `ups-cluster` on `172.16.20.7:3493`; `admin` user can `FSD ups-cluster`.

- [ ] **Step 1:** `cp -a /etc/nut /etc/nut.bak-2026-11-07`
- [ ] **Step 2:** Edit `ups.conf` to the two sections from spec Step 1.1. Fix the `MONITOR` line to `ups@localhost`. Restart `nut-driver.target nut-server nut-monitor`.
  Verify: `upsc -l` → `ups` and `ups-cluster`; `upsc ups-cluster ups.status` → `OL`.
- [ ] **Step 3: FSD isolation test.** Over the NUT protocol, log in as `admin` and send `FSD ups-cluster`.
  Expected: `OK FSD-SET`; `upsc ups-cluster ups.status` contains `FSD`; `upsc ups ups.status` = `OL`; the Pi stays up and `journalctl -u nut-monitor` shows no shutdown. Then `systemctl restart nut-driver@ups-cluster` → `ups.status` back to `OL`.
  If FSD is refused: stop, report back (the group design depends on this).
- [ ] **Step 4: mirror check.** Confirm `upsc ups-cluster` mirrors `battery.charge`, `battery.runtime` and `ups.status` of `ups`. Real `OB`/`LB` pass-through can't be faked without pulling the plug, so Task 6 Step 2 checks it: `upsc ups-cluster ups.status` must show `OB` during the test.

### Task 2: Shutdown script, timer, users, firewall (Pi)

**Files (Pi):**
- Create: `/usr/local/sbin/ups-shutdown` (root 0750)
- Create/modify: `/etc/nut/upssched.conf`, `/etc/nut/upsmon.conf`, `/etc/nut/upsd.users`
- Create: `/etc/nftables.d/nut.nft` (or extend `/etc/nftables.conf`), `systemctl enable nftables`

**Interfaces:**
- Consumes: names from Task 1.
- Produces: `/usr/local/sbin/ups-shutdown <event>`. Handles event `ups-shutdown`; env `DRY_RUN=1` logs every action instead of FSD/`upsmon -c fsd`; `GROUP1_TIMEOUT` (default 360). Lock file `/run/ups-shutdown.lock`. Logs via `logger -t ups-shutdown`.
- Produces: upsd user `monuser`/`secret` (secondary). `upsmon-remote` is reused by Talos and Proxmox.

- [ ] **Step 1:** Add `monuser` to `upsd.users`. In `upsmon.conf` set `HOSTSYNC 240`, `NOTIFYCMD /usr/sbin/upssched`, `NOTIFYFLAG ONBATT SYSLOG+EXEC`, `NOTIFYFLAG ONLINE SYSLOG+EXEC`. Write `upssched.conf` with `CMDSCRIPT /usr/local/sbin/ups-shutdown`, `PIPEFN /run/nut/upssched.pipe`, `LOCKFN /run/nut/upssched.lock`, and the two `AT` lines from the spec (timer 120).
- [ ] **Step 2:** Write `ups-shutdown`: lock → log → FSD `ups-cluster` (admin password read from `/root/nut-credentials.txt`) → poll `upsc -c ups-cluster` every 5 s until empty or timeout → `upsmon -c fsd`. `set -u`, and nothing after the lock may `exit` early before `upsmon -c fsd`.
- [ ] **Step 3: dry run.** `DRY_RUN=1 GROUP1_TIMEOUT=10 /usr/local/sbin/ups-shutdown ups-shutdown`
  Expected in `journalctl -t ups-shutdown`: "would FSD ups-cluster", "0 clients on ups-cluster", "would run upsmon -c fsd". Exit 0.
- [ ] **Step 4: timer cancel.** Temporarily set the timer to 20 s and `CMDSCRIPT` to a wrapper that forces `DRY_RUN=1`. Run `UPSNAME=ups@localhost NOTIFYTYPE=ONBATT upssched`, wait 5 s, then `NOTIFYTYPE=ONLINE upssched`.
  Expected: no `ups-shutdown` log line within 30 s. Repeat without ONLINE → the dry-run lines appear after about 20 s.
- [ ] **Step 5: lock.** Run the dry run twice in parallel → the second logs "already running" and exits 0.
- [ ] **Step 6: hung client.** Hold a NUT login on `ups-cluster` from this workstation (`upsmon`-style `LOGIN ups-cluster` session kept open with `nc`). Run with `DRY_RUN=1 GROUP1_TIMEOUT=15`.
  Expected: "timeout, 1 client(s) still on ups-cluster, continuing", then "would run upsmon -c fsd".
- [ ] **Step 7:** Restore the timer to 120 s and the real `CMDSCRIPT`. nftables: accept tcp/3493 from the allowed list only (Global Constraints), drop the rest. Keep SSH open.
  Verify: from the workstation (not on the list) `nc -zv 172.16.20.7 3493` fails; SSH still works; `upsc ups@localhost` works on the Pi. Then temporarily add the workstation IP to check `LIST UPS` still works, and remove it again.

### Task 3: Talos nut-client (repo)

**Files:**
- Modify: `kubernetes/talos/talconfig.yaml` (top-level `patches:` near line 52; the four `officialExtensions` lists at 114, 146, 182, 230)
- Modify: `kubernetes/talos/talenv.sops.yaml` (add `NUT_UPSMON_PASSWORD`)

**Interfaces:**
- Consumes: `ups-cluster@172.16.20.7`, user `upsmon-remote` (Task 1–2).

- [ ] **Step 1:** On a branch `feat/ups-shutdown`, add an `ExtensionServiceConfig` named `nut-client` to the top-level `patches:` list, following the `lldpd` block above it, with the spec's `MONITOR`/`SHUTDOWNCMD` content and mountPath `/usr/local/etc/nut/upsmon.conf`.
- [ ] **Step 2:** Uncomment `- siderolabs/nut-client` on all four nodes. Delete the "disabled on CPs…" comment at line 114.
- [ ] **Step 3:** `mise exec -- sops kubernetes/talos/talenv.sops.yaml` → add `NUT_UPSMON_PASSWORD` (the value of `upsmon-remote` in the Pi's `/root/nut-credentials.txt`).
- [ ] **Step 4:** `cd kubernetes/talos && mise exec -- talhelper genconfig`.
  Verify: `grep -c "ups-cluster@172.16.20.7" clusterconfig/*.yaml` → 1 per node file; `grep -L '\${NUT' clusterconfig/*.yaml` lists all 4 (variable substituted); a new schematic ID in the installer URL.
- [ ] **Step 5:** Commit `talconfig.yaml` + `talenv.sops.yaml` (clusterconfig is gitignored). Open a PR. Rolling out from the branch is OK (talconfig is applied by hand, not by Flux), but merge only with the owner's go-ahead.
- [ ] **Step 6: roll out llm-1 first** (runbook "Upgrade Talos": `talosctl upgrade --image factory.talos.dev/installer/<schematic>:<version> --drain=false`, then `apply-config` with the node file).
  Verify: `talosctl -n 172.16.20.14 service ext-nut-client` → `Running`; on the Pi `upsc -c ups-cluster` lists `172.16.20.14`.
- [ ] **Step 7:** Repeat for .11, .12, .13, one at a time. Wait for `kubectl get nodes` Ready and etcd healthy (`talosctl -n <ip> etcd status`) before the next.
  Verify: `upsc -c ups-cluster` lists all four IPs.

### Task 4: Proxmox (owner, by hand)

- [ ] **Step 1:** On the Proxmox host: `apt install nut-client`; `/etc/nut/nut.conf` → `MODE=netclient`; `/etc/nut/upsmon.conf` → `MONITOR ups@172.16.20.7 1 upsmon-remote <pw> secondary` and `SHUTDOWNCMD "/sbin/shutdown -h +0"`; `systemctl enable --now nut-monitor`.
  Verify: on the Pi `upsc -c ups` lists `172.16.20.3`.
- [ ] **Step 2:** `pvenode config set --startall-onboot-delay 300`. If the option doesn't exist on this PVE version, report back. The fallback is `startup { up_delay }` in `infra/terraform/kubernetes.tf`, a separate tofu change.
- [ ] **Step 3:** BIOS: "Restore on AC power loss" = Power On. Do this at the next reboot, or during Task 6.

### Task 5: Synology (owner, DSM UI)

- [ ] **Step 1:** Control Panel → Hardware & Power → UPS: enable, type "Synology UPS server", server `172.16.20.7`, safe mode = "Until low battery". Apply.
- [ ] **Step 2:** General tab: enable "Restart automatically when power supply issue is fixed".
  Verify: DSM shows Ellipse PRO + battery %; on the Pi `upsc -c ups` lists `172.16.20.2`.

### Task 6: Docs, then the end-to-end test

**Files:**
- Create: `design/decisions/ups.md` (groups, sequence, DSM `ups`/`monuser` constraint, outlets not switchable, script location on the Pi, how to test FSD on `ups-cluster`)
- Modify: `design/architecture.md:52` (`nut-client` enabled, watches `ups-cluster`)
- Modify: `design/runbook.md` Planned Maintenance Shutdown (add `172.16.20.14`; add a short "UPS-triggered shutdown" subsection pointing to `ups.md`)
- Modify: `design/TODO.md` (trim the NUT item to metrics/alerts, Gatus/Homepage, UniFi, root-password rotation)
- Modify: `.claude/CLAUDE.md` "Which file to read" table — add a row `UPS, NUT, power-loss shutdown order | design/decisions/ups.md`

- [ ] **Step 1:** Write the doc changes, commit to the same PR.
- [ ] **Step 2: end-to-end test (owner present, planned window).** Pull the UPS mains plug for more than 2 min. Watch on the Pi: `journalctl -t ups-shutdown -f`, then `watch upsc -c ups-cluster`.
  Expected order: 4 Talos IPs drop off `ups-cluster` → .2/.3 drop off `ups` → the Pi powers down → the UPS output cuts about 20 s later.
- [ ] **Step 3:** Restore mains. Record the boot times of Synology, Proxmox, VMs and cluster.
  Expected: all nodes Ready, `flux get kustomizations` all Ready, the CNPG cluster healthy, with no manual step. Put the measured Synology boot time in `ups.md` and adjust `startall-onboot-delay` if NFS wasn't up in time.
- [ ] **Step 4:** Pull the plug for 60 s, then restore → nothing shuts down (`journalctl -t ups-shutdown` is empty).
- [ ] **Step 5:** Merge the PR after the owner's go-ahead.
