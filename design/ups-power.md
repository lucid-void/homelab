# UPS-backed graceful shutdown — design proposal

**Date:** 2026-08-21
**Status:** **PROPOSED — not implemented.** Nothing in this document is deployed.
**Hardware:** Eaton Ellipse PRO 850 (ELP850IEC class), battery recently replaced.
**Scope:** Proxmox MS-A2 (`172.16.20.3`) + Talos VMs, Synology RS1219+ (`172.16.20.2`),
UDM SE (`172.16.20.254`).
**Goal:** On mains loss — shut down the Talos cluster and the Proxmox host first, then
the Synology last, and come back up unattended when power returns.

---

## Why this order is mandatory, not just preferred

23 PVCs in the cluster use `storageClassName: nfs-client`, which is backed by the
Synology. **That includes CNPG's Postgres data** (`kubernetes/apps/postgres/cluster/app/cluster.yml:90`
— `storageClass: nfs-client`).

If the NAS powers off before the cluster:

- Every pod holding an NFS mount enters uninterruptible I/O wait (D-state).
- Postgres cannot checkpoint or flush; the shutdown is effectively a hard kill.
- kubelet cannot unmount, so the Talos nodes hang instead of halting cleanly.

So "cluster → Proxmox → Synology" is forced by the storage dependency. Any future
change that moves Postgres off `nfs-client` weakens this constraint but does not remove
it — 22 other PVCs still live there.

---

## The constraint that drives the whole design

| Spec | Value |
|---|---|
| Rating | 850 VA / **510 W** |
| Battery | single 12 V 9 Ah (~108 Wh nominal, ~55–65 Wh realistically usable) |
| Comms | **one** USB port (no network management card slot) |
| Form factor | 2U desktop / rackmountable |
| NUT driver | `usbhid-ups`, MGE HID subdriver |

Estimated steady-state load:

| Load | Est. draw |
|---|---|
| MS-A2 Ultra + 3 Talos VMs | ~50–70 W |
| RS1219+ (8-bay, spinning) | ~55–75 W |
| UDM SE (base, no heavy PoE) | ~20–30 W |
| **Total** | **~130–175 W** |

That is only ~25–35 % of rated capacity — comfortable for headroom, but it buys
**roughly 6–9 minutes of runtime**, not the 20+ minutes people assume from the VA
number. Two consequences:

1. **Trigger early.** The shutdown must begin within seconds, not minutes.
2. **Do not trigger on low battery.** On a 9 Ah battery the LB signal fires with ~1–2
   minutes left — nowhere near enough for three VMs plus a host plus a NAS.

All numbers above are estimates. Measure the real figure once wired: the Ellipse PRO
has energy metering, so `upsc ups` reports actual `ups.realpower` and `ups.load`.

---

## Topology: USB into the Synology

The UPS has one USB port, so exactly one machine owns it. Attach it to the **Synology**,
running DSM's built-in NUT server; Proxmox becomes a NUT secondary over the LAN.

```
Synology .2  ──USB── UPS          (DSM: upsd on :3493, "network UPS server")
     │
     │  NUT protocol over LAN, via the UDM SE
     ▼
Proxmox .3   ── upsmon secondary ── shutdown script ── qm shutdown ×3 → poweroff
```

### Why not USB into Proxmox

The decisive reason is power *restoration*, not monitoring.

Whoever owns the USB is the only party that can command the UPS to cut its output at
the end of the sequence. If Proxmox owns it, Proxmox shuts down first and **nobody is
left to issue that command.** The UPS then runs its battery flat, and — critically —
machines that were powered off by *software* do not restart when mains returns, because
from their PSU's perspective the outlet never lost power. Result: manual button presses.

With the Synology owning the USB, the last machine standing is also the one that cuts
power. The requested ordering falls out of the topology instead of needing glue.

Secondary problem with the Proxmox-owns-USB variant: the Synology would have to be
commanded down either by pointing DSM at a non-Synology NUT server (works, but DSM's
behaviour when that server vanishes mid-event is undefined) or by SSH-ing in from a host
that is itself shutting down. Both are worse.

### Configuration

**DSM** — Control Panel → Hardware & Power → UPS:
- Enable UPS support, type USB
- Enable **Network UPS Server**, allowlist `172.16.20.3`
- Set "time before entering Safe Mode" to ~5 minutes (see timeline)
- Enable "Shutdown UPS when the system enters Safe Mode"

**Proxmox** — `apt install nut-client`, then in `/etc/nut/upsmon.conf`:

```
MONITOR ups@172.16.20.2 1 monuser secret secondary
```

`monuser`/`secret` are DSM's stock upsd credentials, and DSM always names the UPS `ups`.

### The UDM SE must be on a battery outlet

It is the transport for the entire NUT link. Without it, Proxmox goes blind at T+0 and
never learns anything happened — the whole mechanism silently does nothing. This is the
highest-consequence wiring decision in the design.

---

## Sequence and timings

