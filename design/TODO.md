# TODO

Open work only. A finished item is deleted, not struck through.

## Broken now

- ~190Gi of orphaned `Released` PVs on the Synology (`gitea` valkey ×12, `vm-stack-grafana`
  ×5, `plex-config`, `tranga-config`, `freshrss-notify-state`, `postgres-2`, and now
  `joplin-blobs`) — `nfs-client` derives its share path from namespace+PVC name, so a
  same-named PVC **re-adopts the old directory** on recreate. Confirm nothing is needed,
  delete the PV objects, remove the backing directories.
- `kube-apiserver` SLO rules produce no data — vm-stack's `metric_relabel_configs` drops
  the exact histogram buckets its own bundled `kube-apiserver-burnrate`/`-histogram`/
  `-availability` rules consume, so `KubeAPIErrorBudgetBurn` can never fire despite
  having a live, non-blackholed alertmanager route. Either drop the three rule groups
  (and the route) or stop dropping the buckets.

## Planned

- **Joplin leftovers to clear by hand.** The manifests are gone (2026-09-21) but four
  things outlive Flux: the `joplin` database and role in the shared CNPG cluster (the
  `Database` CR's reclaim policy is `retain`, so both survive the prune —
  `DROP DATABASE joplin; DROP ROLE joplin;`), the restic repo at
  `rclone:filen:backups/restic/joplin`, the `joplin-backup` application in Gotify
  (`gotify-bootstrap` no longer manages it), and the local plaintext
  `kubernetes/apps/joplin/**/*-secret.yml` files — which hold the only copy of that
  restic repo's password, so delete them last.
- **Retire Zitadel.** Every OIDC app is on Keycloak and Joplin — its last consumer — was
  deleted on 2026-09-21, so it now serves nothing. Removing it means the HelmRelease,
  the `zitadel` database and managed role, the bootstrap Job and its Terraform (the
  `homelab` project, the three Joplin SAML resources, and the eighteen `removed`
  blocks), the `tfstate-default-zitadel-bootstrap`
  Secret, the `auth.blackcats.cc` DNS record, Mailrise, and the `auth` namespace.
  `auth/proxmox-oidc-secret` must move first — Proxmox reads it and is not going
  anywhere. Keep the Zitadel-side OIDC clients until Keycloak has run a while: they are
  unmanaged, not deleted, and are the only rollback path.
- **Rotate the GitHub PAT in `~/.claude/settings.local.json`** (workstation, not the
  cluster). It was never committed — `git log -S` finds it in no commit and a global
  gitignore covers the file — but it was read aloud into an agent transcript on
  2026-09-20 and must be treated as disclosed. Regenerate at
  https://github.com/settings/tokens. Unrelated to Flux's `github-deploy-key`, which is
  a separate sealed credential and was not exposed.
- **Keycloak groups and the ten browser-flow gates exist only in the server.** No CRD
  expresses group membership, group→role mapping, or `authenticationFlowBindingOverrides`,
  so none of it is reproducible from git — a realm rebuild loses every entitlement and
  reopens all ten clients to any realm user. The layout is written down in
  `design/decisions/keycloak.md`, which is a record, not a restore path. Decide whether
  to accept that or drive it from OpenTofu's Keycloak provider.
- **`pve` and `synology` are on neither Gatus nor Homepage.** The add-a-service path now
  covers both surfaces (`design/docs/gitops.md` steps 9 and 10, plus step 8 for gating a
  new OIDC client), and the eight services that had drifted onto the cluster unregistered
  — Jellyfin, Keycloak, Open WebUI, LiteLLM, RomM, Obsidian LiveSync, kromgo (Joplin
  too, since deleted) — are all wired in (2026-09-20). What is left is the decision: `llama-swap` and
  `minecraft-valkey` are deliberately absent (ClusterIP-only), but `pve` and `synology`
  are real hosts with no Gatus check at all.
- **Jellyfin's usable state lives outside git.** SSO is finished (2026-09-21): groups,
  the `jellyfin_roles` flat mapper, and the `browser-jellyfin` flow bound to the client,
  plus the UI plugins on the config PVC. None of it is expressible in a CRD or a
  manifest, so a realm rebuild or a lost cp-2 disk redoes all of it by hand —
  `design/decisions/jellyfin.md` and `jellyfin-ui.md` are the record. Still outstanding:
  - The **Webhook plugin → Gotify**, so Jellyfin joins the cluster's alerting path.
  - The **Abyss theme** — no Custom CSS is set, so the UI is stock.
  - The local Jellyfin admin is the break-glass path for a forked SSO plugin on the auth
    path. It must keep a real password.
