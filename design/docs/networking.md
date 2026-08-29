# Networking

## IP Plan

| Host | IP | Role |
|---|---|---|
| Synology RS1219+ | 172.16.20.2 | NFS storage (Btrfs /volume2) |
| Proxmox host | 172.16.20.3 | Hypervisor (Intel Core Ultra 5 235HX / Arrow Lake-HX) |
| DGX Spark | 172.16.20.4 | GPU workstation (WOL-managed) |
| `.5–.9` | reserved | Future physical devices |
| cp-1 | 172.16.20.11 | Talos control plane + workloads |
| cp-2 | 172.16.20.12 | Talos control plane + workloads |
| cp-3 | 172.16.20.13 | Talos control plane + workloads |
| llm-1 | 172.16.20.14 | Talos worker — LLM inference only, tainted `workload=llm:NoSchedule` |
| k8s API VIP | 172.16.20.10 | API server endpoint (floats via leader election) |
| Gateway VIP | 172.16.20.50 | Cilium L2 announcement — `shared` Gateway |
| Pool-B VIPs | 172.16.20.51–.52 | Cilium L2 announcement — direct LoadBalancer services (.51 Plex, .52 Velocity proxy) |
| UDM SE | 172.16.20.254 | Gateway, DHCP, DNS resolver, ad blocking |

**Pod CIDR:** `10.244.0.0/16`
**Service CIDR:** `10.96.0.0/12`
**Cluster domain:** `blackcats.cc`

The k8s control planes (`.11`–`.13`), the `llm-1` worker (`.14`) and the API VIP (`.10`) are in the same `/24` as the rest of the homelab — no separate VLAN. (Swarm is retired; the cluster is the primary platform.)

---

## Cilium

Cilium is the CNI, kube-proxy replacement, and Gateway API controller.

| Setting | Value |
|---|---|
| Routing mode | VXLAN encapsulation |
| Encryption | WireGuard node-to-node (`encryption.type: wireguard`) |
| MTU | Jumbo frames — `MTU: 9000` set **explicitly** in Cilium values. The node NIC `ens18` is pinned to `mtu: 9000` in `talconfig.yaml` (and the Proxmox tap is 9000 in `infra/terraform/kubernetes.tf`). **Must be explicit:** Cilium's MTU auto-detection otherwise latches onto the Netbird `wt0` interface (1280) and throttles all pod traffic to ~1200–1280 byte frames. Cilium subtracts VXLAN + WireGuard overhead for pod/tunnel interfaces. Requires bridge/switch jumbo support end-to-end. |
| kube-proxy replacement | Full (`kubeProxyReplacement: true`, `k8sServiceHost: localhost`, `k8sServicePort: 7445` — KubePrism, VIP-independent) |
| Hubble | Enabled — relay + UI |
| L2 Announcements | Enabled (`l2announcements.enabled: true`) |
| Gateway API | Enabled (`gatewayAPI.enabled: true`, `enableAppProtocol: true`, `enableAlpn: true`) |
| IPAM mode | Kubernetes |
| Operator replicas | 1 |

Cilium requires specific capabilities on Talos (cgroupv2 managed by OS, not runtime):

```yaml
cgroup:
  autoMount:
    enabled: false
  hostRoot: /sys/fs/cgroup
securityContext:
  capabilities:
    ciliumAgent: [CHOWN, KILL, NET_ADMIN, NET_RAW, IPC_LOCK, SYS_ADMIN, SYS_RESOURCE, DAC_OVERRIDE, FOWNER, SETGID, SETUID]
    cleanCiliumState: [NET_ADMIN, SYS_ADMIN, SYS_RESOURCE]
```

Manifests: `kubernetes/apps/kube-system/cilium/`

### LoadBalancer IP Pools

Two `CiliumLoadBalancerIPPool` resources + one `CiliumL2AnnouncementPolicy` (manifests: `kubernetes/apps/kube-system/cilium/config/cilium-l2.yml`):

