# TODO

Open work only. A finished item is deleted, not struck through. Grep this file for your
topic rather than reading it whole; owner-only items are at the top.

## Planned

- **CRITICAL — needs the owner. Put the recovery keys somewhere that survives losing the rack.**
  The Sealed Secrets controller key backup and the SOPS age key copy both sit on the Synology
  (`/volume2/backups/keys/`); the only other copies are `recovery.key` on the workstation and a
  paper copy of the age key. The restic and rclone credentials for Filen exist in git only as
  SealedSecrets. Lose the Synology and the workstation together and the offsite backups cannot
  be decrypted. To do: store the Sealed Secrets key, the age key, and each restic password and
  rclone config in a password manager or a second offsite location; then record where in
  `design/docs/secrets.md`, including whether the age key has a passphrase and how
  `/mnt/backups` is mounted on the workstation (the runbook's restore commands depend on it).
- **CRITICAL — needs the owner. Restore-test one backup end to end.** No application snapshot
  and no etcd snapshot has ever been restored. Pick one app (Immich is the highest value),
  restore into a clean PVC and a fresh CNPG database, and write the procedure that worked into
  `design/runbook.md` — today it only restores to a scratch directory. The etcd path
  (`talosctl bootstrap --recover-from`) is equally unexercised.
- **CRITICAL — needs the owner. The gitignored `*-secret.yml` plaintext files are the only
  source for re-sealing, and they are on one workstation.** If the controller key is lost,
  every SealedSecret has to be re-sealed from plaintext; some sealed files have no plaintext
  beside them at all (`design/docs/secrets.md`). Decide where the plaintext lives (password
  manager, or SOPS-encrypted in git), then write the re-seal procedure in `design/runbook.md`.
- **Needs the owner. Back up the OpenTofu state or move it to a remote backend.** It is the
  local gitignored `infra/terraform/terraform.tfstate`; the `backend "pg"` block in
  `infra/terraform/provider.tf` is commented out. Losing it means importing the existing VMs
  and DNS records by hand.
- **Replace the UPS.** The Eaton Ellipse PRO 850 lasts seconds at real load, so a power cut still
  hard-crashes everything. Requirements and the post-install test are in `design/decisions/ups.md`.
- **Rotate the Sonarr Postgres password.** Sonarr prints it in its startup log, and it landed in an
  agent transcript on 2026-11-08.
- **ZeroTier VM (.23) stayed down after both 2026-11-08 restarts.** Find out why it doesn't start
  with the Proxmox host.
- **NUT Pi (`nut-server`, 172.16.20.7) integrations.** The grouped shutdown is configured
  end to end (`design/decisions/ups.md`). Still to do: a NUT exporter + VMRule alerts (on battery, FSD,
  a client missing from `ups-cluster`), Gatus/Homepage entries, a UniFi DHCP reservation and
  DNS name, and rotating the Pi's root password (it was shared in an agent session).
- **Public DNS records are wrong for two services.** `pi.blackcats.cc` is `172.16.20.1` in
  Cloudflare (`ip_last = 1` in `infra/terraform/external.tf`; the Pi is `.7`). `matcha` and
  `vanilla.blackcats.cc` have no public record despite the external-dns annotation. The LAN
  resolvers mask both; Netbird clients still get them wrong.
- **Find what makes Plex run out of memory overnight.** OOM-killed about every 28 minutes
  between 00:25 and 04:25 UTC at the old 4Gi limit; raised to 8Gi on 2026-11-09 without
  establishing the cause. If kills continue, find the job.
- **Ignore stale news articles.** Skip articles published more than 3 days ago (needs the
  `published` field from the FreshRSS API; old Steam patch notes still get through today).
- **Rotate the RapidAPI (AeroDataBox) key.** It was pasted into an agent transcript on
  2026-09-30 and must be treated as disclosed. Regenerate it on RapidAPI, then re-seal
  `kubernetes/apps/monitoring/flight-tracker/app/aerodatabox-sealed.yml` (plaintext in the
  gitignored `aerodatabox-secret.yml`, sealed with the repo's `kubeseal` command).
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
  new OIDC client), and the seven services that had drifted onto the cluster
  unregistered — Jellyfin, Keycloak, Open WebUI, LiteLLM, RomM, Obsidian LiveSync,
  kromgo — are all wired in (2026-09-20). What is left is the decision: `llama-swap` and
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
- **Needs the owner. Twenty PVCs have no backup job and no recorded reason.** `coverage-audit`
  found them; each is exempted as `UNDECIDED` in
  `kubernetes/apps/monitoring/coverage-audit/app/exemptions.json` so the audit stays quiet
  (`grep UNDECIDED` on that file is the list). Decide per PVC: add a backup job, or replace
  the reason with the real one. They are `open-webui-data`, `degoog-data`, `freshrss-data`,
  `gitea-shared-storage`, ten media `*-config` volumes (crosswatch, kavita, prowlarr, radarr,
  romm, sabnzbd, seerr, sonarr, suwayomi, tdarr), `flight-tracker-data`, `gatus-data`,
  `gotify-data`, `vm-stack-grafana`, and the Plex and Jellyfin config PVCs (their own item
  above).
- **Build `restore-drill`: a weekly CronJob that proves a backup restores.** Each run picks
  one app in rotation, restores its latest restic snapshot from Filen into a scratch PVC and
  a throwaway CNPG database, checks the result (the dump loads, row and file counts are
  sane, the snapshot is under 48 h old), posts pass or fail to Gotify, and tears everything
  down. Start with Homebox (SQLite only), then add an app at a time; Immich is the highest
  value and the hardest. It automates the restore test above, it does not replace doing
  one by hand first. Not yet designed.
- `immich-backup` still takes the deployment offline for the whole maintenance window,
  not just the backup — the cache PVC fix landed (`immich-backup-cache`, mounted at
  `/cache` via `RESTIC_CACHE_DIR`), which cut nightly downtime from ~136 min to
  ~12–13 min, but `backup.sh` still scales `immich-server` to 0 at the start and only
  scales back up via `trap cleanup EXIT`, after `restic backup` **and**
  `forget --prune` **and** `check` all finish. Decouple the scale-up from prune/check
  so the service isn't down for maintenance that never touches the live PVC. A SIGKILL
  (OOM, node reboot) mid-run skips the trap entirely and would leave immich-server
  stuck at 0 replicas indefinitely — not yet observed, but undefended.
- No confirmed alert path for a stuck `cert-manager` `Certificate` (`Ready: False`) or
  for a backup failing while Gotify itself is down (`gotify-secret` is wired
  `optional: true` so jobs run before bootstrap). Confirm existing coverage or add a
  secondary path (SMTP sidecar, separate webhook).
- Per-namespace NetworkPolicies — zero `CiliumNetworkPolicy` objects exist cluster-wide,
  so effectively all pods can reach all other pods. Hubble UI is now deployed
  (`hubble-relay`, `hubble-ui` in `kube-system`), which was the prerequisite for
  deriving default-deny rules from observed traffic rather than guesswork — start with
  `postgres` (cleartext intra-cluster Postgres + any pod RCE = full DB access).
- Cilium runs VXLAN tunnel + legacy (iptables) host routing on a single flat L2 segment
  — `routingMode: native` + `autoDirectNodeRoutes: true` + BPF masquerade/host-routing
  fit a flat single-subnet topology better and `kubeProxyReplacement` is already on. Do
  it deliberately (the cluster's only network) and re-verify the pool-b LoadBalancer
  paths (Plex `172.16.20.51`, Velocity `172.16.20.52`) after each step.
- CNPG has no WAL archiving or PITR — only the nightly `pg_dump` via `postgres-backup`,
  so up to 24 h of writes are lost on a restore. Add it with the Barman Cloud **plugin**
  (in-tree `barmanObjectStore` is removed in CNPG 1.31). Needs the owner first: pick an
  S3-compatible target that is not the Synology the data already lives on (Cloudflare R2's
  free tier is 10 GB; check the cluster's size against it). Then: the `plugin-barman-cloud`
  chart beside the operator, a sealed credentials Secret, an `ObjectStore` with a
  `retentionPolicy`, a `plugins` entry on the `Cluster` (`isWALArchiver: true`), and a
  `ScheduledBackup` (`method: plugin`; its cron has a leading seconds field). Worked
  reference: `apps/infisical/` in github.com/jakehulberg/homelab. Enabling archiving rolls
  the pods; if the old primary hangs in Terminating on `wal archive plugin is not
  available`, force-deleting that pod is safe. Finish with a restore into a new `Cluster`
  — an untested PITR is not a backup.
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
  target for the highest-value snapshots (Keycloak, SOPS age key, Sealed Secrets key,
  Immich/Paperless).
- No disaster-recovery runbook for rebuilding application data from Filen into a
  freshly-rebuilt cluster: CNPG re-seed from restic, PVC content restore, Keycloak
  realm re-import, OIDC re-linking verification.
- VictoriaLogs (`vlogs`) is disabled in the vm-stack HelmRelease — container logs are
  ephemeral on each node (`/var/log/pods/`) with no forensic trail beyond the real-time
  Gotify push for Falco events.
- Image policy is minor-semver tags only — no digest pinning, no signature
  verification. Decide explicitly: accept the risk, or move to digest pinning plus
  Kyverno/cosign `verifyImages` for the custom `ghcr.io/lucid-void/*` images at
  minimum.
- UDM SE is still a single point of failure for the gateway and DHCP. DNS no longer depends
  on it (`design/decisions/dns.md`).
- No documented Synology-loss recovery path (rebuild, re-export shares, re-mount PVs,
  restore from Filen).
- No Keycloak break-glass / account-recovery runbook. Jellyfin's local admin is the
  only local-auth fallback that is meant to stay (its SSO plugin is a fork sitting on
  the auth path). Every other app is fully gated on Keycloak SSO with no documented
  path if the realm admin is locked out.
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
- Tdarr: manifests built, see `design/decisions/tdarr.md`. Still to do after deploy: the test-encode
  phase, creating the libraries and flow in the UI, the pilot and the full run. Open: the 3–6 Mbps
  H.264 band (about 714 GB) is outside the v1 filter.
- Host firewall on the Talos nodes (default-deny inbound with an allowlist) — not yet
  implemented. Talos has no SSH and no nftables config of its own to edit, so this means
  `NetworkRuleConfig` documents in `talconfig.yaml`.
- **youtube-queue: auto-delete watched videos.** Today watched videos are deleted by
  hand in Jellyfin. Next step: a small CronJob that asks the Jellyfin API for played
  items under `/YouTube` older than N days and deletes them. Needs a Jellyfin API key
  (SealedSecret). See `design/decisions/youtube-queue.md`.
- **youtube-queue: a broken yt-dlp still exits 0.** ytdl-sub logs the yt-dlp error and
  reports `Success`, so the CronJob stays green when YouTube breaks the pinned version
  and `KubeJobFailed` never fires. Fix by grepping the log for `ERROR:` and exiting
  non-zero, or by alerting when `/Media/YouTube` hasn't changed in N days.
- **youtube-queue: PO token provider not deployed.** Only needed if downloads start failing
  with `Sign in to confirm you're not a bot` or 403s on formats. Deploy
  `bgutil-ytdlp-pot-provider` as a Deployment + Service (not a sidecar, which would keep
  the Job from finishing), and add its yt-dlp client plugin — it isn't in the ytdl-sub
  image.

## Accepted gaps

- NFS and intra-cluster Postgres traffic run cleartext on the internal VLAN — accepted
  risk; private network, VPN-gated (see `design/architecture.md` → Key decisions).
- 64 single-replica Deployments, 2 PDBs, and `postgres-primary` allows 0 disruptions —
  a Talos rolling upgrade blocks on it and takes most services down as it proceeds.
  Reasonable homelab trade-off; design/runbook.md's upgrade section should state it
  explicitly rather than let it be discovered mid-upgrade.
- `openebs-hostpath` PVC `storage:` requests are fiction — there is no quota, so the
  real ceiling is node free space shared with containerd/etcd/logs. Accepted because
  the alerts that actually matter (`NodeFilesystemAlmostFull`, the Minecraft
  world-size exporter) measure real usage instead of the claim, so the fiction is
  cosmetic rather than dangerous.
- **flight-tracker rough edges, accepted (2026-09-30 review):** a state file that is
  non-UTF-8 or valid JSON but not an object still crash-loops the pod (only invalid JSON
  is moved aside); `http.client.HTTPException` subclasses (`IncompleteRead`,
  `BadStatusLine`) are not caught, so one can kill the receiver thread and restart the
  pod; a delay longer than the flight time plus 6 h can drop a flight while it is still
  `Delayed`; a snapshot with no times is polled every 6 h forever; an expected time that
  flaps between present and absent alerts each time; the budget counter resets on the
  calendar month while RapidAPI's cycle resets on the subscription day (read
  `x-ratelimit-api-units-remaining` to fix); `poll_due` holds the state lock across network
  calls, so a reply can lag behind a slow poll.
