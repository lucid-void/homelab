# TODO

Open work only. A finished item is deleted, not struck through.

## Broken now

- Local `talosconfig` (`~/.talos/config`) is empty — `talosctl` is unusable from the
  workstation. Backups are unaffected (`etcd-snapshot` carries its own
  `talosconfig-secret`), but design/runbook.md's recovery procedures all assume a
  working local client. Regenerate from `talhelper genconfig` output / the
  SOPS-encrypted secrets
  and verify `talosctl -n 172.16.20.11 version`.
- ~190Gi of orphaned `Released` PVs on the Synology (`gitea` valkey ×12, `vm-stack-grafana`
  ×5, `plex-config`, `tranga-config`, `freshrss-notify-state`, `postgres-2`) —
  `nfs-client` derives its share path from namespace+PVC name, so a same-named PVC
  **re-adopts the old directory** on recreate. Confirm nothing is needed, delete the PV
  objects, remove the backing directories.
- `kube-apiserver` SLO rules produce no data — vm-stack's `metric_relabel_configs` drops
  the exact histogram buckets its own bundled `kube-apiserver-burnrate`/`-histogram`/
  `-availability` rules consume, so `KubeAPIErrorBudgetBurn` can never fire despite
  having a live, non-blackholed alertmanager route. Either drop the three rule groups
  (and the route) or stop dropping the buckets.
- Orphaned `openebs-hostpath` PV `pvc-641863e1-a4aa-49e9-9594-5568086f2369` (30Gi, ex
  `vmsingle-vm-stack-victoria-metrics-k8s-stack`) is stuck `Released` with node affinity
  naming the pre-rename node `k8s-cp-3` — openebs can't resolve it, so deletion retries
  forever. Delete the PV object and the orphaned directory on cp-3 by hand.

## Planned

- **Delete Joplin.** Decided; it is the only reason Zitadel still exists. Removing it
  means `kubernetes/apps/joplin/` entire (HelmRelease, `joplin-blobs` PVC, the SAML SP
  ConfigMap, the `saml-idp-metadata` initContainer, HTTPRoute), the `joplin` database
  and managed role, `joplin-backup` and its restic repo, the `joplin.blackcats.cc` DNS
  record, and the `joplin` namespace. **Export the notes first** — the blobs PVC and the
  Postgres dump are the only copies, and the backup is deleted with it.
  `design/decisions/joplin.md` goes too.
- **Then retire Zitadel.** Every OIDC app is already on Keycloak; after Joplin it serves
  nothing. Removing it means the HelmRelease, the `zitadel` database and managed role,
  the bootstrap Job and its Terraform (the `homelab` project, the three Joplin SAML
  resources, and the eighteen `removed` blocks), the `tfstate-default-zitadel-bootstrap`
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
- **Keycloak groups and the nine browser-flow gates exist only in the server.** No CRD
  expresses group membership, group→role mapping, or `authenticationFlowBindingOverrides`,
  so none of it is reproducible from git — a realm rebuild loses every entitlement and
  reopens all nine clients to any realm user. The layout is written down in
  `design/decisions/keycloak.md`, which is a record, not a restore path. Decide whether
  to accept that or drive it from OpenTofu's Keycloak provider.
- **A new `KeycloakOIDCClient` is ungated by default.** Declaring roles in git does not
  gate anything; without its own `browser-<svc>` flow and a client binding, a new client
  is open to every realm user. Fold that into the add-a-service checklist.
