# UPS shutdown and recovery — design

Date: 2026-11-07 · Status: draft, not built

## Intent

On the last power cut everything died at once. When power returned, everything booted at the
same time, the cluster came up before the Synology was serving NFS, and recovery took a long
time. CNPG Postgres lives on `nfs-client`, which is the Synology
(`kubernetes/apps/postgres/cluster/app/cluster.yml:93`), so a Synology that disappears before
the cluster is a real data risk, not just a slow boot.

**Goal:** after a power cut longer than 2 minutes, shut the homelab down **in groups** with
the cluster first, then power everything back on and let it **recover with no manual step**.

- **Goals:** a clean Talos shutdown while NFS is still up · the Synology goes down only after
  the cluster · the UPS power-cycles so every device gets a clean "power returned" ·
  automatic, correctly ordered boot.
- **Non-goals (later TODOs, already in `design/TODO.md`):** metrics/alerts, Gatus/Homepage,
  UniFi DHCP reservation and DNS name, notifications.
- **Anti-goals:** a blip under 2 minutes shuts anything down · the Synology goes down while
  a Talos node is still running · a shutdown that leaves devices off when power returns.

## What is on the UPS

Eaton Ellipse PRO 850 (USB to the Pi), about 17 min runtime at 25% load by the UPS's own estimate on 2026-11-06. That was never measured; in practice it lasted seconds at that load (`design/decisions/ups.md`).
Its outlets **cannot be switched individually** (`outlet.*.switchable: no`). The UPS can only
cut its whole output (`shutdown.return`, `ups.delay.shutdown` = 20 s, `ups.delay.start` = 30 s).

| Device | IP | Role in the sequence |
|---|---|---|
| UniFi switch | — | stays powered until the UPS cuts its output; links Proxmox ↔ Synology ↔ Pi |
| Synology RS1219+ | 172.16.20.2 | group 2 |
| Proxmox host (MS-02) | 172.16.20.3 | group 2; runs the 4 Talos VMs and the ZeroTier VM (.23) |
| Pi 4 `nut-server` | 172.16.20.7 | the conductor; group 3 |

## The groups

Groups are **separate NUT UPS names**. FSD ("forced shutdown") is raised per UPS name, so only
the clients watching that name react.

| Group | NUT name | Listeners | Trigger |
|---|---|---|---|
| 1 — cluster | `ups-cluster` (a `dummy-ups` repeater of `ups`) | cp-1/2/3, llm-1 via `siderolabs/nut-client` | the Pi's script raises FSD on `ups-cluster` |
| 2 — storage + host | `ups` (the real driver) | Synology DSM, Proxmox `upsmon` | the Pi runs `upsmon -c fsd` |
| 3 — UPS | `ups` | the Pi's own `upsmon` (primary) | standard NUT primary flow: wait for secondaries, shut down the Pi, killpower → `shutdown.return` |

The real UPS has to be called **`ups`**, because Synology DSM hardcodes that UPS name. It
replaces today's `eaton` name.

## Sequence

```
power lost ─┬─ < 2 min back → cancel, nothing happens
            └─ 2 min ─► [1] FSD ups-cluster ─► Talos nodes run /sbin/poweroff
                         wait until 0 clients on ups-cluster  (max 6 min, then continue anyway)
                     ─► [2] upsmon -c fsd  (FSD on ups) ─► Synology + Proxmox shut down
                         primary waits for secondaries to log out (HOSTSYNC, max 4 min)
                     ─► [3] Pi shuts down; at the end, killpower → UPS cuts output after 20 s
power back  ─► UPS restores output → Pi, switch, Synology, Proxmox boot
            ─► Proxmox waits for NFS before starting the Talos VMs
```

Worst-case budget: 2 + 6 + 4 + ~1 = 13 min, inside 17 min. If the battery hits low first,
NUT's native low-battery path FSDs everything at once. That is an accepted fallback.

