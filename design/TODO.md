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

### Kavita missing from README service list

Kavita is **already deployed** (manga stack, `media` namespace — see `.claude/CLAUDE.md` and
`design/docs/services.md`), but the top-level README's service inventory doesn't list it, and it
lingered in the Service Candidates table below as if un-built. Removed from candidates; **add Kavita
to the README/service inventory** so docs match reality.

---

## Needs Verification

### CNPG WAL archiving

CNPG currently does base backups only — WAL archiving is not configured. Without WAL archiving, point-in-time recovery is not possible; recovery is limited to the last daily snapshot.

**Consider:** adding `backup.barmanObjectStore` to the CNPG Cluster spec for WAL archiving to Synology or Filen.

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

### Migrate per-app backup CronJobs to VolSync

The five backup CronJobs (immich, paperless, gitea, homebox, postgres) work, but they're imperative
scripts wearing GitOps clothes — five copies of similar logic, each a custom image. **VolSync**'s
restic mover gives the same restic→rclone-compatible result as a declarative `ReplicationSource`
per PVC, with built-in scheduling, pruning, and a `ReplicationDestination` CRD that makes *restores*
declarative too. That directly addresses the "Backup restore actually works" item above: restore
becomes a manifest you can rehearse, not a runbook you improvise.

(Why VolSync over Velero: Velero's main value is cluster-*resource* backup, which Git already covers
in this setup. For PVC data the declarative restic flow is the better fit — so VolSync is the better
first move; Velero stays a "maybe later" for full-cluster DR.)

**Action:** Pilot VolSync on one PVC matching the existing restic repo layout
(`rclone:filen:backups/restic/{name}`), prove a restore via `ReplicationDestination`, then migrate
the rest. Pair with the Immich restore drill.

### Dedicated CNPG cluster for Zitadel (blast-radius isolation)

