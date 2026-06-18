# Proxmox host & disk monitoring

Brings the Proxmox host (**MS-A2, `172.16.20.3`**) and its physical NVMe/SSD into the
in-cluster VictoriaMetrics stack: host metrics, **SMART disk health**, **LVM-thin pool
fill**, **kernel-logged disk corruption / I/O errors**, and **Proxmox VE API** metrics.
Alerts route to Gotify like everything else.

## Why this is needed

The monitoring stack runs *inside* the Talos cluster, so in-cluster node-exporter only
sees the VMs' virtual disks. It cannot see the host NVMe's SMART data, the host XFS
filesystem, or the LVM-thin pool. After the etcd instability was traced to the
hypervisor storage layer (see [`design/monitoring-stabilization.md`](../../../../design/monitoring-stabilization.md)),
these are exactly the signals worth watching.

## Architecture

```
Proxmox host 172.16.20.3 (outside k8s)          in-cluster (GitOps, this dir)
┌───────────────────────────────────┐           ┌──────────────────────────────┐
│ node_exporter            :9100     │◀──scrape──│ VMStaticScrape proxmox-node  │
│   + textfile collector:            │           │ VMStaticScrape proxmox-smart │
│     - LVM-thin pool fill %         │           │ VMStaticScrape proxmox-pve   │
│     - kernel disk-corruption count │           │ VMRule proxmox-host          │
│ smartctl_exporter        :9633     │◀──scrape──│   → Alertmanager             │
│ prometheus-pve-exporter  :9221     │◀──scrape──│   → am-gotify-bridge → Gotify│
└───────────────────────────────────┘           └──────────────────────────────┘
```

The GitOps side (VMStaticScrapes + VMRules) is in this directory. **The three exporters
must be installed on the Proxmox host manually** — they cannot be deployed by Flux. The
steps below are the host install; run them as root on `172.16.20.3`.

> Prereq: the `gotify-bootstrap` → `am-gotify-bridge` → `vm-stack` Flux chain must be
> healthy for these scrapes/alerts to reconcile, and the Alertmanager→Gotify webhook in
> the `vm-stack` HelmRelease must be enabled (it was re-enabled alongside this change).

---

## Host install (run as root on 172.16.20.3)

### 1. node_exporter (host metrics + textfile collector)

```bash
apt-get update
apt-get install -y prometheus-node-exporter smartmontools lvm2

# Enable the textfile collector (Debian's default dir is
# /var/lib/prometheus/node-exporter — our scripts write .prom files there).
install -d -o prometheus -g prometheus /var/lib/prometheus/node-exporter
cat >/etc/default/prometheus-node-exporter <<'EOF'
ARGS="--collector.textfile.directory=/var/lib/prometheus/node-exporter"
EOF

systemctl restart prometheus-node-exporter
systemctl enable prometheus-node-exporter
curl -s localhost:9100/metrics | head -n 3   # verify
```

### 2. smartctl_exporter (SMART disk health)

```bash
SC_VER=0.13.0
curl -fsSL -o /tmp/sc.tgz \
  "https://github.com/prometheus-community/smartctl_exporter/releases/download/v${SC_VER}/smartctl_exporter-${SC_VER}.linux-amd64.tar.gz"
tar -xzf /tmp/sc.tgz -C /tmp
install -m0755 "/tmp/smartctl_exporter-${SC_VER}.linux-amd64/smartctl_exporter" /usr/local/bin/smartctl_exporter

cat >/etc/systemd/system/smartctl_exporter.service <<'EOF'
[Unit]
Description=smartctl_exporter
After=network-online.target
Wants=network-online.target

[Service]
# Needs root for raw SMART access to the NVMe/SATA devices.
ExecStart=/usr/local/bin/smartctl_exporter --web.listen-address=:9633
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now smartctl_exporter
curl -s localhost:9633/metrics | grep -m1 smartctl_device_smart_status   # verify
```

### 3. prometheus-pve-exporter (Proxmox VE API metrics)