- **Neither media server's config PVC is backed up.** `plex-config-local` (cp-1) and
  `jellyfin-config-local` (cp-2) are `openebs-hostpath` volumes with no backup CronJob,
  so each node's disk is a single point of loss for that server's library. Jellyfin on a
  separate node bounds the blast radius to one library instead of both — it is not a
  backup. Decide whether a restic job for the two config dirs is worth it, or accept that
  a lost node means a re-scan.
- **Seerr is pointed at Plex and cannot serve both.** It talks to one media server at a
  time, so Jellyfin users cannot request through it and Jellyfin Enhanced's Seerr
  integration has no account to act as. Decide which server owns requests.
- **`client-admin-api:v2` is EXPERIMENTAL.** Keycloak types it below PREVIEW, so any
  `keycloak-k8s-resources` bump can remove it and break `KeycloakOIDCClient`
  reconciliation. Watch for it going PREVIEW/stable, or be ready to fall back to
  console-managed clients. Logins survive either way.
- **kube-proxy is an orphaned bootstrap DaemonSet.** `cluster.proxy.disabled: true` is
  set in `talconfig.yaml` so Talos won't recreate it, but the running DaemonSet predates
  that setting, isn't in git, and Talos won't garbage-collect a bootstrap manifest it no
  longer manages. Delete it (`kubectl delete daemonset kube-proxy -n kube-system`) and
  verify service connectivity from all three control planes — no Flux/git rollback path
  for this one, so it wants a maintenance window.
- **Goldilocks/VPA recommendations are fabricated.** There is no metrics-server in the
  cluster, so the VPA recommender ingests nothing and every VPA object emits its
  configured floor rather than a measurement — `lowerBound == target == upperBound` is
  the tell. Either deploy metrics-server (needs proper kubelet serving certs on Talos)
  or delete Goldilocks + VPA outright; the 91-day `container_*` history already in
  VictoriaMetrics answers the same sizing questions via PromQL.