Zitadel — the single OIDC provider gating Immich, Paperless, Gitea, FreshRSS, Goldilocks, Gatus —
currently shares the one CNPG cluster with six other app databases. Seven DBs in one Postgres is
fine for FreshRSS and Homebox; it's questionable for the thing that gates *everything*. A CNPG
failover hiccup or a major-version upgrade gone wrong takes down auth for every service
simultaneously — including the dashboards you'd use to debug it. A dedicated **single-instance CNPG
cluster for Zitadel** (with its own `barmanObjectStore` once MinIO exists) isolates the blast radius
and decouples Zitadel from the shared-cluster Postgres 16→17 upgrade problem (see "Postgres major
version upgrade plan").

**Action:** Stand up a separate CNPG `Cluster` for Zitadel; migrate the `zitadel` database via
logical dump+restore; repoint Zitadel; give it an independent WAL-archiving target once MinIO lands.

### Per-namespace NetworkPolicies

Cilium supports L7 NetworkPolicies. Currently no `NetworkPolicy` or `CiliumNetworkPolicy` resources are deployed — all pods can reach all other pods. Adding default-deny + per-namespace allow rules would mirror the Swarm overlay isolation model.

**Priority targets** (highest blast-radius first):
1. `postgres` namespace — allow ingress only from declared app namespaces; combined with cleartext intra-cluster Postgres traffic, any pod RCE currently = full DB access
2. `auth` namespace (Zitadel) — allow ingress only from gateway + OIDC clients
3. `cert-manager`, `flux-system`, `kube-system` — restrict egress and cross-namespace ingress

**Prerequisite — observe before enforcing:** writing default-deny policies blind is how Zitadel
breaks at 11pm. Enable **Hubble UI** first (effectively free — Cilium is already running), watch
actual flows for ~a week, then derive `CiliumNetworkPolicy` for the `postgres` and `auth`
namespaces from *observed* traffic instead of guesswork. (See Service Candidates ordering — Hubble
UI is sequenced specifically as the gate to this work.)

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

### Recommended implementation order

Dependency-aware ordering rather than the raw lists below:

1. **MinIO first** — it unlocks the CNPG fix. WAL archiving / PITR is the scariest single gap (one
   shared Postgres backing seven apps incl. Zitadel, currently dump-only). MinIO on OpenEBS hostpath
   (or Synology iSCSI) gives an S3 target for `backup.barmanObjectStore`, taking the Postgres story
   from "yesterday's dump" to point-in-time recovery. It *also* unblocks Velero/VolSync and
   VictoriaLogs object storage later. **Deploy it for CNPG, not as an abstract building block.**
2. **Hubble UI second** — the prerequisite to the NetworkPolicies work (see Future Work). Effectively
   free since Cilium is already running. Enable it, watch real flows for a week, then write
   `CiliumNetworkPolicy` for `postgres`/`auth` from observed traffic instead of guessing.
3. **Vaultwarden third** — the most glaring *functional* gap in a degoog stack that already covers
   identity, photos, docs, and RSS. Fits existing patterns exactly: CNPG database, Sealed Secret,
   HTTPRoute, restic CronJob. **Caveat:** make it the first app you run a full restore drill on —
   even before the Immich drill above — because a password vault you can't restore is worse than no
   vault.
4. **Kyverno fourth, scoped narrowly** — deploy it to *resolve the image-provenance decision* (see
   "Image digest pinning / signature verification"), not as a general policy engine. A single
   `verifyImages` policy for `ghcr.io/lucid-void/*` (cosign-sign the two custom images in the
   existing GitHub Actions workflows) plus a registry allowlist gets ~90% of the value with minimal
   admission-webhook blast radius. **Exclude `kube-system` and `flux-system` from enforcement** so a
   Kyverno outage can't brick reconciliation.

**Deprioritized (with reasons):**
- **Harbor** — Spegel already provides pull-through caching and registry-outage resilience; Harbor
  adds a stateful service to babysit for marginal gain.
- **Tempo / OpenTelemetry Collector** — no instrumented apps emit traces today; it'd be a backend
  with nothing to ingest. Revisit only once apps emit spans.
- **Headlamp** — fine, but k9s via mise costs zero cluster resources for the same day-to-day
  inspection.

**Lowest-friction frontend wins:** **Navidrome** and **Audiobookshelf** — the `media` namespace, NFS
PV, and Gateway patterns already exist, so these are near-drop-in.

### Backend / Infrastructure

| Service | What it adds |
|---|---|
| **MinIO** | **① Do first.** On-prem S3-compatible object store; unlocks CNPG WAL archiving / PITR (`barmanObjectStore`), plus Velero/VolSync and VictoriaLogs object storage. Deploy *for CNPG* first |
| **Hubble UI** | **② Do second.** Cilium already running — real-time network flow visualization + service maps at no extra cost; the prerequisite for writing NetworkPolicies from observed traffic |
| **Kyverno** | **④ Scoped only.** Policy-as-code admission controller; use it narrowly to enforce image provenance (`verifyImages` for `ghcr.io/lucid-void/*` + registry allowlist), **excluding `kube-system`/`flux-system`** |
| **VolSync** | Declarative restic backup/restore per PVC (`ReplicationSource`/`ReplicationDestination`); replaces the five imperative per-app backup CronJobs with GitOps-native, restore-rehearsable manifests — see Future Work |
| **Velero** | Kubernetes-native PVC snapshot + resource backup; cluster-level DR. _Lower priority — cluster resources are already in Git; prefer VolSync for PVC data_ |
| **Grafana Tempo** | _Deprioritized — no instrumented apps emit traces yet, so it'd be a backend with nothing to ingest._ Distributed tracing backend; revisit once apps emit spans |
| **OpenTelemetry Collector** | _Deprioritized (same reason as Tempo)._ Unified pipeline to collect/route traces, metrics, and logs |
| **Harbor** | _Deprioritized — Spegel already gives pull-through caching + registry-outage resilience; Harbor is a stateful service to babysit for marginal gain._ Private OCI registry with proxy cache + Trivy |
| **KEDA** | Event-driven autoscaling; scale jobs based on queue depth rather than CPU (useful for media transcoding or backup queues) |
| **Headlamp** | _Deprioritized — k9s via mise gives the same day-to-day inspection at zero cluster cost._ Lightweight web-based Kubernetes dashboard |

### Frontend / User Applications

| Service | What it replaces / adds |
|---|---|
| **Vaultwarden** | **③ Do third.** Bitwarden-compatible password manager — the most obvious functional gap in the degoog stack. Fits existing patterns (CNPG + Sealed Secret + HTTPRoute + restic CronJob). **Run its full restore drill before Immich's** |
| **Navidrome** | _Lowest-friction win — `media` ns + NFS PV + Gateway patterns already exist._ Self-hosted music streaming with Subsonic API; every mobile client just works |
| **Audiobookshelf** | _Lowest-friction win (same reason as Navidrome)._ Audiobooks + podcasts in one app; self-hosted Audible + Pocket Casts replacement |
| **Stirling PDF** | Browser-based PDF tools (merge, split, OCR, compress); replaces half a dozen disposable web tools |
| **Mealie** | Recipe manager with meal planning and grocery lists |
| **Actual Budget** | Local-first personal finance; YNAB-style zero-based budgeting with no cloud sync required |
| **Hoarder** | Bookmark manager with automatic AI tagging and full-page snapshots; degoog for browser bookmarks |
| **Vikunja** | Self-hosted task/project manager; Todoist/TickTick replacement with CalDAV sync |
| **Syncthing** | P2P file sync across devices; complements Immich for non-photo files and replaces Google Drive sync on desktops |
| **Pterodactyl** | Game server management panel; web UI for provisioning and managing game server instances |
| **playit.gg** | Tunnel service for exposing game servers without port forwarding; companion to Pterodactyl |
| **changedetection.io** | Web page change monitoring and alerting; self-hosted alternative to Visualping/Wachete |