| Pool | IP | Selector |
|---|---|---|
| `pool-a` | 172.16.20.50 | `gateway.networking.k8s.io/gateway-name: shared` |
| `pool-b` | 172.16.20.51–172.16.20.52 | `lbpool: pool-b` |

The L2 announcement policy applies to all Linux nodes on interfaces matching `^ens[0-9]+`. ARP responses are handled by whichever node is elected; if that node goes down, another takes over.

pool-a is exclusively for the `shared` Gateway. pool-b is for any other `LoadBalancer` Service that needs a stable external IP — add `lbpool: pool-b` to its labels.

**One address per Service in pool-b.** Each consumer pins itself with `lbipam.cilium.io/ips` so an IP can't migrate between Services on a reconcile:

| Service | IP | Port | Why pinned |
|---|---|---|---|
| `media/plex` (`plex-direct`) | 172.16.20.51 | 32400 | Plex `ADVERTISE_IP` hardcodes it |
| `media/minecraft-proxy` (Velocity) | 172.16.20.52 | 25565 | matcha/vanilla A records via external-dns |

### Never share one pool-b IP between Services

These two Services previously shared `172.16.20.51` via `lbipam.cilium.io/sharing-key: pool-b-shared`. **Do not reintroduce that.** Cilium's L2 announcer creates one `Lease` per *Service* (`cilium-l2announce-<ns>-<svc>`) and elects each leader independently, so a shared address gets announced by two different nodes simultaneously:

```
cilium-l2announce-media-mc-router     → cp-3    (the historical example; that
                                                 Service has since been replaced
                                                 by minecraft-proxy on its own IP)
cilium-l2announce-media-plex-direct   → cp-2
```

Both nodes then answer ARP for the same IP. Because `externalTrafficPolicy` is `Cluster`, whichever node receives a flow SNATs it locally, so the connection's conntrack state exists on that node alone. When a client's ARP entry flips to the other node mid-connection, its packets are re-SNATed there under a fresh source port, arrive at the backend pod as an unknown 4-tuple, and the pod's kernel replies `RST`.

Symptoms, which took a long time to attribute: long-lived TCP dies a few seconds in while short HTTP bursts complete fine (they finish before a flip); clients on the same L2 segment as the nodes are unaffected because their ARP entry stays pinned, while clients routed in from another VLAN break constantly because the router re-resolves on its own schedule. The `RST` carries the *pod's* TTL, not the gateway's, which rules out middlebox injection and misleadingly points at the backend. Diagnosed 2026-08-01 against Minecraft; see the comments in `cilium-l2.yml`.

Widening the pool further is the correct way to add a third direct-LB Service. Cross-namespace sharing would need `lbipam.cilium.io/sharing-cross-namespace`; not used.

### Netbird IP Isolation

Netbird runs as a Talos extension (`siderolabs/netbird`) and adds a `wt0` WireGuard interface with a `100.80.x.x/16` address to every node. Several Kubernetes components auto-select the "primary" IP and will pick `100.80.x.x` without explicit constraints. Three guards in `talconfig.yaml` prevent this:

| Component | Guard | Consequence if removed |
|---|---|---|
| etcd | `cluster.etcd.advertisedSubnets/listenSubnets: [172.16.20.0/24]` | etcd peers advertise Netbird IPs; quorum breaks across nodes |
| kubelet | `machine.kubelet.nodeIP.validSubnets: [172.16.20.0/24]` | Node `InternalIP` is `100.80.x.x`; Cilium VXLAN tunnels go to wrong IPs |
| kube-apiserver | Per-node `cluster.apiServer.extraArgs.advertise-address: <LAN IP>` | containerd picks `100.80.x.x` as `$(POD_IP)` for the static pod; API endpoint registers Netbird IP in the `kubernetes` Service endpoints |

All three must be present. Symptom check:

```bash
kubectl get endpointslices -n default   # all endpoints must be 172.16.20.x
kubectl get ciliumnodes -o wide          # all INTERNALIP must be 172.16.20.x
```

Recovery for stale CiliumNode:
```bash
kubectl delete ciliumnode <name>   # agent recreates from correct kubelet-reported InternalIP
```

---

## Gateway API