| T | Event |
|---|---|
| 0:00 | Mains fails. UPS on battery. Out-of-band notification fires (see below). |
| 0:00–0:20 | **Debounce.** Ignore flickers — do not shut down a cluster over a 5-second blip. |
| 0:20 | Proxmox `upsmon` fires `SHUTDOWNCMD`. |
| 0:20–1:20 | `qm shutdown --timeout 60` on cp-1/2/3 **in parallel**, plus the ZeroTier VM (.23). |
| ~1:30 | `systemctl poweroff` on the host. Load drops to ~80 W → remaining runtime roughly triples. |
| 5:00 | DSM timer expires → Safe Mode: unmount volumes, halt, **and tell the UPS to cut output**. |
| ~6:30 | UPS output off. Battery preserved rather than drained flat. |

The two timers (Proxmox at 0:20, DSM at 5:00) are independent, so the ~4.5-minute gap is
the safety margin. Once Proxmox is down the load halves, so a 5-minute DSM timer is not
at risk of outrunning the battery.

### Implementation notes

**Parallel guest shutdown is load-bearing.** Do not rely on bare `systemctl poweroff`
letting `pve-guests` handle it — that stops guests *sequentially* with a 180 s default
timeout each. Three CPs plus the ZeroTier VM is up to 12 minutes of budget that does not
exist. Write the script explicitly and background the `qm shutdown` calls.

**Use `qm shutdown`, not `talosctl shutdown`.** All three nodes already carry
`siderolabs/qemu-guest-agent` (`kubernetes/talos/talconfig.yaml`), so Proxmox gets a
proper guest-agent halt. The `talosctl` route would require a talosconfig — an
admin-level credential — sitting on the hypervisor. If it were ever needed, mint an
`os:operator` role rather than `os:admin`.

**Leave `siderolabs/nut-client` disabled.** It is commented out at
`kubernetes/talos/talconfig.yaml:105` (and :137, :169). Keep it that way. It shuts down
on low-battery by default — far too late here — and a guest halting itself races the
hypervisor that is trying to stop it. The host should orchestrate its own guests.

Follow-up: the current comment says "disabled to avoid issues during cluster upgrades"
and `design/ARCHITECTURE.md:50` says "disabled — no UPS yet". Both go stale once this
lands; update them to say the hypervisor owns guest shutdown.

---

## Restart on power return

This is the half that these setups usually get wrong. Cutting UPS output at the end is
what makes the outlets re-energize when mains returns, which is what lets firmware bring
the machines back. Three settings:

- **MS-A2 BIOS** → "Restore on AC Power Loss" = **Power On**. *Not* "Last State" — the
  last state was "off", so the machine would stay off.
- **DSM** → Hardware & Power → General → "Restart automatically after a power failure".
- **DSM** → UPS → "Shutdown UPS when the system enters Safe Mode".

**Measure this:** NUT's default `ups.delay.shutdown` on Eaton HID is ~20 s, but DSM needs
60–90 s to unmount and halt. If the UPS cuts before the NAS is down, raise the delay.

**Proxmox guests** — `onboot: 1` on all VMs. The cluster will retry NFS mounts
indefinitely if it beats the Synology up, so this self-heals; a modest `startup` delay
just reduces alert noise during the recovery window.

---

## Outlets and wiring

**Check the EcoControl outlets.** The Ellipse PRO can switch slave outlets off when the
master device idles. If the Synology or the UDM SE lands on a controlled socket it gets
cut arbitrarily. Disable the feature, or verify nothing critical sits on one.

**Outlet count is variant-dependent.** The **IEC model has only 3 battery-backed C13
outlets plus 1 surge-only** — exactly the three required loads with nothing spare. DIN/FR
variants have more. Confirm which unit is on hand.

Proposed allocation:

| Outlet | Device | Note |
|---|---|---|
| Battery 1 | Proxmox MS-A2 | |
| Battery 2 | Synology RS1219+ | owns the USB link |
| Battery 3 | UDM SE | **non-negotiable — NUT transport** |
| Surge only | DGX Spark (.4) or unused | not part of the shutdown sequence |

If the ISP ONT should also stay up, hang a small IEC strip off one battery outlet —
there is ample watt headroom (~175 W used of 510 W), just no spare sockets.

---

## Monitoring integration

Follow the existing `proxmox-monitoring` pattern: a new Flux Kustomization at
`kubernetes/apps/monitoring/ups-monitoring/` with `nut_exporter`
(`DRuggeri/nut_exporter`) as a Deployment + Service + `VMServiceScrape` + `VMRule` +
Grafana dashboard ConfigMap, pointed at `172.16.20.2:3493`.

Two gotchas specific to this:

- The `VMServiceScrape` selector must key off `monitoring.blackcats.cc/scrape`, since
  Flux `commonMetadata` rewrites `app.kubernetes.io/name` (already documented in
  `.claude/CLAUDE.md`).
