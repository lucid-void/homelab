# Networking

## IP Plan

| Host | IP | Role |
|---|---|---|
| Synology RS1219+ | 172.16.20.2 | NFS storage (Btrfs /volume2) |
| Proxmox MS-A2 | 172.16.20.3 | Hypervisor |
| DGX Spark | 172.16.20.4 | GPU workstation (WOL-managed) |
| `.5–.9` | reserved | Future physical devices |
| k8s-cp-1 | 172.16.20.20 | Talos control plane + workloads |
| k8s-cp-2 | 172.16.20.21 | Talos control plane + workloads |
| k8s-cp-3 | 172.16.20.22 | Talos control plane + workloads |
| k8s API VIP | 172.16.20.19 | API server endpoint (floats via leader election) |
| Gateway VIP | 172.16.20.50 | Cilium L2 announcement — `shared` Gateway |
| Pool-B VIP | 172.16.20.51 | Cilium L2 announcement — direct LoadBalancer services |
| UDM SE | 172.16.20.254 | Gateway, DHCP, DNS resolver, ad blocking |

**Pod CIDR:** `10.244.0.0/16`
**Service CIDR:** `10.96.0.0/12`
**Cluster domain:** `blackcats.cc`

The k8s VMs (`.20`–`.22`) are in the same `/24` as the rest of the homelab. No separate VLAN. The existing Docker Swarm VMs (`.10`–`.17`) are untouched.

---

## Cilium

Cilium is the CNI, kube-proxy replacement, and Gateway API controller.

| Setting | Value |
|---|---|
| Routing mode | VXLAN encapsulation |
| Encryption | WireGuard node-to-node (`encryption.type: wireguard`) |
| kube-proxy replacement | Full (`kubeProxyReplacement: true`, `k8sServiceHost: 172.16.20.19`) |
| Hubble | Enabled — relay + UI |
| L2 Announcements | Enabled (`l2announcements.enabled: true`) |
| Gateway API | Enabled (`gatewayAPI.enabled: true`, `enableAppProtocol: true`) |
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
| `pool-b` | 172.16.20.51 | `lbpool: pool-b` |

The L2 announcement policy applies to all Linux nodes on interfaces matching `^ens[0-9]+`. ARP responses are handled by whichever node is elected; if that node goes down, another takes over.

pool-a is exclusively for the `shared` Gateway. pool-b is for any other `LoadBalancer` Service that needs a stable external IP — add `lbpool: pool-b` to its labels.

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

- **GitRepository** `gateway-api` → `https://github.com/kubernetes-sigs/gateway-api` tag `v1.5.1`
- **Kustomization** `gateway-api` → path `./config/crd/experimental`
- The `cilium` Kustomization depends on `gateway-api` — CRDs exist before the Cilium HelmRelease applies

Manifests: `kubernetes/flux/repositories/git/gateway-api.yml`, `kubernetes/apps/kube-system/gateway-api/`

### Hierarchy

```
GatewayClass: cilium  (kube-system, controller: io.cilium/gateway-controller)
  └── Gateway: shared  (namespace: gateway, IP: 172.16.20.50)
        ├── Listener: http   port 80   *.blackcats.cc   → HTTPRoute (redirect to https)
        └── Listener: https  port 443  *.blackcats.cc   → TLS terminated by cert shared-tls
              ├── HTTPRoute: <app>  (per-app namespace)
              └── GRPCRoute: <app>  (per-app namespace, e.g. Zitadel gRPC-Web)
```

The Gateway accepts routes from all namespaces (`allowedRoutes.namespaces.from: All`).

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

external-dns watches `HTTPRoute` and `GRPCRoute` resources and creates Cloudflare A records pointing to `172.16.20.50`. DNS creation is **opt-in**:

```yaml
annotations:
  external-dns.alpha.kubernetes.io/enabled: "true"
```

Without this annotation, no DNS record is created. The Gateway's own wildcard hostname is intentionally not annotated — it would create a `*.blackcats.cc` wildcard A record.

`txtOwnerId: homelab-k8s` — external-dns uses TXT records to track ownership. Records not present in git will be deleted (`policy: sync`).

All A records resolve to `172.16.20.50` (gateway VIP). No Cloudflare proxy. External access requires Netbird VPN.

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
