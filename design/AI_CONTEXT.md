# AI Context — homelab-k8s Kubernetes Cluster

Canonical reference for AI agents working on this cluster. Last updated: 2026-05-24.

---

## Cluster Topology

| Node | IP | Role | Spec |
|---|---|---|---|
| cp-1 | 172.16.20.11 | Control plane + workloads | 8 vCPU, 30 GB RAM, 100 GB disk |
| cp-2 | 172.16.20.12 | Control plane + workloads | 8 vCPU, 30 GB RAM, 100 GB disk |
| cp-3 | 172.16.20.13 | Control plane + workloads | 8 vCPU, 30 GB RAM, 100 GB disk |
| llm-1 | 172.16.20.14 | Worker — LLM inference only | 8 vCPU, 64 GB RAM, 250 GB disk |
| API VIP | 172.16.20.10 | Kubernetes API server endpoint | Floats via leader election |
| Gateway VIP | 172.16.20.50 | Ingress for all HTTP/HTTPS | Cilium L2 announcement |

**OS:** Talos Linux v1.13.2 | **k8s:** v1.36.1 | **Cluster name:** `homelab-k8s`

All three control planes run user workloads (`allowSchedulingOnControlPlanes: true`). Losing one node keeps etcd quorum (2 of 3). Rebuilt node rejoins automatically — no snapshot needed for single-node loss.

`llm-1` is the one worker: a non-etcd node dedicated to LLM inference, tainted `workload=llm:NoSchedule` and labelled `workload=llm`. Nothing schedules there without an explicit toleration. It exists because a large mmap'd model on an etcd member evicts etcd's page cache and etcd is fsync-latency-sensitive, so the memory pressure causes leader-election churn — see `design/llm-inference.md`.

**The RAM figures in the table above are the committed IaC, not the running cluster.** `llm-1` is defined in `infra/terraform/kubernetes.tf` and `kubernetes/talos/talconfig.yaml` but **not provisioned** — no `tofu apply`, no `talhelper apply`. The control planes are still running at **32 GB**; dropping them to 30 GB to fund `llm-1` is pending and requires a rolling reboot, one node at a time. Total commit is 154 GB, 4 GB over the 150 GB budget the design doc assumes — verify installed host RAM before applying.

---

## Network Layout

**Subnet:** `172.16.20.0/24` | **Gateway:** `172.16.20.254` (UDM SE) | **Domain:** `blackcats.cc`
**Pod CIDR:** `10.244.0.0/16` | **Service CIDR:** `10.96.0.0/12`

**CNI:** Cilium — VXLAN encapsulation, WireGuard node-to-node encryption, full kube-proxy replacement.

**LoadBalancer pools (Cilium L2 announcements):**
- `pool-a` → `172.16.20.50` — `shared` Gateway only (auto-selected by `gateway.networking.k8s.io/gateway-name: shared` label)
- `pool-b` → `172.16.20.51`–`172.16.20.52` — direct LoadBalancer services (add label `lbpool: pool-b`). One IP per Service, pinned with `lbipam.cilium.io/ips`: `.51` plex-direct, `.52` minecraft-proxy (Velocity). **Never share one IP across two Services** — Cilium keeps one L2 Lease per Service, so a shared IP gets announced by two nodes and resets long-lived connections. See `docs/networking.md`.

**Netbird VPN** runs as a Talos extension on every node (`wt0` interface, `100.80.x.x/16`). Three guards in `talconfig.yaml` prevent Netbird IPs from polluting Kubernetes networking: etcd `advertisedSubnets`, kubelet `nodeIP.validSubnets`, per-node kube-apiserver `advertise-address`.

**Internet exposure:** Cloudflare DNS-01 for TLS only. All A records resolve to internal IPs. No port forwarding. Access requires Netbird VPN.

---

## Ingress Model

```
GatewayClass: cilium  (io.cilium/gateway-controller)
  └── Gateway: shared  (namespace: gateway, IP: 172.16.20.50)
        ├── Listener: http   port 80   *.blackcats.cc
        ├── Listener: https  port 443  *.blackcats.cc  TLS: shared-tls (wildcard)
        │     ├── HTTPRoute: <app>  (per-app namespace)
        │     └── GRPCRoute: <app>  (Zitadel gRPC-Web)
        └── Listener: ssh    port 22   (no hostname — TCP has nothing to match on)
              └── TCPRoute: gitea-ssh  (git-over-SSH → gitea-ssh:22 → pod :2222)
```

