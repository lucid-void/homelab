# AI Context — homelab-k8s Kubernetes Cluster

Canonical reference for AI agents working on this cluster. Last updated: 2026-05-24.

---

## Cluster Topology

| Node | IP | Role | Spec |
|---|---|---|---|
| cp-1 | 172.16.20.11 | Control plane + workloads | 4 vCPU, 16 GB RAM, 60 GB disk |
| cp-2 | 172.16.20.12 | Control plane + workloads | 4 vCPU, 16 GB RAM, 60 GB disk |
| cp-3 | 172.16.20.13 | Control plane + workloads | 4 vCPU, 16 GB RAM, 60 GB disk |
| API VIP | 172.16.20.10 | Kubernetes API server endpoint | Floats via leader election |
| Gateway VIP | 172.16.20.50 | Ingress for all HTTP/HTTPS | Cilium L2 announcement |

**OS:** Talos Linux v1.13.2 | **k8s:** v1.36.1 | **Cluster name:** `homelab-k8s`

No workers. All three control planes run user workloads (`allowSchedulingOnControlPlanes: true`). Losing one node keeps etcd quorum (2 of 3). Rebuilt node rejoins automatically — no snapshot needed for single-node loss.

---

## Network Layout

**Subnet:** `172.16.20.0/24` | **Gateway:** `172.16.20.254` (UDM SE) | **Domain:** `blackcats.cc`
**Pod CIDR:** `10.244.0.0/16` | **Service CIDR:** `10.96.0.0/12`

**CNI:** Cilium — VXLAN encapsulation, WireGuard node-to-node encryption, full kube-proxy replacement.

**LoadBalancer pools (Cilium L2 announcements):**
- `pool-a` → `172.16.20.50` — `shared` Gateway only (auto-selected by `gateway.networking.k8s.io/gateway-name: shared` label)
- `pool-b` → `172.16.20.51` — direct LoadBalancer services (add label `lbpool: pool-b`)

**Netbird VPN** runs as a Talos extension on every node (`wt0` interface, `100.80.x.x/16`). Three guards in `talconfig.yaml` prevent Netbird IPs from polluting Kubernetes networking: etcd `advertisedSubnets`, kubelet `nodeIP.validSubnets`, per-node kube-apiserver `advertise-address`.

**Internet exposure:** Cloudflare DNS-01 for TLS only. All A records resolve to internal IPs. No port forwarding. Access requires Netbird VPN.

---

## Ingress Model

```
GatewayClass: cilium  (io.cilium/gateway-controller)
  └── Gateway: shared  (namespace: gateway, IP: 172.16.20.50)
        ├── Listener: http   port 80   *.blackcats.cc
        └── Listener: https  port 443  *.blackcats.cc  TLS: shared-tls (wildcard)
              ├── HTTPRoute: <app>  (per-app namespace)
              └── GRPCRoute: <app>  (Zitadel gRPC-Web)
```

**Gateway API CRDs:** from `kubernetes-sigs/gateway-api` v1.5.1 (experimental channel — includes GRPCRoute).

**TLS:** cert-manager `letsencrypt-production` ClusterIssuer → `*.blackcats.cc` wildcard cert in `gateway` namespace.

**DNS:** external-dns, Cloudflare provider, sources `gateway-httproute` + `gateway-grpcroute`. **Opt-in:** annotate route with `external-dns.alpha.kubernetes.io/enabled: "true"`.

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

**Gotify tokens:** plain k8s Secrets written by `gotify-bootstrap` Job (not SealedSecrets). `optional: true` on backup CronJob references.

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
| Gitea | gitea | `gitea.blackcats.cc` | Zitadel OIDC |
| FreshRSS | freshrss | `rss.blackcats.cc` | Zitadel OIDC |
| Homebox | homebox | `homebox.blackcats.cc` | Built-in |
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
| CNPG cluster `postgres` | postgres | — | Per-DB roles |

Full inventory with storage details: `docs/services.md`.

---

## Non-Obvious Decisions

**app-template naming:** Single controller named `app` → Deployment/Service is `{release-name}` (no suffix). Two+ controllers/services → `{release-name}-{name}`. HTTPRoute `backendRef.name` must match exactly. Always verify with `kubectl get svc -n <namespace>` before writing.

**Flux targetNamespace pitfall:** `spec.targetNamespace` overrides ALL namespace fields unconditionally. Cross-namespace resources must be in a separate Kustomization without targetNamespace.

**Talos kubelet mount namespace:** Kubelet runs in a private mount namespace. Pod `hostPath` writes are invisible to the kubelet unless `machine.kubelet.extraMounts` is configured (required for OpenEBS `openebs-hostpath`). This is already patched in `talconfig.yaml`.

**Static NFS PV nfsvers:** Talos kernel supports NFSv4 only for host-level static PV mounts. Always `nfsvers=4` in PV `mountOptions`. Democratic-csi dynamic PVCs are unaffected.

**Plex SQLite over NFS:** NFS causes SQLite WAL locking errors in Plex. Config PVC uses `openebs-hostpath` (local disk), not `nfs-client`.

**gotify-bootstrap Job immutability:** Job spec is immutable after creation. If the manifest changes while the completed Job is within 24h TTL, delete it and let Flux recreate: `kubectl delete job gotify-bootstrap -n monitoring`.

**Stakater Reloader image tag:** Chart v1.0.112 has a `vv1.0.112` double-v appVersion bug causing ImagePullBackOff. Override: `reloader.deployment.image.tag: "v1.0.112"` in HelmRelease values.

**Sonarr/Radarr use CNPG Postgres** (migrated from SQLite). The migration Jobs are in `kubernetes/apps/media/sonarr/app/migration-job.yml` and `radarr/app/migration-job.yml`.

**rclone Filen backend** requires rclone ≥ v1.69. Alpine's `apk add rclone` installs an older version. Always use the official binary from `downloads.rclone.org`.

**backup-tools image** (`ghcr.io/lucid-void/backup-tools`) contains bash, curl, kubectl, restic, rclone, postgresql17-client — but NOT jq or python3. Scripts needing JSON parsing use `alpine:3.21` + `apk add bash curl jq kubectl` at container startup.

**YAML block scalar + Python:** Python code at 0-indent inside a YAML `|` block scalar breaks the kustomize YAML parser. Put Python scripts as separate ConfigMap keys.

**Security namespace PSA:** `security` namespace requires `pod-security.kubernetes.io/enforce: privileged` for Falco (privileged container + hostPath volumes).

**Trivy Operator dbRepository:** Do not override with a full `ghcr.io/...` path — the chart prepends the registry, causing double-prefix. Leave at chart default.

**etcd snapshot CronJob** (`kube-system/etcd-snapshot`, daily 01:00): downloads `talosctl` at runtime (version pinned in script — update alongside Talos upgrades), tries CP nodes `.11 → .12 → .13` in order, uploads via restic to `rclone:filen:backups/restic/etcd-snapshot`. Three SealedSecrets required: `restic-secret` (RESTIC_PASSWORD + RESTIC_REPOSITORY), `rclone-secret` (rclone.conf with Filen creds), `talosconfig-secret` (talosconfig file from `~/.talos/config`). Gotify `optional: true` — kube-system not yet in gotify-bootstrap token list.