- **Reconcile the remaining docs to the new Gatus/Homepage registration step.**
  Eight services had reached the cluster without ever being added to either surface —
  Jellyfin, Keycloak, Open WebUI, LiteLLM, RomM, Joplin, Obsidian LiveSync and kromgo —
  because nothing in the add-a-service path says to. All eight are wired in now
  (2026-09-20), and `design/docs/gitops.md` now carries both as steps 8 and 9 of
  "Adding a New Application" — the gap that let them drift. Still open:
  - The ungated-`KeycloakOIDCClient` item above belongs on that same checklist; it is
    not there yet.
  - `design/decisions/monitoring.md` documents gatus's two endpoint *gotchas* but not
    its coverage, so a missing endpoint is invisible. Record the three non-obvious
    probe choices made here: Keycloak is checked at
    `/realms/homelab/.well-known/openid-configuration` because its real `/health` is on
    management port 9000 and the Gateway does not route it; LiteLLM at
    `/health/liveliness` proves only the proxy process, never llama-swap behind it;
    Obsidian LiveSync asserts `401`, not `200`, because a `200` would mean CouchDB's
    `require_valid_user` had come off.
  - `design/docs/services.md`'s Homepage row says "ConfigMap-only config" and lists no
    groups; a new `AI` group now exists (Open WebUI, LiteLLM).
  Also decide whether the four services still on neither surface belong there:
  `llama-swap` and `minecraft-valkey` are ClusterIP-only by design, but `pve` and
  `synology` are real hosts with no Gatus check at all.
- **Jellyfin is deployed but not configured.** The manifests are in git; everything that
  makes it usable is not, and none of it is expressible in one:
  - Keycloak groups `/jellyfin/user` and `/jellyfin/admin`, the `browser-jellyfin` flow
    override, and a **flat roles protocol mapper** — the SSO plugin's `RoleClaim` reads a
    flat array and cannot walk `resource_access.jellyfin.roles`. Until the flow override
    exists, the client is ungated (the item above, in the concrete).
  - Plugins and the Abyss theme, per `design/decisions/jellyfin-ui.md`. Install File
    Transformation first: dependents install, report healthy and render nothing without
    it, which reads as a broken plugin rather than a missing dependency.
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
- **No backup script clears a stale restic lock.** A job killed mid-run (OOM, node
  reboot, `activeDeadlineSeconds`) leaves a lock that then blocks *every following
  night* for that repo, not just its own run: restic only auto-expires a lock it can
  prove is dead, which needs the lock's hostname to match the current host, and every
  Job pod has a unique hostname. Plain `restic unlock` therefore does not clear it —
  `--remove-all` does — and any restic command blocked on a held lock exits `rc=11` in
  ~2s. Observed 2026-08-05 (`immich-backup`, `Exit 11`: the snapshot was written, only
  `forget --prune` and `check` were skipped, so Gotify reported a failure against an
  otherwise intact repo). Add a defensive `restic unlock --remove-all` at the start of
  each backup script — safe *here* only because each repo has exactly one job and every
  CronJob is `concurrencyPolicy: Forbid`; never copy that to a shared repo.
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
- No Zitadel break-glass / account-recovery runbook. Joplin is the only app with a
  local-auth fallback (`LOCAL_AUTH_ENABLED=true`); every other app is fully gated on
  Zitadel SSO with no documented path if the admin is locked out.
- SOPS age key protection is undocumented — no record of where the single key lives,
  whether it has a passphrase, or whether an off-Synology copy exists. It decrypts
  Talos secrets and the Sealed Secrets controller key backup.
- Sealed Secrets key rotation procedure is undocumented (rotation itself is
  intentionally disabled). Write the "if forced by compromise" procedure without
  performing it.
- Matrix (Dendrite) deployment — homeserver on an `nfs-client` PVC, Zitadel OIDC/SSO
  wiring. Not yet built.
- Keycloak per-application roles — the operator, database, realm and httproute are
  deployed to `main` (`51e8ac9`, 20 files), but it currently has **zero clients
  registered**; Zitadel still serves all ten applications. Keycloak's client-level
  roles are the reason it exists — the same user can be `admin` in Gitea and `viewer`
  in FreshRSS, which Zitadel has no equivalent for short of a trigger-action plus
  custom-claim workaround. Remaining work is the staged per-application migration, not
  the deploy: pilot with one app first (Gitea) — re-register its OIDC client, update
  the callback URI/secret, update the app to read the role claim from the ID token,
  validate — then migrate the rest one at a time. End state is full replacement of
  Zitadel for these apps, staged rather than a cutover.
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
