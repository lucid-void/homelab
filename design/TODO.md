# TODO — homelab-k8s

Known gaps, planned work, and items that need verification.

---

## Not Yet Built

*(nothing currently — see Future Work and Service Candidates below)*

---

## Known Broken

*(nothing currently)*

---

## Stale / Needs Update

*(nothing currently)*

---

## Needs Verification

### Goldilocks and Gatus OIDC callback URIs

`docs/services.md` lists these as "TBD" — the OIDC redirect URIs for Goldilocks and Gatus have not been confirmed. Verify in the Zitadel console or check the HelmRelease values.

### CNPG WAL archiving

CNPG currently does base backups only — WAL archiving is not configured. Without WAL archiving, point-in-time recovery is not possible; recovery is limited to the last daily snapshot.

**Consider:** adding `backup.barmanObjectStore` to the CNPG Cluster spec for WAL archiving to Synology or Filen.

### Homepage configuration

The `homepage` HelmRelease uses a ConfigMap for all configuration. Current contents of the ConfigMap have not been audited — some entries may point to services that have moved from Swarm to k8s (with changed hostnames) or been decommissioned.

### Backup restore actually works

Five backup CronJobs exist (immich, paperless, gitea, homebox, postgres) writing restic snapshots to `rclone:filen:backups/restic/`. None have been restore-tested end-to-end. "Backup created" ≠ "data restorable."

**Action:** Pick one app (Immich is highest-value) and run a restore drill into a clean PVC + fresh CNPG database. Document the procedure in `RUNBOOK.md` once it works.

### TLS certificate expiry alerting

cert-manager renews `shared-tls` automatically via Cloudflare DNS-01. If renewal fails (Cloudflare API token rotation, ACME rate limit, network issue), there's no documented alert path — users will see browser warnings before the operator notices.

**Action:** Confirm whether Gatus or cert-manager metrics scrape catches a Certificate object stuck in `Ready: False` and routes to Gotify. If not, add a Gotify webhook tied to cert-manager events.

### Backup failure notification path

All backup CronJobs reference `gotify-secret` with `optional: true`. If a backup fails AND Gotify is down or its token is invalid (e.g. after a Gotify SQLite reset before `gotify-bootstrap` re-runs), the failure is silent.

**Action:** Add a secondary alert path (email via SMTP sidecar, or a separate webhook) so silent-Gotify doesn't hide silent-backups.

### Falco → Gotify → Telegram bridge

Falco events route to Gotify, then a Python WebSocket bridge forwards to Telegram. Bridge reconnect behavior on Telegram API rate-limits / network drops is not proven. No alert if the bridge pod itself crashes silently.

**Action:** Verify the bridge has a liveness probe and that bridge pod restarts are themselves notified.

---

## Future Work

### Per-namespace NetworkPolicies

Cilium supports L7 NetworkPolicies. Currently no `NetworkPolicy` or `CiliumNetworkPolicy` resources are deployed — all pods can reach all other pods. Adding default-deny + per-namespace allow rules would mirror the Swarm overlay isolation model.

**Priority targets** (highest blast-radius first):
1. `postgres` namespace — allow ingress only from declared app namespaces; combined with cleartext intra-cluster Postgres traffic, any pod RCE currently = full DB access
2. `auth` namespace (Zitadel) — allow ingress only from gateway + OIDC clients
3. `cert-manager`, `flux-system`, `kube-system` — restrict egress and cross-namespace ingress

### nftables host firewall on k8s nodes

Same as the broader homelab plan: default-deny inbound, SSH/node_exporter/Promtail allowlist, per-host service overrides. Not yet implemented on k8s nodes.

### Zitadel break-glass / account recovery runbook

Zitadel is the single OIDC provider for Immich, Paperless, Gitea, FreshRSS, Goldilocks, Gatus. If the admin is locked out (lost TOTP, recovery codes gone, bootstrap secret broken) there is no documented recovery path.

**Action:** Add a "Zitadel admin recovery" section to `RUNBOOK.md` covering: (1) recovery code regeneration, (2) emergency admin reset via `kubectl exec` into the Zitadel pod, (3) restoring from the `zitadel-bootstrap` Job + CNPG `zitadel-role-secret`.

### Document SOPS age key protection

