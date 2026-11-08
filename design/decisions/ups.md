# UPS and power-loss shutdown

**Read before editing:** `kubernetes/talos/talconfig.yaml` (the `nut-client` ExtensionServiceConfig), the Pi at 172.16.20.7 (`/etc/nut/`, `/usr/local/sbin/ups-shutdown`)

## Current state

An Eaton Ellipse PRO 850 is connected over USB to a Raspberry Pi 4 (`nut-server`, 172.16.20.7, Debian).
The Pi runs NUT in `netserver` mode. It powers the UniFi switch, the Synology, the Proxmox host and
itself. **The Pi config is hand-managed.** A redacted copy lives in `infra/nut-pi/` (same paths as
on the Pi), so re-copy it there after changing it on the Pi. The passwords are only on the Pi, in
`/root/nut-credentials.txt`. The original Debian configs are in `/etc/nut/*.orig`; the
pre-groups state is in `/etc/nut.bak-2026-11-07/`.

The UPS's outlets **cannot be switched individually** (`outlet.*.switchable: no`). The only power
control is cutting the whole output (`shutdown.return`, 20 s delay, 30 s restart delay).

### Groups = NUT UPS names

FSD ("forced shutdown") is raised per UPS name, so each name works as a shutdown group:

| Group | NUT name | Clients | Goes down when |
|---|---|---|---|
| 1 — cluster | `ups-cluster` (`dummy-ups` repeater of `ups`) | cp-1/2/3, llm-1 (`siderolabs/nut-client`, user `upsmon-remote`) | the script raises FSD on `ups-cluster` |
| 2 — storage + host | `ups` (`usbhid-ups`) | Synology (`monuser`), Proxmox (`upsmon-remote`) | the script runs `upsmon -c fsd` |
| 3 — Pi + UPS | `ups` | the Pi's own upsmon (primary, `upsmon-local`) | after group 2 logs out (`HOSTSYNC 240`), then killpower |

### Sequence

1. On battery → `upssched` starts a **120 s** timer. Power back first → timer cancelled, nothing happens.
2. Timer fires → `/usr/local/sbin/ups-sched-cmd` (runs as `nut`) touches
   `/run/nut/ups-shutdown.trigger` → `ups-shutdown.path` starts the root `ups-shutdown.service`.
3. `/usr/local/sbin/ups-shutdown`: FSD on `ups-cluster`. Each node runs `/sbin/poweroff`, which
   calls the Talos Shutdown API **without force**. That is the same as `talosctl shutdown` without
   `--force`: the node **cordons, then drains** (up to 1 min for the cordon and up to 5 min for
   evictions, `DrainTimeout`, after which Talos logs a warning and carries on). After that, pods get
   SIGTERM + grace, volumes unmount, and etcd stops. There is no etcd member removal, so quorum forms
   again on boot. `/sbin/poweroff` has no force option.
   **The drain always stalls:** the `postgres-primary` PDB allows 0 disruptions, and
   `gitea-valkey-cluster` drops to 0 after the first eviction. Expect about 6–7 min until the node
   that holds them powers off.
4. It polls `upsc -c ups-cluster` until there are 0 clients, for at most **360 s**, then moves on anyway.
5. `upsmon -c fsd` → the Synology and Proxmox shut down → the Pi shuts down → Debian's
   `nutshutdown` hook sends killpower → the UPS cuts its output 20 s later and restores it when mains
   is back.
6. **Once started, the sequence always finishes**, even if mains returns. The Talos VMs are
   already off, and only a power-cycle brings everything back in order. If the UPS ignores
   `shutdown.return` on mains, `POWEROFF_WAIT=5m` in `nut.conf` reboots the Pi.

If the battery runs low first, NUT's native low-battery path FSDs both names at once. That is an
accepted fallback.

**Why CNPG makes the order matter:** Postgres data is on `nfs-client`, the Synology. A Synology
that goes before the cluster would take Postgres's disk away mid-write.

### Recovery

The Pi boots whenever it has power. Synology: "Restart automatically when power supply issue is
fixed". Proxmox: BIOS "Restore on AC power loss = Power On", plus a start-on-boot delay so the Talos
VMs don't start before NFS is served.

**Status:** the Pi and the Talos side are done. **Done:** the Synology and Proxmox NUT clients (2026-11-08).
**Still pending:** the Proxmox BIOS setting, confirming the boot delay, the group-1 drain fix
(the drain can outlast the 360 s wait), and the end-to-end plug-pull test.

## Rules

- **Never rename `ups` and never change `monuser`/`secret`** — Synology DSM hardcodes the UPS name
  `ups` and that login. Because the password is public, `/etc/nftables.conf` on the Pi allows
  TCP 3493 only from .2, .3, .11–.14 and localhost. Add any new NUT client's IP there.
- **Never let the Pi's own upsmon `MONITOR ups-cluster`** — FSD on group 1 would then shut the Pi
  down too, before group 2.
- **Clear a stray FSD with `systemctl restart nut-server`, not a driver restart** — upsd holds the
  FSD flag, and it survives `nut-driver@ups-cluster` restarts. A stale FSD on `ups-cluster` powers
  every Talos node off as soon as it connects.
- **Expect `nut-driver@ups-cluster` to fail once at boot** — the repeater needs upsd, which
  starts after the drivers. systemd restarts it about 15 s later. Don't add `After=nut-server`
  (it would cycle with nut-server's `After=nut-driver.target`).
- **Put the timer script in a systemd service, never directly in `CMDSCRIPT`** — upssched runs as
  `nut`, which can't read `/root/nut-credentials.txt` or run `upsmon -c fsd`.
- **Create `/run/nut` on Proxmox yourself** — the PVE `nut-client` install lacks
  `nut-common-tmpfiles.conf`, so `/etc/tmpfiles.d/nut-local.conf` (`d /run/nut 0770 root nut -`)
  creates it. Without it upsmon can't write its PID file.
- **`ERR ACCESS-DENIED` in a client's upsmon log means a wrong password.** upsd logs only
  successful logins, so the Pi shows nothing. Copy the password from the Pi with
  `sed -n 's/^upsmon-remote=//p' /root/nut-credentials.txt` rather than retyping it.
- **Keep `upsmon-remote`'s password in sync** with `NUT_UPSMON_PASSWORD` in
  `kubernetes/talos/talenv.sops.yaml` (and Proxmox's `upsmon.conf`). Passwords on the Pi:
  `/root/nut-credentials.txt`.

## Verify

```bash
# on the Pi
upsc -l                          # ups, ups-cluster
upsc -c ups-cluster              # .11 .12 .13 .14
upsc -c ups                      # ::1 (Pi), .2 (Synology), .3 (Proxmox)
DRY_RUN=1 GROUP1_TIMEOUT=10 /usr/local/sbin/ups-shutdown ups-shutdown   # logs only
journalctl -t ups-shutdown

# a node
mise exec -- talosctl -n 172.16.20.11 service ext-nut-client   # Running
```

Testing FSD isolation without touching the cluster only works while **no** nodes are connected to
`ups-cluster`. With nodes connected, an FSD on it is a real cluster shutdown.
