# DNS and ad blocking

**Read before editing:** `kubernetes/apps/network/blocky/`, `kubernetes/apps/monitoring/dns-monitoring/`, `infra/blocky-pi/`, `kubernetes/talos/talconfig.yaml` (`nameservers`)

## Current state

Two Blocky resolvers serve the LAN, and DHCP on the UDM hands out both:

| Resolver | Address | Runs on | Survives |
|---|---|---|---|
| Cluster | `172.16.20.53` (pool-b LoadBalancer, 2 replicas) | Flux, `network` namespace | Pi failure |
| Pi | `172.16.20.7` | systemd on the NUT Pi | Cluster or Proxmox failure |

Both use one config, `kubernetes/apps/network/blocky/app/blocky.conf`. The cluster mounts it
through a ConfigMap, and Reloader rolls the pods when it changes. On the Pi, `blocky-sync`
runs every 15 minutes, pulls that file from `main`, and reads the Blocky version from the
image tag in the same app's HelmRelease, so a Renovate bump moves both. `blocky-sync`
validates first and rolls back if Blocky stops answering.

`*.blackcats.cc` is answered locally, so those names work with no internet. Cloudflare still
holds the public records for remote Netbird clients and DNS-01. The UDM has no local DNS
records and forwards everything upstream; its own upstream stays on public DNS.

There is no web UI: Blocky is configured only through `blocky.conf`, and Grafana is how you
look at it.

## Per-device visibility

Two Grafana dashboards live in `kubernetes/apps/monitoring/dns-monitoring/`, both upstream's
with the datasources filled in:

- **Blocky** uses metrics scraped from both resolvers: rates, blocks, cache, top clients.
- **Blocky query** uses the query log: which domains each device looked up, and what was
  blocked. Pick a device in `client_name`. This is the one that answers "what is my TV doing".

Device names are reverse lookups against the UDM's DHCP leases. Real client IPs need
`externalTrafficPolicy: Local` on the Service.

The log lives in a dedicated single-instance CNPG cluster, `blocky-logs`, kept for 14 days.
It's separate from the shared cluster because the Pi reaches it over the LAN at
`172.16.20.54:5432`, and that must not expose the database behind SSO. Grafana reads it with
the `grafana_ro` role.

Blocky connects its log writer **only at startup**, and retries before it starts answering
DNS. So `creationAttempts` is 1, and a Blocky that starts with the database down serves DNS
but logs nothing until restarted:

- **Cluster:** an initContainer waits up to 3 minutes for the database first.
- **Pi:** `blocky-sync` restarts Blocky once the database is reachable again.

A database that dies after Blocky has connected is harmless: DNS is unaffected, those
queries are lost, and logging resumes on its own.

## Rules

- **Never point a node at `172.16.20.53`.** Nodes use the Pi, then the UDM. A node that needs
  the cluster's DNS to start the cluster can't recover from a full restart.
- **Set `nameservers` at node level in `talconfig.yaml`, never under `networkInterfaces[]`.**
  There it is silently dropped, and the node runs on Talos's default public resolvers.
- **Add a `customDNS` mapping for any name not behind the Gateway.** The `blackcats.cc` entry
  is a wildcard to `172.16.20.50`, so a missing exception doesn't fail: it answers with the
  wrong address.
- **Keep `filterUnmappedTypes: true`.** With it off, AAAA for a local name goes upstream and
  times out during an outage. It does make TXT under `blackcats.cc` come back empty, which is
  why cert-manager's DNS-01 check uses `--dns01-recursive-nameservers-only` against public DNS.
  Don't remove those flags.
- **Never add a public resolver as a client's third DNS server.** Clients use any of them, so
  it bypasses blocking and can't be relied on for local names beyond what Cloudflare publishes.
- **Keep `blocky.conf` free of dollar signs and named `.conf`.** Flux substitutes variables in
  the rendered ConfigMap, and CI's kubeconform rejects a `.yml` with no `kind:`. The dashboards
  are `.dashboard` for the same reason, and their Kustomization has no `postBuild`.
- **Never raise `queryLog.creationAttempts`.** Each retry delays DNS at startup.
- **Never leave `/etc/blocky/querylog-dsn` missing.** Blocky exits on a missing `file:` target.
  `install.sh` writes a placeholder, which means "run without logging".
- **Keep `log.level: warn`.** Below that, the fallback prints every query, which fills the Pi's
  journal and rotates out the UPS shutdown logs.
- **Keep `externalTrafficPolicy: Local` on the Blocky Service.** With `Cluster`, every query
  appears to come from a node. Cilium's docs still call `Local` incompatible with L2
  announcements, but v1.20's `pkg/l2announcer` withdraws the announcement on nodes with no live
  local pod. Re-check that code before a Cilium major upgrade.
- **Keep port 4000 off the Service, and keep `blocky.nft` on the Pi.** The API can turn blocking
  off and has no auth. `blocky-sync` refuses to run without that firewall table.
- **Keep `repository:` and `tag:` adjacent in the HelmRelease.** `blocky-sync` reads the
  version from those two lines.
- **Re-run `install.sh` after changing anything else in `infra/blocky-pi/`.** `blocky-sync`
  updates only the config and the binary, not itself or the systemd units.

## Limits

- **No serve-stale.** A long internet outage still breaks external names once cached answers
  expire. `minTime: 5m` and prefetching cover short gaps only.
- **Query history has gaps.** Lookups made while the log database is down aren't recorded,
  including everything the Pi answers during a cluster outage.
- **Only DNS is visible.** A device using its own DoH or a hardcoded resolver doesn't appear,
  and the log shows domains, never what was sent to them.
- **Every household device is logged.** Lookups are kept for 14 days and readable by anyone
  with Grafana access. Change `logRetentionDays` to adjust.
- **A bad config reaches both resolvers.** The cluster keeps its old pods if new ones fail to
  start, and the Pi refuses or rolls back a config that fails validation. A valid but wrong
  config, such as a bad mapping, applies to both.

## Hand-managed in UniFi

Not in git, so a UDM reset means redoing these:

- DHCP DNS servers on every VLAN: `172.16.20.7`, `172.16.20.53`
- Firewall: allow TCP/UDP 53 to both from `172.16.10.0/23`
- UDM ad blocking off

## Verify

```bash
# both resolvers answer a local name
dig +short jellyfin.blackcats.cc @172.16.20.7
dig +short jellyfin.blackcats.cc @172.16.20.53

# blocking is on (expect 0.0.0.0)
dig +short app-measurement.com @172.16.20.7

# nodes really use the Pi first
mise exec -- talosctl -n 172.16.20.11 get resolverstatus

# the Pi is on the version and config from main
journalctl -u blocky-sync.service -n 20

# real client names in the metrics, not node IPs
curl -s http://172.16.20.7:4000/metrics | grep blocky_query_total   # from a cluster node only

# give the Pi its query log credentials (once, after the cluster side is up)
mise exec -- kubectl get secret blocky-logs-dsn -n network -o jsonpath='{.data.dsn}' | base64 -d \
  | sed 's/blocky-logs-rw.network.svc/172.16.20.54/' \
  | ssh root@172.16.20.7 'cat > /etc/blocky/querylog-dsn'
```