The single SOPS age key decrypts: Talos secrets, the SealedSecrets controller key backup at `/volume2/backups/keys/sealed-secrets-key.sops.yaml`, and any other SOPS blob. `design/docs/secrets.md` does not specify where the age key itself lives, whether it has a passphrase, or whether an off-Synology copy exists. If the age key is on the same Synology volume it protects, the chain is single-link.

**Action:** Document age key location, protection (passphrase?), and require at least one off-Synology copy (paper, hardware token, second offsite). Mention in `secrets.md` and `RUNBOOK.md` recovery section.

### Disaster recovery runbook — rebuild from Filen

`RUNBOOK.md` covers single-CP loss, all-three-CP loss, and etcd quorum recovery, but does not cover restoring application data from Filen into a freshly-rebuilt cluster. Without this, even with valid backups, restore is improvisational.

**Action:** Document the steps to: (1) re-seed CNPG databases from restic snapshots, (2) restore PVC contents (Immich library, Paperless docs, Gitea repos, Homebox SQLite), (3) re-run Zitadel bootstrap with restored data, (4) verify OIDC re-linking. Run this end-to-end during the Immich restore drill above.

### CNPG resource requests/limits + capacity plan

Shared CNPG cluster hosts seven application databases (immich, paperless, gitea, zitadel, freshrss, homebox, and any future). No documented resource requests/limits, no capacity ceiling. A memory-leaky app on the same node can starve Postgres; Goldilocks is in recommender-only mode so nothing enforces.

**Action:** Set explicit requests/limits on the CNPG cluster spec; document target headroom; add a Gatus or Prometheus alert when CNPG approaches limits.

### Postgres major version upgrade plan

Sharing one CNPG cluster across seven apps means a Postgres major upgrade (e.g. 16 → 17) must be schema-compatible with all seven simultaneously. No tested procedure, no rollback plan.

**Action:** Document the upgrade approach (in-place via CNPG image bump vs. logical dump+restore), schedule a dry-run on a test cluster, identify per-app schema compatibility checks before any future upgrade.

### Immich v2.7.5 upgrade plan

Immich is pinned to v2.7.5 with kysely migrations (see memory: `project_immich_migration`). Major version bumps require migration testing; the `oauthId` re-linking story is known-fragile after Zitadel migration.

**Action:** Define the upgrade criteria (when to bump), the rollback path (PVC snapshot + DB dump before bump), and the validation checklist (mobile sync, OIDC re-link, asset/album/person FK integrity).

### Storage capacity monitoring + quotas

Immich library and the static `/volume2/Media` share both grow uncapped. No quota, no alert before Synology pool fills, no tiering plan. A full pool stops all DB writes cluster-wide.

**Action:** Add a Synology pool-usage alert (synology metrics → the in-cluster VictoriaMetrics stack) at 80% and 90%; consider per-namespace `ResourceQuota` for PVC storage.

### Immutable / second-offsite backup tier

Restic on Filen with 30-day retention is not immutable. A cluster compromise (or a runaway delete script) can wipe recent backups before they age out. Single offsite provider = single account-compromise risk.

**Action:** Either enable restic append-only mode on a separate Filen account, or add a second offsite target (B2/MinIO/Storj) for the highest-value snapshots (Zitadel + SOPS age key + Sealed Secrets master key + Immich/Paperless).

### Centralized log retention in cluster

The in-cluster VictoriaMetrics stack handles metrics, but **VictoriaLogs is not enabled** (`vlogs` is commented out in the vm-stack HelmRelease). k8s container logs live ephemerally in `/var/log/pods/` on each node and are editable by anyone who roots a node. No forensic trail for Falco events beyond the real-time Gotify push.

**Action:** Enable VictoriaLogs (`vlogs`) in the vm-stack HelmRelease and ship node/pod logs to it for durable, queryable retention.

### Image digest pinning / signature verification

Current policy is minor-semver tags (`sonarr:4.0.*`, etc.). Tags can be re-pushed; Renovate auto-PRs accept new minor versions without provenance checks. Custom images (`ghcr.io/lucid-void/*`) are also tag-only.

