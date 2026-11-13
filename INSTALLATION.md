# Installation — homelab-k8s

The bootstrap procedure now lives in
[design/runbook.md](design/runbook.md) → **"Bootstrap — New Cluster from Zero"**.
Recovery procedures and the troubleshooting reference are in the same file.
For the stack overview, see [README.md](README.md).

Nodes, IPs and the API VIP are defined in
[kubernetes/talos/talconfig.yaml](kubernetes/talos/talconfig.yaml): `cp-1/2/3` at
`172.16.20.11`–`.13`, the worker `llm-1` at `172.16.20.14`, API VIP `172.16.20.10`.

---

## Pending a move into the runbook

The sections below are the only content of this file that `design/runbook.md` does not
cover yet. They stay here until they are moved; do not add anything else to this file.

### Seal Flux bootstrap secrets (fresh cluster only)

Runs between the runbook's Phase 10 (back up the Sealed Secrets key, cache
`pub-cert.pem`) and Phase 11 (install Flux), which applies the two sealed files produced
here. Skip it when restoring a cluster with the backed-up Sealed Secrets key — the
sealed files already in git still decrypt.

The `.secret.yml` / `cluster-secrets.yml` files are gitignored plaintext templates —
fill them in, seal, then delete the plaintext.

```bash
# Generate a deploy key for Flux and add the public half to GitHub
ssh-keygen -t ed25519 -C "flux@homelab-k8s" -f /tmp/flux-deploy-key -N ""
# Add /tmp/flux-deploy-key.pub as a deploy key on the GitHub repo

# Fill the private key into the template and seal
sed "s|<paste private key here>|$(cat /tmp/flux-deploy-key)|" \
  kubernetes/bootstrap/flux/github-deploy-key.secret.yml \
  | kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  > kubernetes/bootstrap/flux/github-deploy-key.sealed.yml

# Cluster-wide secrets
kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < kubernetes/flux/vars/cluster-secrets.yml \
  > kubernetes/flux/vars/cluster-secrets.sealed.yml

shred -u /tmp/flux-deploy-key
git add kubernetes/bootstrap/flux/github-deploy-key.sealed.yml \
        kubernetes/flux/vars/cluster-secrets.sealed.yml
git commit -m "feat(k8s): sealed bootstrap secrets"
git push
```

### Netbird (VPN) IP leakage into cluster networking

Netbird adds a `wt0` WireGuard interface with a `100.80.x.x/16` address to every
node. Several Kubernetes components auto-detect the "primary" node IP and will
pick `100.80.x.x` if not constrained. The `talconfig.yaml` has three guards to
prevent this:

| Component | Guard | Location in talconfig |
|---|---|---|
| etcd | `advertisedSubnets`/`listenSubnets: [172.16.20.0/24]` | `controlPlane.patches` |
| kubelet | `machine.kubelet.nodeIP.validSubnets: [172.16.20.0/24]` | top-level `patches` (all nodes) |
| kube-apiserver | `cluster.apiServer.extraArgs.advertise-address: <LAN IP>` | per-node `patches` on `cp-1/2/3` (`172.16.20.11`–`.13`) |

**Symptom — kube-apiserver endpoints use Netbird IPs:**
containerd uses the Netbird IP as `status.podIP` for hostNetwork pods (it reads the
host's IP list and picks `100.80.x.x`). The kube-apiserver static pod has
`--advertise-address=$(POD_IP)`, so it registers the Netbird IP in the `kubernetes`
service endpoints. Pods that land on a Cilium node that can't route Netbird IPs get
DNS/API timeouts.

Check: `kubectl get endpointslices -n default` — endpoints must all be LAN IPs.
Fix: per-node `advertise-address` overrides the `$(POD_IP)` default. Apply config
and restart kube-apiserver pods.

**Symptom — CiliumNode has Netbird IP as InternalIP:**
If Cilium registers a node with its Netbird IP, cross-node VXLAN tunnels use the wrong
destination. Pods on other nodes can't reach endpoints on that node.

Check: `kubectl get ciliumnodes -o wide` — all `INTERNALIP` entries must be LAN IPs.
Fix: `kubectl delete ciliumnode <node>` — the Cilium agent recreates it with the
Kubernetes node's InternalIP (which kubelet now reports correctly via
`nodeIP.validSubnets`).

**Symptom — etcd peers use Netbird IPs:**
etcd cluster loses quorum if peers can't reach each other. One node survives on LAN;
others fail.
Fix: `etcd.advertisedSubnets`/`listenSubnets` in `talconfig.yaml` — apply and
reboot the affected nodes.
Recovery if quorum is already broken: see
[design/runbook.md](design/runbook.md) → "etcd force-new-cluster (quorum already broken)".
