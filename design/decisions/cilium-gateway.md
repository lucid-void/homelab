# Cilium and Gateway API

**Read before editing:** `kubernetes/apps/kube-system/cilium/`, `kubernetes/apps/gateway/`, `kubernetes/talos/talconfig.yaml`

## Current state

Cilium is both the CNI and the Gateway API controller. Ingress is **Cilium Gateway API
only**: `HTTPRoute`/`GRPCRoute` attach to the `shared` Gateway in the `gateway`
namespace. No `Ingress` objects, no Traefik. The Gateway also carries one **TCP:22
listener** (`ssh`) for git-over-SSH; raw TCP otherwise goes to pool-b LoadBalancers, not
the Gateway.

The Gateway API CRD bundle is **pinned to 1.6.x** (experimental channel), with Renovate
constrained to `<1.7.0`. Cilium 1.20 documents support for Gateway API **v1.6.1**, so
the bundle matches the controller rather than running ahead of it. The bundle ships an
enforcing VAP that blocks rollbacks below **v1.6.0**.

Two Cilium Helm values in `kubernetes/apps/kube-system/cilium/app/helm-values.yml` are
load-bearing and must stay explicit:

- `gatewayAPI.enableAlpn: true`
- `MTU: 9000`

Jumbo frames run end to end: node `ens18` is pinned to `mtu: 9000` in `talconfig.yaml`,
and the Proxmox tap is already 9000 via `infra/terraform/kubernetes.tf`, propagated into
the guest by virtio `host_mtu`. All three control planes are VMs on the same Proxmox
host, so inter-node pod/VXLAN traffic rides the bridge rather than the physical switch —
confirm the bridge supports 9000 before changing anything here.

## Rules

- **Never set `gatewayAPI.enableAlpn` to false or drop it** — without it the Envoy HTTPS
  listener negotiates **no** ALPN protocol. curl and browsers silently fall back to
  HTTP/1.1, but strict clients fail the TLS handshake outright with
  `server did not agree on a protocol`. That breaks any gRPC route (gRPC needs h2)
  and external OIDC clients such as Proxmox `proxmox-openid` ("Failed to contact
  token endpoint: Request failed").
- **Never remove the explicit `MTU: 9000`** — Cilium's MTU auto-detection otherwise
  picks the Netbird `wt0` interface (MTU 1280) and throttles ALL pod traffic to
  ~1200–1280 byte frames (`cilium_vxlan` and pod `lxc*` at 1280, `cilium_wg0` at 1200).
  The talconfig Netbird IP guards do not cover MTU detection.
- **1.6.1 is the floor for TCPRoute — do not roll the bundle below it** — Cilium 1.20
  added TCPRoute/UDPRoute, but `HasTCPRouteSupport` checks the
  **`gateway.networking.k8s.io/v1`** GVK, and TCPRoute only reaches `v1` in bundle 1.6;
  1.5.1 serves `v1alpha2` only.
- **Do not treat `GatewayClass.status.supportedFeatures` as evidence that TCPRoute
  works** — Cilium lists `TCPRoute` there either way, because the check runs against its
  compiled-in scheme rather than the installed CRD. Test the actual CRD instead.
- **Re-check Cilium's own `gateway-api` dependency before bumping the bundle** —
  `curl -s .../cilium/<ver>/go.mod | grep gateway-api`. Running ahead of the controller
  is the failure this pin exists to prevent.
- **Restart the Cilium operator after bumping the bundle** —
  `kubectl -n kube-system rollout restart deploy/cilium-operator`. Cilium discovers
  optional Gateway API CRDs *once at startup* with a version-exact check, so a running
  operator ignores newly installed CRDs; the symptom is an empty route `status` with
  every Gateway listener `Programmed` and the port refusing connections.
- **Remember the CRD Kustomization is `prune: true`** — a bundle version that drops a
  CRD deletes it from the cluster. 1.6.1 only *adds* (`xbackends`), but a future bump
  may not.

## Verify

```bash
# ALPN actually negotiated
curl -v --http2 https://<host>/ 2>&1 | grep -i alpn

# TCPRoute really served at v1 (not just claimed in supportedFeatures)
mise exec -- kubectl get tcproutes.v1.gateway.networking.k8s.io -A

# MTU end to end on a node
mise exec -- talosctl get links -o yaml | grep -iE 'id:|mtu:'
```