**Action:** Decide explicitly: (1) accept the risk and document it in `ARCHITECTURE.md` key decisions, or (2) move to digest pinning for the custom images at minimum and consider Sigstore/cosign verification via Kyverno admission policy. Either outcome is fine — leaving it undecided is the issue.

### UDM SE DNS single point of failure

UDM SE serves gateway + DHCP + DNS resolver + ad blocking. UDM reboot or failure → all `*.blackcats.cc` unresolvable cluster-wide. No secondary DNS resolver, no fallback path.

**Consider:** A secondary resolver (Technitium on a Pi, or k8s CoreDNS exposed on the VLAN) configured as the second nameserver on DHCP. Low-priority if UDM uptime has been acceptable.

### Synology NFS — failover / recovery story

All `nfs-client` PVCs and the static `/volume2/Media` PV depend on a single Synology. Disk pool failure or controller crash = cluster-wide PVC unavailability. The design doesn't document a recovery procedure (rebuild Synology, re-export shares, re-mount PVs, restore from Filen).

**Action:** Document the Synology-loss recovery path in `RUNBOOK.md`. Pair with the Filen restore drill above.

### Secret rotation procedure (documentation only)

Sealed Secrets key rotation is intentionally disabled (`ARCHITECTURE.md` decision: "single stable key simplifies backup/restore"). If a future incident forces rotation, no procedure exists.

**Action:** Add a "Key rotation (if forced by compromise)" section to `RUNBOOK.md` documenting: (1) generate new key, (2) re-seal every committed SealedSecret with the new public cert, (3) restore new private key into the controller, (4) restart pods consuming rotated secrets. Document only — don't perform unless forced.

---

## Service Candidates

### Backend / Infrastructure

| Service | What it adds |
|---|---|
| **MinIO** | On-prem S3-compatible object store; unlocks Loki object storage mode, Grafana Tempo, and Velero without cloud deps |
| **Velero** | Kubernetes-native PVC snapshot + resource backup; cluster-level DR to complement per-app CronJob backups |
| **Grafana Tempo** | Distributed tracing backend; closes the observability triangle alongside existing metrics (Prometheus) + logs (Loki) |
| **OpenTelemetry Collector** | Unified pipeline to collect/route traces, metrics, and logs; makes adding Tempo and future backends cleaner |
| **Kyverno** | Policy-as-code admission controller; enforces resource limits, allowed image registries, no-privileged rules — complements Falco |
| **Hubble UI** | Already running Cilium — Hubble gives real-time network flow visualization and service dependency maps at no extra cost |
| **Harbor** | Private OCI registry with proxy cache and Trivy integration; reduces ghcr.io dependency and improves pull reliability |
| **KEDA** | Event-driven autoscaling; scale jobs based on queue depth rather than CPU (useful for media transcoding or backup queues) |
| **Headlamp** | Lightweight web-based Kubernetes dashboard; friendlier than raw kubectl for day-to-day inspection |

### Frontend / User Applications

| Service | What it replaces / adds |
|---|---|
| **Vaultwarden** | Bitwarden-compatible password manager — the most obvious gap in a degoogled stack |
| **Navidrome** | Self-hosted music streaming with Subsonic API; every mobile client just works |
| **Audiobookshelf** | Audiobooks + podcasts in one app; self-hosted Audible + Pocket Casts replacement |
| **Kavita** | Digital library for books, comics, and manga — companion to the existing media stack |
| **Stirling PDF** | Browser-based PDF tools (merge, split, OCR, compress); replaces half a dozen disposable web tools |
| **Mealie** | Recipe manager with meal planning and grocery lists |
| **Actual Budget** | Local-first personal finance; YNAB-style zero-based budgeting with no cloud sync required |
| **Hoarder** | Bookmark manager with automatic AI tagging and full-page snapshots; degoog for browser bookmarks |
| **Vikunja** | Self-hosted task/project manager; Todoist/TickTick replacement with CalDAV sync |
| **Syncthing** | P2P file sync across devices; complements Immich for non-photo files and replaces Google Drive sync on desktops |
| **Pterodactyl** | Game server management panel; web UI for provisioning and managing game server instances |
| **playit.gg** | Tunnel service for exposing game servers without port forwarding; companion to Pterodactyl |
| **changedetection.io** | Web page change monitoring and alerting; self-hosted alternative to Visualping/Wachete |