Create a read-only PVE API token:

```bash
pveum role add Monitoring -privs "Datastore.Audit VM.Audit Sys.Audit Pool.Audit" 2>/dev/null || true
pveum user add prometheus@pve --comment "metrics scraper" 2>/dev/null || true
pveum aclmod / -user prometheus@pve -role Monitoring
# Prints the token secret ONCE — copy it into pve.yml below.
pveum user token add prometheus@pve monitoring --privsep 0
```

Install the exporter into a venv and configure it:

```bash
apt-get install -y python3-venv
python3 -m venv /opt/pve-exporter
/opt/pve-exporter/bin/pip install --upgrade pip prometheus-pve-exporter

install -d /etc/prometheus
cat >/etc/prometheus/pve.yml <<'EOF'
default:
  user: prometheus@pve
  token_name: monitoring
  token_value: "PASTE_TOKEN_SECRET_HERE"
  verify_ssl: false
EOF
chmod 600 /etc/prometheus/pve.yml

cat >/etc/systemd/system/prometheus-pve-exporter.service <<'EOF'
[Unit]
Description=prometheus-pve-exporter
After=network-online.target pve-cluster.service
Wants=network-online.target

[Service]
ExecStart=/opt/pve-exporter/bin/pve_exporter --config.file /etc/prometheus/pve.yml --web.listen-address 0.0.0.0:9221
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now prometheus-pve-exporter
curl -s 'localhost:9221/pve?target=127.0.0.1' | grep -m1 pve_up   # verify
```

### 4. Textfile script — LVM-thin pool fill + kernel corruption log

This is the "log + alert if the disk gets corrupted" piece. It runs every 5 min, writes
metrics for node_exporter to expose, and appends any matched kernel error lines to
`/var/log/disk-corruption.log` (the persistent on-host record).