- **Control-plane leader-election flapping investigation was never closed.**
  kube-controller-manager/kube-scheduler intermittently lost their lease and restarted
  (~1 per 17–20 min at the worst). CPU contention and etcd disk I/O (the TSDB sharing
  etcd's disk on the leader node) were both tested and ruled out — stopping the TSDB
  write path entirely did not change the flap rate. Last finding: a crash log showed
  `etcdserver: request timed out` with etcd logs otherwise silent (no slow-apply/fsync
  warnings) and low disk/CPU/memory — the signature of raft not committing in time
  (quorum/peer/CPU-scheduling), not local disk. `listen-metrics-urls: http://0.0.0.0:2381`
  (etcd) and `bind-address: "0.0.0.0"` (controller-manager, scheduler) in
  `talconfig.yaml` were added specifically to expose the metrics needed to confirm
  this and are still live for that reason. Never checked:
  `etcd_server_proposals_pending`, `etcd_network_peer_round_trip_time_seconds`,
  `etcd_disk_backend_commit_duration_seconds`. **Unverified as of 2026-09-18:** current
  `kube-controller-manager`/`kube-scheduler` pod restart counts are low (2 per node
  over the last 20 days, vs. hundreds/week historically), which suggests the flap rate
  has dropped substantially — but this was not root-caused via the metrics above and
  must be re-checked against the live cluster before treating it as resolved.
- Move Gitea's `valkey` cluster off NFS (`emptyDir` or `openebs-hostpath`) — the bundled
  chart persists AOF to `nfs-client` PVCs, the same fsync-locking trap RomM's embedded
  Valkey avoids by using `emptyDir`. Replica placement is also imperative runtime state
  (`CLUSTER REPLICATE` run by hand so no replica shares a node with its primary) that
  lives only in `nodes.conf` and won't survive a cluster rebuild.
- Move `trivy-operator` from `standalone` to `ClientServer` mode — standalone mode scans
  a pod's containers in parallel against one shared cache volume, so multi-container
  pods can deadlock on the lock, and every scan job re-downloads the ~300MB
  vulnerability DB, which is also a chunk of the recurring `/var/lib/containerd` growth.
- Replace the `filebrowser/filebrowser` Minecraft file-manager sidecar — upstream
  archived the project 2026-09-01 with 8+ open advisories (some high severity,
  including out-of-scope file deletion via symlink-following). Migrate to the
  maintained `gtsteffaniak/filebrowser` fork (different config schema — the
  `filebrowser-init` initContainer needs reworking), drop it in favor of `kubectl cp`,
  or explicitly accept the risk — currently undecided, and the deadline has passed.
- Migrate the per-app backup CronJobs to VolSync — seven imperative restic scripts
  wearing GitOps clothes; VolSync's `ReplicationSource`/`ReplicationDestination` makes
  restore declarative and rehearsable, which also closes the next item.
- `immich-backup` still takes the deployment offline for the whole maintenance window,
  not just the backup — the cache PVC fix landed (`immich-backup-cache`, mounted at
  `/cache` via `RESTIC_CACHE_DIR`), which cut nightly downtime from ~136 min to
  ~12–13 min, but `backup.sh` still scales `immich-server` to 0 at the start and only
  scales back up via `trap cleanup EXIT`, after `restic backup` **and**
  `forget --prune` **and** `check` all finish. Decouple the scale-up from prune/check
  so the service isn't down for maintenance that never touches the live PVC. A SIGKILL
  (OOM, node reboot) mid-run skips the trap entirely and would leave immich-server
  stuck at 0 replicas indefinitely — not yet observed, but undefended.
- No backup has ever been restore-tested end-to-end. Pick one app (Immich highest
  value), restore into a clean PVC + fresh CNPG database, document the procedure in
  design/runbook.md. The etcd snapshot restore (`talosctl bootstrap --recover-from`) is
  the same story — never exercised.
- No confirmed alert path for a stuck `cert-manager` `Certificate` (`Ready: False`) or
  for a backup failing while Gotify itself is down (`gotify-secret` is wired
  `optional: true` so jobs run before bootstrap). Confirm existing coverage or add a
  secondary path (SMTP sidecar, separate webhook).
- Per-namespace NetworkPolicies — zero `CiliumNetworkPolicy` objects exist cluster-wide,
  so effectively all pods can reach all other pods. Hubble UI is now deployed
  (`hubble-relay`, `hubble-ui` in `kube-system`), which was the prerequisite for
  deriving default-deny rules from observed traffic rather than guesswork — start with
  `postgres` (cleartext intra-cluster Postgres + any pod RCE = full DB access) and
  `auth` (Zitadel).
- Cilium runs VXLAN tunnel + legacy (iptables) host routing on a single flat L2 segment
  — `routingMode: native` + `autoDirectNodeRoutes: true` + BPF masquerade/host-routing
  fit a flat single-subnet topology better and `kubeProxyReplacement` is already on. Do
  it deliberately (the cluster's only network) and re-verify the pool-b LoadBalancer
  paths (Plex `172.16.20.51`, Velocity `172.16.20.52`) after each step.
- CNPG has no WAL archiving or PITR — only the nightly `pg_dump` via `postgres-backup`.
  Add `backup.barmanObjectStore` to the `Cluster` spec once an S3-compatible target
  (e.g. MinIO) exists.
- No tested Postgres major-version upgrade procedure for the shared CNPG cluster (7 app
  databases must stay schema-compatible simultaneously).
- Immich is pinned to `v2.7.5` (kysely migrations, fragile `oauthId` re-linking after
  the Zitadel migration) with no defined upgrade criteria, rollback path, or validation
  checklist for a future major bump.
- Obsidian LiveSync shares one CouchDB **admin** credential across every device — not
  revocable per-device, far more powerful than the vault it protects. Enable LiveSync's
  client-side E2EE passphrase (stops the server reading the vault at all — highest
  value, not captured in this repo) and/or add per-device non-admin CouchDB users
  scoped to the vault DB via `_security`. Neither is built, and there is currently no
  plain-text copy of the vault anywhere in the backup chain.
- Storage capacity has no quota or real telemetry — Immich library and
  `/volume2/Media` grow uncapped; the only coverage is one 85% threshold
  (`SynologyShareAlmostFull`) off a single pinned `nfs-client` PVC's
  `kubelet_volume_stats`, not real Synology SNMP/node-exporter telemetry.
- Single offsite backup target (Filen), not immutable — a cluster compromise or a
  runaway delete script can wipe recent snapshots before 30-day retention ages them
  out. Enable restic append-only mode on a separate account, or add a second offsite
  target for the highest-value snapshots (Zitadel, SOPS age key, Sealed Secrets key,
  Immich/Paperless).
- No disaster-recovery runbook for rebuilding application data from Filen into a
  freshly-rebuilt cluster: CNPG re-seed from restic, PVC content restore, Zitadel
  bootstrap re-run, OIDC re-linking verification.
- VictoriaLogs (`vlogs`) is disabled in the vm-stack HelmRelease — container logs are
  ephemeral on each node (`/var/log/pods/`) with no forensic trail beyond the real-time
  Gotify push for Falco events.
- Image policy is minor-semver tags only — no digest pinning, no signature
  verification. Decide explicitly: accept the risk, or move to digest pinning plus
  Kyverno/cosign `verifyImages` for the custom `ghcr.io/lucid-void/*` images at
  minimum.
- UDM SE is a single point of failure for DNS as well as gateway/DHCP/ad-blocking — no
  secondary resolver for `*.blackcats.cc`.
- No documented Synology-loss recovery path (rebuild, re-export shares, re-mount PVs,
  restore from Filen).
- No Keycloak break-glass / account-recovery runbook. Jellyfin's local admin is the
  only local-auth fallback that is meant to stay (its SSO plugin is a fork sitting on
  the auth path) — Joplin's `LOCAL_AUTH_ENABLED=true` went with Joplin. Every other app
  is fully gated on Keycloak SSO with no documented path if the realm admin is locked
  out.
- SOPS age key protection is undocumented — no record of where the single key lives,
  whether it has a passphrase, or whether an off-Synology copy exists. It decrypts
  Talos secrets and the Sealed Secrets controller key backup.
- Sealed Secrets key rotation procedure is undocumented (rotation itself is
  intentionally disabled). Write the "if forced by compromise" procedure without
  performing it.
- Matrix (Dendrite) deployment — homeserver on an `nfs-client` PVC, Keycloak OIDC/SSO
  wiring (client plus its own `browser-matrix` flow gate). Not yet built.
- Karakeep deployment — bookmark/read-later archive: stores a full snapshot of each saved
  page and full-text searches it, closing the gap FreshRSS leaves (it delivers articles
  but keeps nothing). Not yet built. Needs a CNPG database, a Meilisearch sidecar for the
  search index, an `nfs-client` PVC for page snapshots and assets, a Keycloak OIDC client
  plus its `browser-karakeep` flow gate, and a restic backup CronJob. Auto-tagging runs
  against the existing LiteLLM endpoint (`llm.blackcats.cc`) over its OpenAI-compatible
  API rather than a hosted provider.
- Tdarr deployment — library-wide transcode automation over `media-nfs`, to bound the
  uncapped `/volume2/Media` growth recorded above. Not yet built. Splits into a server
  (config PVC on `nfs-client`, web UI, HTTPRoute) and one or more worker nodes that do
  the encoding; workers need `media-nfs` mounted **read-write**, unlike Plex and
  Jellyfin which mount it `readOnly`. Two constraints decide whether it is worth it:
  - **CPU-only.** The DGX Spark is not a k8s node and no Talos node has a GPU, so every
    encode competes with the cluster for the same six P-cores. Workers want a hard CPU
    limit and an off-hours schedule, and must not run while Plex or Jellyfin is
    transcoding.
  - **It replaces originals, and the media library has no backup.** Every restic repo
    covers app config and databases; `/volume2/Media` is in none of them, so a bad
    transcode policy is unrecoverable. Keep Tdarr's health-check and original-retention
    settings on until a policy has been proven against a throwaway copy.
- nftables host firewall on the Talos nodes (default-deny inbound, SSH/node_exporter/
  Promtail allowlist, per-host overrides) — not yet implemented on k8s nodes.

## Accepted gaps

- NFS and intra-cluster Postgres traffic run cleartext on the internal VLAN — accepted
  risk; private network, VPN-gated (see `AGENTS.md` key decisions).
- 64 single-replica Deployments, 2 PDBs, and `postgres-primary` allows 0 disruptions —
  a Talos rolling upgrade blocks on it and takes most services down as it proceeds.
  Reasonable homelab trade-off; design/runbook.md's upgrade section should state it
  explicitly rather than let it be discovered mid-upgrade.
- `openebs-hostpath` PVC `storage:` requests are fiction — there is no quota, so the
  real ceiling is node free space shared with containerd/etcd/logs. Accepted because
  the alerts that actually matter (`NodeFilesystemAlmostFull`, the Minecraft
  world-size exporter) measure real usage instead of the claim, so the fiction is
  cosmetic rather than dangerous.