**Power returns mid-sequence (my call):** once step [1] has fired, the sequence **finishes**.
The Talos VMs are already off and nothing would restart them otherwise. On mains, the UPS
still power-cycles its output on `shutdown.return`, so every device reboots in order.

---

## Step 1 — NUT config (Pi, 172.16.20.7)

Hand-managed on the Pi, not in git (same as today). Back up `/etc/nut` before changing it.

1. **Rename the real UPS** `eaton` → `ups` in `ups.conf`, and add the group-1 repeater:
   ```ini
   [ups]
     driver = usbhid-ups
     port = auto
     vendorid = 0463
     productid = FFFF
     serial = P359L18AR6
     desc = "Eaton Ellipse PRO 850"

   [ups-cluster]
     driver = dummy-ups
     port = ups@localhost
     desc = "Group 1 - Talos cluster"
   ```
2. **Users** in `upsd.users`:
   - keep `admin` (`actions = SET FSD`, `instcmds = ALL`) — the script uses it to FSD `ups-cluster`
   - keep `upsmon-local` (primary) — the Pi's own upsmon
   - keep `upsmon-remote` (secondary) — Talos nodes and Proxmox
   - add `monuser` / `secret` (secondary) — **DSM hardcodes these**, so they can't be changed
3. **Firewall:** DSM forces a public password, so allow TCP 3493 only from .2, .3, .11–.14
   and localhost (nftables on the Pi).
4. **`upsmon.conf`:** `MONITOR ups@localhost 1 upsmon-local <pw> primary`. **Do not** monitor
   `ups-cluster` here, so that FSD on group 1 doesn't shut the Pi down. Set `HOSTSYNC 240` so the
   primary waits up to 4 min for the Synology and Proxmox to log out. Keep `POWERDOWNFLAG` so
   the shutdown ends in killpower.
5. **Timer:** `NOTIFYCMD /usr/sbin/upssched` with `NOTIFYFLAG ONBATT/ONLINE SYSLOG+EXEC`.
   `upssched.conf`:
   ```
   AT ONBATT ups@localhost START-TIMER ups-shutdown 120
   AT ONLINE ups@localhost CANCEL-TIMER ups-shutdown
   ```
   `CMDSCRIPT` → `/usr/local/sbin/ups-shutdown`.
6. **`/usr/local/sbin/ups-shutdown`** (on the timer event `ups-shutdown`):
   1. log a line, then write a lock file so a second run is a no-op
   2. FSD group 1: send `USERNAME admin` / `PASSWORD …` / `FSD ups-cluster` to `localhost:3493`
      over the NUT protocol
   3. poll `upsc -c ups-cluster` every 5 s until it lists no clients, or 6 min pass
   4. run `upsmon -c fsd` — groups 2 and 3 follow through the standard NUT primary flow
7. **Recovery:** Raspberry Pis boot whenever they get power, so the Pi needs no setting.

**Verify before anything else (spike, no cluster impact):** with no clients on `ups-cluster`,
raise FSD on it and confirm `upsc ups-cluster ups.status` shows `FSD`, the Pi's upsmon does
**not** react, and the real `ups` stays `OL`. Also confirm that the `dummy-ups` repeater
passes `OB`/`LB` through. Then restart the driver to clear FSD.

## Step 2 — Talos config (`kubernetes/talos/talconfig.yaml`)

For all four nodes (cp-1, cp-2, cp-3, llm-1):

1. Uncomment `siderolabs/nut-client` in `schematic.customization.systemExtensions`, and
   delete the misleading "disabled on CPs" comment (`talconfig.yaml:114`).
2. Add the extension's config. Prefer one shared block (top-level patch or per-node
   `extensionServices`, the builder's choice) so the four copies can't drift:
   ```yaml
   extensionServices:
     - name: nut-client
       configFiles:
         - content: |-
             MONITOR ups-cluster@172.16.20.7 1 upsmon-remote ${NUT_UPSMON_PASSWORD} secondary
             SHUTDOWNCMD "/sbin/poweroff"
           mountPath: /usr/local/etc/nut/upsmon.conf
   ```