**Gateway API CRDs:** from `kubernetes-sigs/gateway-api` v1.6.1 (experimental channel — includes GRPCRoute and the v1 TCPRoute that Cilium 1.20's TCPRoute controller watches).

The `ssh` listener is the one exception to "raw TCP goes to a pool-b LoadBalancer": Gitea advertises `git@gitea.blackcats.cc:...`, that hostname is an A record for this VIP, and one name cannot resolve to both `.50` (HTTPS) and a separate pool-b address (SSH). See `docs/networking.md`.

**TLS:** cert-manager `letsencrypt-production` ClusterIssuer → `*.blackcats.cc` wildcard cert in `gateway` namespace.

**DNS:** external-dns, Cloudflare provider, sources `gateway-httproute` + `gateway-grpcroute` + `service`. **Opt-in:** annotate route with `external-dns.alpha.kubernetes.io/enabled: "true"`. The `service` source exists only for raw-TCP workloads that bypass the Gateway entirely — currently just `minecraft-proxy` (Velocity), which publishes the Minecraft hostnames at `172.16.20.52` via an additional `external-dns.alpha.kubernetes.io/hostname` annotation. The annotation-filter applies to every source, so unannotated LoadBalancers (e.g. `plex-direct`) stay unpublished.

Every HTTPRoute must reference `parentRefs: [{name: shared, namespace: gateway}]`.

**Never use `Ingress` objects.** This cluster uses Gateway API exclusively.

---

## Auth Model

**Identity provider:** Zitadel (`auth` namespace, `auth.blackcats.cc`), backed by CNPG Postgres.

All protected services use Zitadel OIDC directly (no forward-auth proxy). Non-obvious OIDC specifics:

| App | Notes |
|---|---|
| Immich | Zitadel **Web** app type (not Native); redirect URI `https://immich.blackcats.cc/api/oauth/mobile-redirect` |
| Paperless | django-allauth 65.x; provider_id `zitadel`; callback `https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/`; requires `PAPERLESS_APPS: allauth.socialaccount.providers.openid_connect` |
| FreshRSS | Apache mod_auth_openidc; redirect URI `https://rss.blackcats.cc/i/oidc/` (NOT `/i/?get=oidc`) |
| Gitea | Callback `https://gitea.blackcats.cc/user/oauth2/Zitadel/callback` — provider name is case-sensitive |

**Joplin is the one exception — SAML, not OIDC.** Upstream has no OIDC support, only SAML. ACS `https://joplin.blackcats.cc/api/saml`, entityID `https://joplin.blackcats.cc`. No client secret exists for a SAML SP, so unlike every OIDC app nothing is written back into a k8s Secret — Joplin fetches the public IdP metadata from Zitadel at pod start. See `docs/services.md` → SAML, and the SAML entries under Non-Obvious Decisions.

Zitadel bootstrap Job provisions OIDC clients via Terraform + Zitadel API. Writes `*-oidc-secret` Secrets into app namespaces (env-var style for most apps; Helm-valuesFrom style for Gitea).

---

## Secrets Model

**k8s app secrets:** Sealed Secrets (controller `sealed-secrets-controller` in `kube-system`).
- Encrypted SealedSecret CRDs committed to git
- Seal: `kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < secret.yaml > sealed.yml`
- Key rotation disabled. Key backed up: `sops -e` → Synology `/volume2/backups/keys/sealed-secrets-key.sops.yaml`
- **Never commit raw `Secret` manifests.**

**Talos secrets:** SOPS + age via talhelper (`talsecret.sops.yaml`). Netbird key in `talenv.sops.yaml`.

**CNPG DB passwords:** `{app}-role-secret` in `postgres` namespace (CNPG-managed) → mirrored to app namespace by Reflector. Apps reference `secretKeyRef: {name: myapp-role-secret, key: password}`.

**Gotify tokens:** plain k8s Secrets written by `gotify-bootstrap` Job (not SealedSecrets). `optional: true` on backup CronJob references. Includes the `flux` app token → `monitoring/flux-gotify` (`headers` key, not `GOTIFY_TOKEN`) consumed by the `flux-notifications` Provider.

**Flux failure alerting:** `flux-notifications` (in `monitoring`) = one generic-webhook `Provider` → Gotify + one `Alert` at `eventSeverity: error` watching `Kustomization` (cluster-wide via flux-system, the gapless backstop) and `HelmRelease` (per app namespace). Closes the meta-hole where Flux — which deploys the metrics-alerting stack — could itself fail silently.

---

## GitOps Model

**Tool:** FluxCD | **Git source:** `github.com/lucid-void/Homelab` branch `main` | **Path:** `kubernetes/`

**Root Kustomization** `cluster-apps` → `kubernetes/apps/` — applies all application Kustomizations.

**Repo layout:**
```
kubernetes/
├── bootstrap/helmfile.yml     # Pre-Flux: prometheus-crds + Cilium + Spegel + Sealed Secrets
├── bootstrap/flux/            # kubectl apply -k entrypoint for Flux install
├── flux/
│   ├── config/                # GitRepository + root cluster Kustomization
│   ├── apps.yml               # cluster-apps Kustomization → kubernetes/apps/
│   ├── repositories/          # HelmRepositories, GitRepositories, OCI sources
│   └── vars/                  # cluster-settings ConfigMap + cluster-secrets SealedSecret
└── apps/
    └── <namespace>/
        ├── namespace.yml
        ├── kustomization.yml  # lists ks.yml files
        └── <app>/
            ├── ks.yml         # Flux Kustomization: dependsOn, path, targetNamespace
            └── app/           # HelmRelease, SealedSecret, PVC, HTTPRoute, …
```

**No `infrastructure/` split.** Operators (Cilium, Sealed Secrets, CNPG, democratic-csi) are in `apps/kube-system/`, `apps/cnpg-system/`, etc., ordered by `dependsOn`.

**Variable substitution:** `${CLUSTER_DOMAIN}` = `blackcats.cc`, `${CLUSTER_NAME}` = `homelab-k8s` from `cluster-settings` ConfigMap.

**Flux Kustomization `targetNamespace`** overrides namespace on ALL resources, including explicit `metadata.namespace`. Cross-namespace RBAC must be in a separate Kustomization without `targetNamespace` (see `apps/auth/bootstrap-rbac/`).

**All cluster changes go through git.** No `kubectl apply` for config changes.

---

## Key Services Inventory

| Service | Namespace | Hostname | Auth |
|---|---|---|---|
| Zitadel | auth | `auth.blackcats.cc` | Self (IDP) |
| Immich | immich | `immich.blackcats.cc` | Zitadel OIDC |
| Paperless-ngx | paperless | `paperless.blackcats.cc` | Zitadel OIDC |
| Proton Mail Bridge | paperless | — (ClusterIP `protonmail-bridge:143`, IMAP only) | Proton account, entered once by hand |
| Gitea | gitea | `gitea.blackcats.cc` | Zitadel OIDC |
| FreshRSS | freshrss | `rss.blackcats.cc` | Zitadel OIDC |
| Homebox | homebox | `homebox.blackcats.cc` | Built-in |
| Joplin Server | joplin | `joplin.blackcats.cc` | Zitadel **SAML** (+ local break-glass admin) |
| Homepage | homepage | `home.blackcats.cc` | None |
| Gotify | monitoring | `gotify.blackcats.cc` | SealedSecret admin creds |
| Gatus | monitoring | `gatus.blackcats.cc` | Zitadel OIDC |
| Goldilocks | goldilocks | `goldilocks.blackcats.cc` | Zitadel OIDC |
| Plex | media | `plex.blackcats.cc` | Built-in |
| Sonarr | media | `sonarr.blackcats.cc` | Built-in |
| Radarr | media | `radarr.blackcats.cc` | Built-in |
| Prowlarr | media | `prowlarr.blackcats.cc` | Built-in |
| SABnzbd | media | `nzb.blackcats.cc` | Built-in |
| Seerr | media | `seerr.blackcats.cc` | Built-in |
| Minecraft `matcha` (Paper, plugins) | media | `matcha.blackcats.cc` — **TCP 25565 via the Velocity proxy, not the Gateway** | None (VPN-gated); files UI at `matcha-files.blackcats.cc` |
| Minecraft `vanilla` (Paper, plugin-free) | media | `vanilla.blackcats.cc` — **TCP 25565 via the Velocity proxy, not the Gateway** | None (VPN-gated); files UI at `vanilla-files.blackcats.cc` |
| minecraft-proxy (Velocity 3.5.1) | media | LoadBalancer `172.16.20.52:25565` (own IP; never share with Plex) | — |
| minecraft-valkey | media | ClusterIP `:6379` — QuickChat cross-server chat bus | — |
| minecraft-events | media | — (no Service) — Velocity log watcher → Gotify player join/leave notifications | — |
| CNPG cluster `postgres` | postgres | — | Per-DB roles |

Full inventory with storage details: `docs/services.md`.

---

## Non-Obvious Decisions

**app-template naming:** Single controller named `app` → Deployment/Service is `{release-name}` (no suffix). Two+ controllers/services → `{release-name}-{name}`. HTTPRoute `backendRef.name` must match exactly. Always verify with `kubectl get svc -n <namespace>` before writing.

**Flux targetNamespace pitfall:** `spec.targetNamespace` overrides ALL namespace fields unconditionally. Cross-namespace resources must be in a separate Kustomization without targetNamespace.

**Talos kubelet mount namespace:** Kubelet runs in a private mount namespace. Pod `hostPath` writes are invisible to the kubelet unless `machine.kubelet.extraMounts` is configured (required for OpenEBS `openebs-hostpath`). This is already patched in `talconfig.yaml`.

**Static NFS PV nfsvers:** Talos kernel supports NFSv4 only for host-level static PV mounts. Always `nfsvers=4` in PV `mountOptions`. Democratic-csi dynamic PVCs are unaffected.

**Proton Mail Bridge cert has one SAN, `IP:127.0.0.1`:** Proton Mail is E2E-encrypted, so Paperless reads mail only through Proton Mail Bridge, which serves STARTTLS with a self-signed cert (no plaintext option exists). That cert names **only `127.0.0.1`** — no DNS SANs — and Paperless verifies hostnames unconditionally (`ssl.create_default_context()`), so trusting the cert is only half the job: the address dialled must literally be `127.0.0.1`. A plain-TCP socat sidecar in the Paperless pod provides that. See `decisions/protonmail-bridge.md`.

**`shenxn/protonmail-bridge` is stale despite active commits:** its CI has failed on every version bump for ~16 months, so the newest *published* image is from 2025-04 while `VERSION` tracks upstream. Check registry tags, not the commit log. We use `ghcr.io/videocurio/proton-mail-bridge`.

**Plex SQLite over NFS:** NFS causes SQLite WAL locking errors in Plex. Config PVC uses `openebs-hostpath` (local disk), not `nfs-client`.

**gotify-bootstrap Job immutability:** Job spec is immutable after creation. If the manifest changes while the completed Job is within 24h TTL, delete it and let Flux recreate: `kubectl delete job gotify-bootstrap -n monitoring`.

**Stakater Reloader image tag:** Chart v1.0.112 has a `vv1.0.112` double-v appVersion bug causing ImagePullBackOff. Override: `reloader.deployment.image.tag: "v1.0.112"` in HelmRelease values.

**Sonarr/Radarr use CNPG Postgres** (migrated from SQLite). The migration Jobs are in `kubernetes/apps/media/sonarr/app/migration-job.yml` and `radarr/app/migration-job.yml`.

**rclone Filen backend** requires rclone ≥ v1.69. Alpine's `apk add rclone` installs an older version. Always use the official binary from `downloads.rclone.org`.

**backup-tools image** (`ghcr.io/lucid-void/backup-tools`) contains bash, curl, kubectl, restic, rclone, postgresql17-client — but NOT jq or python3. Scripts needing JSON parsing use `alpine:3.21` + `apk add bash curl jq kubectl` at container startup.

**YAML block scalar + Python:** Python code at 0-indent inside a YAML `|` block scalar breaks the kustomize YAML parser. Put Python scripts as separate ConfigMap keys.

**Security namespace PSA:** `security` namespace requires `pod-security.kubernetes.io/enforce: privileged` for Falco (privileged container + hostPath volumes).

**Trivy Operator dbRepository:** Do not override with a full `ghcr.io/...` path — the chart prepends the registry, causing double-prefix. Leave at chart default.

**CNPG managed-role race on a new app.** A new service adds its `{app}-role-secret` SealedSecret (in its own `{app}-database` Kustomization) and its managed role (in `postgres-cluster`). These reconcile near-simultaneously, and if CNPG evaluates the role before Sealed Secrets has decrypted the secret it records `cannotReconcile: failed to get password secret ... not found` and **never retries** — the status stays frozen. Downstream: no role → the `Database` CR fails with `role "x" does not exist` → no database → the app crash-loops on `password authentication failed` (SQLSTATE 28P01). `flux reconcile` does **not** fix it, because the `Cluster` object already matches git so no watch event fires. Nudge the object directly: `kubectl annotate cluster postgres -n postgres reconcile-nudge="$(date +%s)" --overwrite`, then remove the annotation. Verify with `kubectl get cluster postgres -n postgres -o jsonpath='{.status.managedRolesStatus}'`. Hit while adding Joplin.

**Joplin probes need an explicit `Host` header.** Joplin Server picks its API vs website router from the request host and 404s anything matching neither. The kubelet probes by pod IP, so a plain `httpGet: /api/ping` always fails and the pod restart-loops while the app is perfectly healthy. Both probes set `httpHeaders: [{name: Host, value: joplin.blackcats.cc}]`. Diagnose with `curl -H 'Host: joplin.blackcats.cc' http://<podIP>:22300/api/ping` (200) vs without (404).

**Zitadel→Joplin SAML attribute mapping.** Zitadel emits `Email`, `FullName`, `FirstName`, `SurName`, `UserName`, `UserID`. Joplin does a hard-coded, case-sensitive lookup for exactly `email` and `displayName` (`routes/api/login.ts`). Without a bridge every SSO login fails. `zitadel_action.joplin_saml_attributes` (trigger `FLOW_TYPE_SAML_RESPONSE` / `TRIGGER_TYPE_PRE_SAML_RESPONSE_CREATION`) adds the two lowercase aliases via `api.v1.attributes.setCustomAttribute(key, nameFormat, value)`, which only adds keys not already present, leaving the stock attributes intact.

**That action must coerce, never `typeof`-check.** Zitadel's action user object (`internal/actions/object/user.go`) types `DisplayName` as a plain Go `string` but `Email` as `domain.EmailAddress` — a *named* string type, which goja does **not** surface as a JS string primitive. A `typeof x === 'string'` guard therefore passes for displayName and silently drops email. Use `String(value)` coercion. The struct is flat (`human.email`, `human.displayName`) — there is no nested `human.profile` or `human.email.email`.

**Joplin 3.7.1 has no SAML attribute type guards** (they exist only on `dev`), so a missing attribute surfaces as the opaque `Could not login using email "undefined" and displayName "..."` — that `"undefined"` is a JS `undefined` interpolated into a template literal, i.e. **the attribute was absent**, not malformed. Read the tag matching the deployed version, not `dev`.

**Never create a local Joplin account with a Zitadel email.** `UserModel.ssoLogin` refuses a SAML assertion for an email already owned by a password-based account (`if (user && !user.is_external) return null;`) and never upgrades `is_external`. SAML for that address is then permanently blocked; the only fix is deleting the user or editing the DB. SAML auto-provisions on first login, so no pre-creation is needed. `admin@localhost` stays local as break-glass.

**Joplin backup needs podAffinity, unlike immich-backup.** `joplin-blobs` is RWO and the job must mount it *while the Joplin pod still holds it* (scale-down happens inside the script), so the job is pinned to the app's node. `immich-backup` skips this only because `immich-library` is RWX.

**etcd snapshot CronJob** (`kube-system/etcd-snapshot`, daily 01:00): downloads `talosctl` at runtime (version pinned in script — update alongside Talos upgrades), tries CP nodes `.11 → .12 → .13` in order, uploads via restic to `rclone:filen:backups/restic/etcd-snapshot`. 30-day retention (`restic forget --keep-daily 30 --prune`) plus a `restic check --read-data-subset=1/10` spot-check. Three SealedSecrets required: `restic-secret` (RESTIC_PASSWORD + RESTIC_REPOSITORY), `rclone-secret` (rclone.conf with Filen creds), `talosconfig-secret` (talosconfig file from `~/.talos/config`). Gotify token is provisioned by gotify-bootstrap (`etcd-snapshot` → `kube-system/gotify-secret`); the CronJob still references it with `optional: true` so it runs before bootstrap completes. Restore procedure: RUNBOOK → "Restore etcd from a snapshot" (`talosctl bootstrap --recover-from`) — documented but **never restore-tested**.