- **DSM's allowlist is by source IP, and Cilium masquerades pod egress as the node IP**
  — so allowlist `172.16.20.11/.12/.13`, not a pod CIDR. Verify what DSM actually sees.

Proposed alerts:

| Alert | Condition | Why |
|---|---|---|
| `UPSOnBattery` | `ups.status` contains OB | the event itself |
| `UPSLowBattery` | LB flag | last-resort |
| `UPSRuntimeLow` | `battery.runtime` < 300 s | |
| `UPSLoadHigh` | `ups.load` > 70 % | early warning the UPS has been outgrown |
| `UPSBatteryReplace` | RB flag | battery ageing |
| `UPSCommunicationLost` | scrape target down / stale status | **catches a dead USB cable or stopped upsd, which silently disarms the entire mechanism** |

A Gatus `tcp://172.16.20.2:3493` check covers the last one cheaply and independently.

Bonus: `ups.realpower` gives whole-homelab draw for free — useful for a watts panel and
for tracking the load budget against the 510 W ceiling over time.

### The notification path inverts here

The existing rule in `.claude/CLAUDE.md` — *"always post to the in-cluster Service, never
`https://gotify.blackcats.cc`"* — is correct for backup jobs, where **egress** is the
fragile part. **For a power event it is backwards.**

The chain is vmalert → Alertmanager → `am-gotify-bridge` → Gotify → `gotify-telegram`,
all in-cluster. Alertmanager's default `group_wait` alone is 30 s, and the cluster starts
shutting down at T+20s. The on-battery alert will very likely never escape.

So the T+0 notification must come from the Proxmox `upsmon` `NOTIFYCMD` hitting the
**Telegram Bot API directly** — outside the cluster, needing only the UDM SE and the
internet, both of which survive the sequence. Keep the in-cluster `VMRule` alerts as the
slower, richer second layer for non-shutdown conditions (load high, battery replace,
comms lost), which are exactly the ones where the cluster is still alive.

"Power restored" cannot come from Proxmox or the cluster either — both are off. Recovery
announces itself naturally via Gatus and Flux notifications once the cluster is back.

---

## Test plan

Untested, this plan is worth little. Two of these can only be answered empirically.

1. **Transfer test first.** Pull the plug with everything running and confirm nothing
   reboots. Line-interactive units output a stepped approximation of a sine wave on
   battery, and active-PFC PSUs sometimes object. Find out now, not during an outage.
2. Read `ups.realpower` for true load; confirm it is under ~50 % of 510 W.
3. Measure actual runtime to low battery — once, deliberately, with backups verified.
4. Run the Proxmox shutdown script manually (**not** via `upsmon -c fsd`) and time each
   phase.
5. Full pull-the-plug test: verify order, verify the UPS cuts output, verify unattended
   restart, verify `kubectl get cluster postgres -n postgres` returns healthy with 2 ready.
6. Re-time against measured runtime and adjust. Repeat annually and after any battery swap.

---

## Assumptions to confirm

These change the numbers, not the architecture:

- 8 drives populated in the RS1219+
- No heavy PoE load on the UDM SE
- The ZeroTier VM (.23) runs on this same Proxmox host
- The unit is the IEC outlet variant (3 battery + 1 surge)

---

## Follow-up work if adopted

- [ ] `.claude/CLAUDE.md` key-decisions row (platform & networking section)
- [ ] `design/RUNBOOK.md`: power-event procedure, cold-start-from-dark procedure, annual test
- [ ] `design/ARCHITECTURE.md:50`: update the `nut-client` "no UPS yet" note
- [ ] `kubernetes/talos/talconfig.yaml`: update the three `nut-client` comments
- [ ] `kubernetes/apps/monitoring/ups-monitoring/` Kustomization
- [ ] Gatus check for `tcp://172.16.20.2:3493`
- [ ] Proxmox host: `nut-client` install + shutdown script (manual — the host is not
      under IaC; consider committing the config to `infra/proxmox/nut/` for reproducibility)

---

## Sources

- [Eaton Ellipse PRO datasheet (650–1600 VA)](https://www.eaton.com/content/dam/eaton/products/backup-power-ups-surge-it-power-distribution/backup-power-ups/eaton-ellipse-pro-ups/Eaton%20Ellipse%20PRO%20UPS%20-%20650-800-1200-1600%20VA%20-%20Datasheet.pdf)
- [ELP850IEC product specifications](https://docs.rs-online.com/b339/A700000009976135.pdf)
- [ELP850IEC product page](https://www.eaton.com/gb/en-gb/skuPage.ELP850IEC.html)
- [NUT hardware compatibility — Ellipse PRO 650](https://networkupstools.org/ddl/Eaton/Ellipse_PRO_650.html)
- [NUT issue #3010 — Ellipse PRO 1600 confirmed on usbhid-ups](https://github.com/networkupstools/nut/issues/3010)
- [Synology UPS compatibility list](https://www.synology.com/en-us/compatibility?search_by=category&category=upses)