```bash
cat >/usr/local/bin/pve-disk-health.sh <<'EOF'
#!/usr/bin/env bash
# Emits LVM-thin fill + kernel disk-corruption metrics for node_exporter's
# textfile collector, and logs corruption lines to /var/log/disk-corruption.log.
set -euo pipefail

TEXTFILE_DIR=/var/lib/prometheus/node-exporter
OUT="${TEXTFILE_DIR}/disk-health.prom"
TMP="$(mktemp "${OUT}.XXXX")"
STATE_DIR=/var/lib/prometheus/node-exporter
CURSOR_FILE="${STATE_DIR}/.kmsg-cursor"
COUNT_FILE="${STATE_DIR}/.corruption-count"
CORRUPTION_LOG=/var/log/disk-corruption.log

# Kernel patterns that indicate media/filesystem corruption or I/O failure.
PATTERN='I/O error|Buffer I/O error|XFS.*([Cc]orrupt|internal error)|EXT4-fs error|critical (target|medium) error|blk_update_request.*error|Medium Error|nvme.*(I/O|reset|timeout)'

# --- LVM-thin pool fill % -------------------------------------------------
lvs --noheadings --nosuffix --units b --separator '|' \
    -o vg_name,lv_name,lv_attr,data_percent,metadata_percent 2>/dev/null \
| while IFS='|' read -r vg lv attr data meta; do
    attr="$(echo "$attr" | tr -d '[:space:]')"
    case "$attr" in
      t*)   # thin pool
        vg="$(echo "$vg" | xargs)"; lv="$(echo "$lv" | xargs)"
        data="$(echo "${data:-0}" | xargs)"; meta="$(echo "${meta:-0}" | xargs)"
        echo "node_lvm_thinpool_data_percent{vg=\"$vg\",lv=\"$lv\"} ${data:-0}"
        echo "node_lvm_thinpool_metadata_percent{vg=\"$vg\",lv=\"$lv\"} ${meta:-0}"
        ;;
    esac
  done >>"$TMP"

# --- Kernel corruption / I/O errors (cursor-based, no double counting) ----
total="$(cat "$COUNT_FILE" 2>/dev/null || echo 0)"
cursor="$(cat "$CURSOR_FILE" 2>/dev/null || true)"
if [ -n "$cursor" ]; then
  journalctl -k -o short-iso --after-cursor="$cursor" --show-cursor >/tmp/kmsg.$$ 2>/dev/null || true
else
  # First run: seed the cursor, don't backfill the whole journal.
  journalctl -k -o short-iso --lines=1 --show-cursor >/tmp/kmsg.$$ 2>/dev/null || true
fi
newcur="$(grep -- '-- cursor:' /tmp/kmsg.$$ | tail -1 | sed 's/^-- cursor: //')"
[ -n "$newcur" ] && echo "$newcur" >"$CURSOR_FILE"
if [ -n "$cursor" ]; then
  matches="$(grep -aiE "$PATTERN" /tmp/kmsg.$$ | grep -av -- '-- cursor:' || true)"
  if [ -n "$matches" ]; then
    printf '%s\n' "$matches" >>"$CORRUPTION_LOG"
    n="$(printf '%s\n' "$matches" | grep -c . || true)"
    total=$(( total + n ))
  fi
fi
rm -f /tmp/kmsg.$$
echo "$total" >"$COUNT_FILE"
echo "node_disk_corruption_events_total{pattern=\"kernel_disk_errors\"} ${total}" >>"$TMP"

chmod 644 "$TMP"
mv "$TMP" "$OUT"
EOF
chmod +x /usr/local/bin/pve-disk-health.sh
touch /var/log/disk-corruption.log

# systemd timer (every 5 min)
cat >/etc/systemd/system/pve-disk-health.service <<'EOF'
[Unit]
Description=Collect Proxmox LVM-thin + disk-corruption metrics
[Service]
Type=oneshot
ExecStart=/usr/local/bin/pve-disk-health.sh
EOF
cat >/etc/systemd/system/pve-disk-health.timer <<'EOF'
[Unit]
Description=Run pve-disk-health every 5 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pve-disk-health.timer
/usr/local/bin/pve-disk-health.sh   # run once now
cat /var/lib/prometheus/node-exporter/disk-health.prom   # verify metrics
```

### 5. Firewall

If the PVE host firewall is enabled, allow the cluster/VLAN to reach the exporters:

```bash
# Datacenter or host firewall rule — allow 9100, 9221, 9633 from the VLAN.
# e.g. source 172.16.20.0/24 → tcp dport 9100,9221,9633 ACCEPT
```

---

## Verify the cluster side

After Flux reconciles `proxmox-monitoring`:

```bash
mise exec -- kubectl get vmstaticscrape -n monitoring | grep proxmox
# In vmagent /targets you should see jobs proxmox-node / proxmox-smartctl / proxmox-pve = UP
# In VM: up{job=~"proxmox-.+"}  → 1,1,1
# Sample data: smartctl_device_smart_status, node_lvm_thinpool_data_percent,
#              node_disk_corruption_events_total, pve_up
```

## What fires to Gotify

| Alert | Trigger |
|---|---|
| `ProxmoxExporterDown` | host/exporter unreachable 5m (host may be down) |
| `ProxmoxDiskWriteLatencyHigh` | disk write latency >100ms 10m (the etcd-stall pattern) |
| `SmartDeviceHealthFailed` | SMART self-assessment FAILED |
| `SmartPendingOrUncorrectableSectors` / `SmartReallocatedSectors` | bad sectors |
| `NvmeMediaErrors` / `NvmeCriticalWarning` | NVMe data-integrity / critical flag |
| `NvmeWearHigh` | >80% rated endurance used |
| `LvmThinPoolDataHigh` / `LvmThinPoolMetadataHigh` | thin pool nearing exhaustion |
| `DiskCorruptionDetected` | kernel logged corruption/I/O errors (see `/var/log/disk-corruption.log`) |
| `ProxmoxStorageAlmostFull` | a PVE storage >85% full |