3. Put `NUT_UPSMON_PASSWORD` in `kubernetes/talos/talenv.sops.yaml` (SOPS/age), never in
   plain text.
4. `talhelper genconfig`. The new schematic needs a node upgrade to the new image. Do it one
   node at a time (runbook "upgrade Talos" section), with **llm-1 first** as the canary.
5. Verify: `talosctl -n <node> service ext-nut-client` shows `Running`, and on the Pi
   `upsc -c ups-cluster` lists all four IPs.

`/sbin/poweroff` triggers Talos's graceful shutdown: pods get SIGTERM and their grace period,
volumes unmount, etcd flushes and stops. No etcd member removal, so quorum forms again on boot
(`design/runbook.md`, Planned Maintenance Shutdown). No drain is needed.

**Update `design/runbook.md` (Planned Maintenance Shutdown):** add **172.16.20.14** to the
`talosctl shutdown` node list. It is missing today.

## Step 3 — Synology and Proxmox config

### Synology (DSM → Control Panel → Hardware & Power)
1. **UPS** tab: enable UPS support with **UPS type = "Synology UPS server"**, which is DSM's
   label for any NUT server despite the name. Server IP `172.16.20.7`. DSM connects as
   `monuser`/`secret` to the UPS `ups`.
2. "Time before DiskStation enters Safe Mode" = **"Until low battery"**, so DSM reacts **only**
   to FSD/LB and never to its own timer.
3. **General** tab: enable **"Restart automatically when power supply issue is fixed"**.
4. Verify: the DSM UPS tab shows the Ellipse PRO and its battery level, and on the Pi
   `upsc -c ups` lists `172.16.20.2`.

### Proxmox host (172.16.20.3)
1. `apt install nut-client`, `nut.conf: MODE=netclient`, and in `upsmon.conf`:
   `MONITOR ups@172.16.20.7 1 upsmon-remote <pw> secondary` with
   `SHUTDOWNCMD "/sbin/shutdown -h +0"`. A clean host shutdown also ACPI-stops the ZeroTier VM
   through `pve-guests`. A hard cut by the UPS is acceptable if it hangs (your call).
2. **BIOS (MS-02):** set "Restore on AC power loss" = **Power On**.
3. **Boot order:** Talos VMs must not start before Synology NFS is serving. Builder's choice,
   in order of preference:
   - a node-level start-on-boot delay of about 5 min (`pvenode config set
     --startall-onboot-delay 300`; confirm the option exists on this PVE version), or
   - `startup { up_delay }` on the first VM in `infra/terraform/kubernetes.tf` (OpenTofu,
     applied by hand).
   The Synology needs about 3–5 min to boot; measure it on the first real test and adjust.
4. Verify: on the Pi, `upsc -c ups` lists `172.16.20.3`.

---

## End-to-end test (planned window, after steps 1–3)

1. Pull the UPS mains plug. Leave it out for **more than 2 min**.
2. Watch: Talos nodes disappear from `upsc -c ups-cluster` → Synology + Proxmox log out of
   `ups` → the Pi shuts down → the UPS cuts its output.
3. Plug mains back in. Time each device's return and confirm Flux and CNPG are healthy without
   intervention.
4. Separately: pull the plug for **under 2 min** and confirm nothing shuts down.

## Docs to update when built

- `design/architecture.md` — the extension table (`nut-client` enabled, not "no UPS yet").
- A new `design/decisions/ups.md` with the group design, the DSM `ups`/`monuser` constraint,
  and the "outlets not switchable" fact.
- `design/runbook.md` — the llm-1 fix plus a short "UPS-triggered shutdown" section.
- `design/TODO.md` — trim the NUT item down to what is left.

## Left to the builder

The exact script code, the nftables syntax, a shared vs per-node `extensionServices` block, and
which boot-delay mechanism, within the preferences above.