### CRD Installation

Gateway API CRDs are installed from the upstream `kubernetes-sigs/gateway-api` git source (experimental channel, which includes `GRPCRoute`):

- **GitRepository** `gateway-api` → `https://github.com/kubernetes-sigs/gateway-api` tag `v1.6.1`
- **Kustomization** `gateway-api` → path `./config/crd/experimental`
- The `cilium` Kustomization depends on `gateway-api` — CRDs exist before the Cilium HelmRelease applies

Manifests: `kubernetes/flux/repositories/git/gateway-api.yml`, `kubernetes/apps/kube-system/gateway-api/`

### Version constraint — pinned to 1.6.x

**Do not bump the Gateway API bundle past 1.6.x without checking Cilium first.** The CRDs are only as useful as the controller reading them, and Cilium is that controller:

| | Gateway API |
|---|---|
| Cilium 1.20.0 (running) documents | v1.6.1 |
| Installed here | **v1.6.1** — matched to what Cilium targets |

The bundle used to sit at v1.5.1 and Renovate was constrained to `<1.6.0`, on the reasoning that v1.6.1 (#113) would put us two minors ahead of Cilium 1.19.6 (which documented v1.4.1) for zero gain. **Both halves of that went stale** and the pin moved to `<1.7.0` on 2026-08-12:

- Cilium 1.20 tracks Gateway API v1.6.1, so v1.5.1 was *behind* the controller, not ahead of it.
- The gain is no longer zero: Cilium 1.20 added `TCPRoute`/`UDPRoute` support, and **its controller watches the `gateway.networking.k8s.io/v1` GVK** (`HasTCPRouteSupport` checks the v1 kind against the compiled-in scheme). `TCPRoute` was promoted to `v1` in Gateway API **1.6.1**; the 1.5.1 bundle serves `v1alpha2` only, so on 1.5.1 a `TCPRoute` is unusable — Cilium advertises TCPRoute in `GatewayClass.status.supportedFeatures` regardless (the check is against its own scheme, not the installed CRD), so that field is **not** evidence the feature works. `kubectl get tcproutes.v1.gateway.networking.k8s.io` is the real test. This is what git-over-SSH on the shared Gateway needs.

To check before revisiting:

```bash
# what does the Cilium release actually claim?
curl -s https://raw.githubusercontent.com/cilium/cilium/v<ver>/go.mod | grep sigs.k8s.io/gateway-api

# does the installed CRD actually serve the version Cilium watches?
kubectl get crd tcproutes.gateway.networking.k8s.io \
  -o jsonpath='{range .spec.versions[*]}{.name}{" served="}{.served}{"\n"}{end}'
```

Two mechanical notes for the next bump:

- The `gateway-api` Kustomization is `prune: true`, so a bundle that *removes* a CRD would delete it. The 1.5.1 → 1.6.1 move only added one (`xbackends`), but this must be re-checked per bump.
- The bundle installs an enforcing `ValidatingAdmissionPolicy` (`safe-upgrades.gateway.networking.k8s.io`) that rejects installing bundles older than the current one and experimental CRDs on top of standard-channel ones. It does **not** block forward upgrades — but having moved to 1.6.1, rolling back below it now requires deleting the policy first.

### Hierarchy

```
GatewayClass: cilium  (kube-system, controller: io.cilium/gateway-controller)
  └── Gateway: shared  (namespace: gateway, IP: 172.16.20.50)
        ├── Listener: http   port 80   *.blackcats.cc   → HTTPRoute (redirect to https)
        ├── Listener: https  port 443  *.blackcats.cc   → TLS terminated by cert shared-tls
        │     ├── HTTPRoute: <app>  (per-app namespace)
        │     └── GRPCRoute: <app>  (per-app namespace, e.g. Zitadel gRPC-Web)
        └── Listener: ssh    port 22   (no hostname)    → TCPRoute
              └── TCPRoute: gitea-ssh  (namespace gitea) → gitea-ssh:22 → pod :2222
```

The Gateway accepts routes from all namespaces (`allowedRoutes.namespaces.from: All`).

**Why SSH is on the Gateway and not a pool-b LoadBalancer.** Gitea advertises its clone
URL as `git@gitea.blackcats.cc:...`, and that hostname is an A record for the Gateway VIP,
so port 22 must answer on `172.16.20.50`. A hostname resolves to one address, so it cannot
send HTTPS to `.50` and SSH to a pool-b IP; and putting a second `LoadBalancer` Service on
`.50` via `lbipam.cilium.io/sharing-key` is the dual-announcement/RST failure described in
`kubernetes/apps/kube-system/cilium/config/cilium-l2.yml`. A TCP listener on the Gateway is
the only option that keeps the advertised URL literal. Raw-TCP services that *can* take
their own hostname (Plex direct, Velocity) still use pool-b — see the IP plan above.

Notes on the TCP listener:

- `hostname` is invalid on a `TCP` listener (nothing to match on before the payload), so
  the listener is port-only and a `TCPRoute` selects it by `sectionName: ssh`.
- Requires Gateway API ≥ 1.6 — see the version constraint section above.
- **Cilium serves TCPRoute without Envoy.** Unlike HTTPRoute/GRPCRoute, there is no Envoy
  listener or `tcp_proxy` filter chain: the operator generates an `EndpointSlice` owned by
  the Gateway and attached to the Gateway's *own* Service (`cilium-gateway-shared`),
  annotated `gateway.cilium.io/backend-service` + `gateway.cilium.io/backend-port`, whose
  endpoints are the backend pods on their target port. `.50:22` therefore reaches the Gitea
  pod's `:2222` through the ordinary service datapath.

  This matters when debugging: **a working TCPRoute leaves no trace in the
  `CiliumEnvoyConfig`.** Grepping the CEC for the backend or a `tcp_proxy` filter finds
  nothing whether the route works or not. The real evidence is the generated slice:

  ```bash
  kubectl get endpointslice -n gateway     # expect cilium-gateway-shared-<hash>-ipv4
  kubectl get tcproute gitea-ssh -n gitea -o jsonpath='{.status}'   # Accepted + ResolvedRefs
  ```

- The Gateway Service is `externalTrafficPolicy: Cluster`
  (`gateway-api-service-externaltrafficpolicy`), so the client IP is SNATed to a node
  address — Gitea logs `10.244.x.x`, not the real client. Auth is by SSH key, so this costs
  attribution only. `enable-gateway-api-proxy-protocol` cannot recover it: that setting is
  an Envoy listener option, and this path never reaches Envoy.
- Cilium's `gateway-api-hostnetwork-enabled` must stay `false`. In host-network mode there
  is no generated LoadBalancer Service for the EndpointSlice to attach to, so
  `TCPRoute`/`UDPRoute` fall back to a random node port instead of the configured listener
  port.
- Gatus probes `tcp://gitea.blackcats.cc:22` separately from the HTTPS check — the two ride
  different listeners, so a healthy web UI proves nothing about clone-over-SSH.

#### Gotcha: adding a Gateway API CRD version requires restarting cilium-operator

Cilium discovers optional Gateway API CRDs **once, at operator startup**
(`operator/pkg/gateway-api/cell.go` → `checkCRDs`), and the check is version-exact:

```go
for _, v := range crd.Spec.Versions {
    if v.Name == gvk.Version { found = true; break }
}
```

Kinds that fail it are left out of `InstalledOptionalKinds`, never registered into the
client scheme, and their reconcilers never start. Bumping the bundle to 1.6.1 makes
`TCPRoute` **v1** available, but a `cilium-operator` that was already running does not
re-run discovery — so the first TCPRoute after the bump sits with an **empty `status`**
forever while the Gateway itself reports every listener `Programmed`, and connections to
the listener port are refused (the LoadBalancer exposes the port, nothing backs it).

Hit on 2026-08-12 wiring up Gitea SSH. Fix:

```bash
kubectl -n kube-system rollout restart deploy/cilium-operator
# confirm: "TCPRoute CRD is installed, TCPRoute support is enabled"
kubectl -n kube-system logs deploy/cilium-operator | grep -i 'TCPRoute CRD'
```

Two misleading signals to ignore while diagnosing this:

- `GatewayClass.status.supportedFeatures` lists `TCPRoute` **regardless** — it did so for
  the whole time the v1 CRD was absent and the reconciler was not running. It is not
  evidence the feature works.
- The absence of a `tcp_proxy` filter in the CEC is normal (see above), so it does not
  distinguish "not reconciled" from "working".

The real test is `kubectl get tcproutes.v1.gateway.networking.k8s.io` for the CRD, and a
populated `TCPRoute.status.parents` for the reconciler.

**ALPN:** Cilium is configured with `gatewayAPI.enableAlpn: true` (in `kubernetes/apps/kube-system/cilium/app/helm-values.yml`). Without it the Envoy HTTPS listener negotiates *no* ALPN protocol — tolerant clients (browsers, curl) silently fall back to HTTP/1.1, but strict clients fail the TLS handshake. This previously broke the Zitadel bootstrap (gRPC requires h2) and external OIDC clients such as Proxmox's `proxmox-openid` (token-endpoint call failed with "Failed to contact token endpoint: Request failed"). With ALPN enabled the listener advertises `h2` + `http/1.1`.

Manifests: `kubernetes/apps/gateway/`

### TLS (cert-manager)

cert-manager issues a wildcard certificate for the Gateway's HTTPS listener:

- **ClusterIssuer:** `letsencrypt-production` (ACME DNS-01, Cloudflare)
- **Certificate:** `shared-tls` in namespace `gateway`
  - `dnsNames: ["*.blackcats.cc", "blackcats.cc"]`
  - Referenced by the Gateway's `tls.certificateRefs`

Manifests: `kubernetes/apps/cert-manager/`, `kubernetes/apps/gateway/shared-gateway/config/certificate.yml`

The Cloudflare API token is stored as `cloudflare-api-token` Secret in `cert-manager` namespace (SealedSecret).

### DNS (external-dns)

external-dns watches `HTTPRoute`, `GRPCRoute` and `Service` resources and creates Cloudflare A records. DNS creation is **opt-in**:

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
```

Without this annotation, no DNS record is created. The Gateway's own wildcard hostname is intentionally not annotated — it would create a `*.blackcats.cc` wildcard A record.

`txtOwnerId: homelab-k8s` — external-dns uses TXT records to track ownership. Records not present in git will be deleted (`policy: sync`).

Almost all A records resolve to `172.16.20.50` (gateway VIP). The `service` source exists for the one case that can't route through the Gateway — Minecraft is raw TCP, so the Velocity proxy's LoadBalancer publishes `matcha.blackcats.cc` and `vanilla.blackcats.cc` at `172.16.20.52` via:

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
  external-dns.alpha.kubernetes.io/hostname: matcha.blackcats.cc,vanilla.blackcats.cc
```

Because the annotation-filter applies to every source, adding `service` did not change any existing LoadBalancer — `plex-direct` is unannotated and stays unpublished.

No Cloudflare proxy. External access requires Netbird VPN.

Manifests: `kubernetes/apps/network/external-dns/`

### Adding a Route

Every HTTPRoute:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: myapp
  namespace: myapp
  annotations:
    external-dns.alpha.kubernetes.io/enabled: "true"
spec:
  parentRefs:
    - name: shared
      namespace: gateway
  hostnames:
    - myapp.blackcats.cc
  rules:
    - backendRefs:
        - name: myapp          # follows app-template naming — see gitops.md
          port: 8080
```

HTTP→HTTPS redirect is handled by the `httproute-redirect` resource on the `http` listener (`kubernetes/apps/gateway/shared-gateway/config/httproute-redirect.yml`) — individual app HTTPRoutes only need to target the `https` listener.

---

## Internet Exposure

- Cloudflare DNS-01 is used **only** for valid TLS certs — all A records resolve to internal IPs
- No port forwarding on UDM SE
- No Cloudflare proxy
- External access: Netbird VPN (primary) or ZeroTier (gaming only)
