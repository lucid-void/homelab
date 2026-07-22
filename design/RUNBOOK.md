# Runbook

Operational procedures for the `homelab-k8s` Kubernetes cluster.

## Prerequisites (one-time, workstation)

```bash
brew install siderolabs/tap/talosctl talhelper
brew install kubectl kubeseal fluxcd/tap/flux helm helmfile
brew install sops age
```

---

## Bootstrap — New Cluster from Zero

Phases are ordered — each depends on the previous.

### Phase 1 — Talos cluster secrets (once only)

```bash
cd kubernetes/talos/
talhelper gensecret > talsecret.sops.yaml
sops -e -i talsecret.sops.yaml
git add talsecret.sops.yaml && git commit -m "feat(talos): cluster secrets"
```

If lost, the cluster must be rebuilt from scratch.

### Phase 2 — Review talconfig.yaml

`kubernetes/talos/talconfig.yaml` defines nodes, Talos/k8s versions, cluster settings, and Talos extensions. Verify `talosVersion` and `kubernetesVersion` before proceeding.

### Phase 3 — Generate node configs

```bash
cd kubernetes/talos/
talhelper genconfig
# outputs to clusterconfig/ (gitignored — always regenerated from talsecret.sops.yaml)
```

After generation, get the schematic ID for Packer:
```bash
grep "metal-installer" clusterconfig/homelab-k8s-cp-1.yaml
# → factory.talos.dev/metal-installer/<schematic-id>:v1.x.y
```
Update `talos_schematic_id` in `infra/packer/Talos/talos-base.pkr.hcl` to match.

### Phase 4 — Build Talos Packer template

```bash
just build-talos-template
# downloads Talos ISO to Proxmox local storage, creates template VM 9001
```

Run once per Talos version/schematic change.

### Phase 5 — Provision VMs (Tofu)

Add static DHCP leases on UDM SE for these MACs before running:

| Node | MAC | IP |
|---|---|---|
| cp-1 | `BC:24:11:01:20:00` | 172.16.20.11 |
| cp-2 | `BC:24:11:01:21:00` | 172.16.20.12 |
| cp-3 | `BC:24:11:01:22:00` | 172.16.20.13 |

```bash
just plan && just apply
```

VMs boot into maintenance mode. Talos API reachable on port 50000 (unauthenticated).

### Phase 6 — Apply Talos machine configs

```bash
talhelper apply   # applies to all nodes defined in talconfig.yaml
```

First apply requires `--insecure` (no PKI yet — talhelper adds this automatically on first run). Each node reboots with its config; static IP becomes permanent after reboot.

### Phase 7 — Bootstrap etcd (once, one node only)

```bash
talosctl bootstrap \
  --nodes 172.16.20.11 \
  --endpoints 172.16.20.11 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

**Run exactly once.** Re-running corrupts etcd. The other two CPs join automatically.

```bash
talosctl health \
  --nodes 172.16.20.11,172.16.20.12,172.16.20.13 \
  --endpoints 172.16.20.11 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

### Phase 8 — kubeconfig

```bash
talosctl kubeconfig \
  --nodes 172.16.20.11 \
  --endpoints 172.16.20.10 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
# merges into ~/.kube/config; API server endpoint is the VIP

kubectl get nodes   # STATUS: NotReady — expected (no CNI yet)
```

### Phase 9 — Bootstrap Cilium + Sealed Secrets

**Restoring an existing cluster:** Restore the Sealed Secrets key **before** running helmfile so the controller can decrypt existing SealedSecrets in git:

```bash
sops -d /mnt/backups/keys/sealed-secrets-key.sops.yaml | kubectl apply -f -
```

**New cluster (fresh install):** skip the above.

```bash
helmfile --file kubernetes/bootstrap/helmfile.yml apply --skip-diff-on-install --suppress-diff
```

This installs in order: prometheus-operator-crds → Cilium → Spegel → Sealed Secrets.

Wait for nodes Ready:
```bash
until kubectl wait --for=condition=Ready nodes --all --timeout=600s; do sleep 10; done
kubectl get nodes
```

### Phase 10 — Back up the Sealed Secrets key

**Do this immediately after install.** Without this, the cluster is unrecoverable if rebuilt.

```bash
kubectl get secret -n kube-system \
  -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o yaml > /tmp/ss-key.yaml

sops -e /tmp/ss-key.yaml > /mnt/backups/keys/sealed-secrets-key.sops.yaml
shred -u /tmp/ss-key.yaml
```

Cache the public cert:
```bash
kubeseal --fetch-cert \
  --controller-name=sealed-secrets-controller \
  --controller-namespace=kube-system \
  > kubernetes/flux/pub-cert.pem

git add kubernetes/flux/pub-cert.pem
git commit -m "feat(k8s): sealed-secrets public cert"
git push
```

### Phase 11 — Install Flux

```bash
kubectl create namespace flux-system
kubectl apply -f kubernetes/bootstrap/flux/github-deploy-key.sealed.yml
kubectl apply -k kubernetes/bootstrap/flux
kubectl apply -f kubernetes/flux/vars/cluster-secrets.sealed.yml
kubectl apply -f kubernetes/flux/vars/cluster-settings.yml
kubectl apply -k kubernetes/flux/config
```

Watch Flux come up:
```bash
flux get kustomizations --watch
```

### Phase 12 — Infrastructure reconciles

FluxCD applies resources in `dependsOn` order. All operators, StorageClasses, and apps deploy automatically.

Wait for Sealed Secrets controller (needed before apps can start):
```bash
kubectl wait deployment sealed-secrets-controller \
  -n kube-system --for=condition=Available --timeout=3m
```

Watch full reconciliation:
```bash
flux get kustomizations --watch
kubectl get pods -A --watch
```

---

## Ongoing Operations

### Patch Talos machine config

```bash
# edit talconfig.yaml as needed
talhelper genconfig

# apply to all nodes (reboots only if required)
talhelper apply

# or target a single node
talosctl apply-config \
  --nodes 172.16.20.12 \
  --file kubernetes/talos/clusterconfig/homelab-k8s-cp-2.yaml \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --mode=auto
```

### Upgrade Talos

Roll one node at a time. etcd quorum is maintained throughout.

```bash
# 1. Update talosVersion in talconfig.yaml
talhelper genconfig

# 2. Get installer image URL
grep "metal-installer" kubernetes/talos/clusterconfig/homelab-k8s-cp-1.yaml
# use "installer" not "metal-installer" in the upgrade command

# 3. Upgrade one node at a time
talosctl upgrade \
  --nodes 172.16.20.11 \
  --image factory.talos.dev/installer/<schematic-id>:<version> \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --drain=false   # --drain=false required: CNPG PodDisruptionBudget blocks eviction

# Wait for node to rejoin, then repeat for .12 and .13

# 4. Apply config to deliver ExtensionServiceConfig documents
talosctl apply-config \
  --nodes 172.16.20.11 \
  --file kubernetes/talos/clusterconfig/homelab-k8s-cp-1.yaml \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

**Before any upgrade:** run kubent to check for deprecated APIs:
```bash
kubectl create job --from=cronjob/kubent kubent-precheck -n security
# findings are echoed by the notify container (the scan runs as an initContainer)
kubectl logs -n security -l job-name=kubent-precheck -c notify -f
```

### Upgrade Kubernetes

Run after all nodes are on the new Talos version:

```bash
talosctl upgrade-k8s \
  --nodes 172.16.20.11 \
  --to <new-k8s-version> \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

Update `kubernetesVersion` in `talconfig.yaml` after upgrading.

### Add a New Secret

```bash
# Write plaintext, seal, delete plaintext, commit
kubeseal --cert kubernetes/flux/pub-cert.pem \
  --format yaml < /tmp/new-secret.yaml \
  > kubernetes/apps/<namespace>/<app>/app/<name>-sealed.yml

shred -u /tmp/new-secret.yaml
git add kubernetes/apps/<namespace>/<app>/app/<name>-sealed.yml
git push
```

### Add a New Application

See `docs/gitops.md` — Adding a New Application section.

### Force Flux Reconciliation

```bash
flux reconcile kustomization cluster --with-source
flux reconcile kustomization <app-name>
```

### Re-run gotify-bootstrap (after Gotify DB reset)

The `gotify-bootstrap` Job spec is immutable while the completed Job is within its 24h TTL:

```bash
kubectl delete job gotify-bootstrap -n monitoring
flux reconcile kustomization gotify-bootstrap   # Flux recreates it
```

### Configure Proxmox SSO via Zitadel OIDC

The `zitadel-bootstrap` Job registers a `Proxmox VE` OIDC app and writes the
credentials to the `proxmox-oidc-secret` Secret in the `auth` namespace (Proxmox is
bare metal, outside the cluster, so nothing in-cluster consumes it). Read them out:

```bash
kubectl get secret proxmox-oidc-secret -n auth \
  -o jsonpath='{.data.OIDC_CLIENT_ID}'     | base64 -d; echo
kubectl get secret proxmox-oidc-secret -n auth \
  -o jsonpath='{.data.OIDC_CLIENT_SECRET}' | base64 -d; echo
```

On the Proxmox host, create the OpenID Connect realm (or use Datacenter → Realms → Add):

```bash
pveum realm add zitadel --type openid \
  --issuer-url https://zitadel.blackcats.cc \
  --client-id <OIDC_CLIENT_ID> \
  --client-key <OIDC_CLIENT_SECRET> \
  --username-claim email \
  --autocreate 1 \
  --default 0
```

Then add a Proxmox ACL/user mapping for the autocreated `<user>@zitadel` accounts.

**Redirect URI:** Proxmox sends the web UI base URL (no path) as the OIDC redirect.
The Zitadel app registers both `https://pve.blackcats.cc:8006` and
`https://pve.blackcats.cc`, so login works on the default port or on 443.

**Serving Proxmox on 443 (optional):** Proxmox's `pveproxy` only listens on 8006 and
the port is not configurable through supported means. To reach it at `https://pve.blackcats.cc`
add a host-level redirect on the Proxmox node (persist via `/etc/network/interfaces`
`post-up` or an nftables ruleset):

```bash
nft add rule ip nat prerouting tcp dport 443 redirect to :8006
```

Do **not** front Proxmox behind the cluster Gateway (172.16.20.50) — that creates a
circular dependency, since the Gateway runs on the VMs this host hypervises.

### Defragment etcd (`etcdDatabaseHighFragmentationRatio`)

etcd is copy-on-write with MVCC: every write creates a new revision, and
auto-compaction reclaims old revisions *logically* but never shrinks the on-disk
file — freed pages stay allocated to etcd as internal free space. Over time the DB
file grows to ~2× its live data. All three members replicate the same writes via
Raft, so they fragment in lockstep (the alert names one node, but all three are
affected). This is **cosmetic** until the file approaches etcd's ~2 GiB quota
(default; not overridden in `talconfig.yaml`) — at which point etcd goes read-only
until defragged. Only `defrag` returns free pages to the OS.

The `etcdDatabaseHighFragmentationRatio` alert is tuned to fire only when it
matters: the upstream rule guards on in-use bytes > 100 MiB (which flaps at our
scale), so it's disabled in the vm-stack HelmRelease and replaced by the
`etcd-custom` VMRule (`kubernetes/apps/monitoring/vm-stack/app/vmrules.yml`), which
fires only when the **allocated file** exceeds **1.5 GiB (75% of the 2 GiB quota)**
*and* is still < 50% in use. If you ever raise `quota-backend-bytes`, bump that
guard to match.

```bash
export TALOSCONFIG=kubernetes/talos/clusterconfig/talosconfig

# 1. Check IN USE % and find the LEADER
mise exec -- talosctl -n 172.16.20.11,172.16.20.12,172.16.20.13 etcd status

# 2. Confirm there is no NOSPACE alarm (empty output = clean)
mise exec -- talosctl -n 172.16.20.11,172.16.20.12,172.16.20.13 etcd alarm list

# 3. Defrag ONE node at a time — followers first, LEADER LAST (keeps quorum 2/3).
#    Each defrag briefly blocks that member's reads/writes (sub-second at our size).
mise exec -- talosctl -n <follower-1> etcd defrag
mise exec -- talosctl -n <follower-2> etcd defrag
mise exec -- talosctl -n <leader>     etcd defrag

# 4. If a NOSPACE alarm was set (DB hit the quota), clear it AFTER defragging:
mise exec -- talosctl -n 172.16.20.11,172.16.20.12,172.16.20.13 etcd alarm disarm
```

Each member should drop back to ~100% in-use after its defrag. This is an
operational action — nothing to commit.

---

## Planned Maintenance Shutdown

Full power-down for hardware work (cabling, NAS fans, Proxmox host maintenance).
etcd lives on each node's **local Talos disk** (the Proxmox VM disk), not the NAS — so a
clean Talos shutdown is all that's needed to protect cluster state. etcd data is only ever
at risk if a node's `EPHEMERAL`/`STATE` partition is wiped. **Do not `talosctl reset` or
`tofu destroy` these nodes — only shut them down.**

### Shut down

Bring the **cluster down before the NAS**. CNPG Postgres data is on `nfs-client` (the
Synology `kubernetes.nfs` share), so this order lets Postgres checkpoint cleanly while NFS
is still mounted.

```bash
# Halts all three CPs concurrently: stops pods (SIGTERM + grace),
# unmounts volumes, flushes + stops etcd, powers off the VM.
mise exec -- talosctl shutdown \
  --nodes 172.16.20.11,172.16.20.12,172.16.20.13 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

A *shutdown* does **not** call etcd `MemberRemove` — the 3-member list is preserved on
disk, so the nodes simply re-form quorum on next boot. No bootstrap or `force-new-cluster`
needed afterward.

- **No cordon/drain** — pointless for a full shutdown, and the CNPG PodDisruptionBudget
  blocks eviction anyway.
- If `clusterconfig/` isn't present, run `mise exec -- talhelper genconfig` in
  `kubernetes/talos/` first.

Then power off the Synology and do the hardware work.

### Start back up

Startup is automatic — the bootstrap phases above are only for a from-zero rebuild.

The VM resource (`infra/terraform/kubernetes.tf`) does not set `on_boot`, and the
`bpg/proxmox` provider defaults it to `true`. So:

| What you powered off | Restart |
|---|---|
| VMs only (Proxmox host stayed up) | Stay off — start manually: `qm start 2020 2021 2022`, or Proxmox UI |
| The whole Proxmox host | VMs autostart with the host → Talos boots → etcd quorum recovers → kubelet → Flux reconciles |

**Bring the NAS up before the cluster.** Proxmox can't sequence its VM startup against the
external Synology, so if the cluster boots first, CNPG and NFS-backed apps crashloop until
NFS is serving, then self-heal (etcd/Talos itself is on local disk and unaffected). Either
power the NAS on first, or add a blind `startup { up_delay = N }` block in `kubernetes.tf`
to delay the VMs after host boot.

Verify after boot:

```bash
mise exec -- talosctl health \
  --nodes 172.16.20.11,172.16.20.12,172.16.20.13 \
  --endpoints 172.16.20.11 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
mise exec -- kubectl get nodes
mise exec -- flux get kustomizations
```

---

## Recovery Procedures

### Recreate one control plane (cluster has quorum)

With 3 CPs, losing one keeps etcd quorum. Cluster continues running throughout.

```bash
# 1. Reprovision via Tofu
just apply

# 2. Apply Talos config to rebuilt node (--insecure: no PKI on fresh VM)
talosctl apply-config \
  --nodes 172.16.20.12 --insecure \
  --file kubernetes/talos/clusterconfig/homelab-k8s-cp-2.yaml

# 3. Node contacts existing etcd and rejoins automatically
kubectl get nodes --watch   # NotReady → Ready
```

### Recreate all control planes (full cluster wipe)

Only if all three CPs are lost simultaneously.

```bash
# 1. Reprovision all three VMs
just apply

# 2. Apply configs to all nodes
talhelper apply

# 3. Re-bootstrap etcd (ONE node only)
talosctl bootstrap \
  --nodes 172.16.20.11 \
  --endpoints 172.16.20.11 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig

# 4. Get fresh kubeconfig
talosctl kubeconfig \
  --nodes 172.16.20.11 \
  --endpoints 172.16.20.10 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig

# 5. Restore Sealed Secrets key (before Flux reconciles SealedSecrets)
sops -d /mnt/backups/keys/sealed-secrets-key.sops.yaml | kubectl apply -f -

# 6. Bootstrap Cilium + Sealed Secrets (Phase 9)
helmfile --file kubernetes/bootstrap/helmfile.yml apply --skip-diff-on-install --suppress-diff

# 7. Install Flux (Phase 11)
kubectl create namespace flux-system
kubectl apply -f kubernetes/bootstrap/flux/github-deploy-key.sealed.yml
kubectl apply -k kubernetes/bootstrap/flux
kubectl apply -f kubernetes/flux/vars/cluster-secrets.sealed.yml
kubectl apply -f kubernetes/flux/vars/cluster-settings.yml
kubectl apply -k kubernetes/flux/config

# Flux reconciles everything; controller uses restored Sealed Secrets key
```

### etcd force-new-cluster (quorum already broken)

If etcd has lost quorum, pick the node with the most recent data. Add a temporary patch in `talconfig.yaml` for that node:

```yaml
patches:
  - |-
    cluster:
      etcd:
        extraArgs:
          force-new-cluster: "true"
```

Apply, wait for it to be healthy, then wipe EPHEMERAL on the other nodes so they rejoin:

```bash
talosctl reset --system-labels-to-wipe EPHEMERAL \
  --nodes 172.16.20.12,172.16.20.13 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --reboot
```

After they rejoin, remove the `force-new-cluster` patch, regenerate, and apply.

---

## Troubleshooting

### Node not joining

```bash
talosctl dmesg --nodes <ip> --talosconfig kubernetes/talos/clusterconfig/talosconfig | tail -20
talosctl service ext-netbird --nodes <ip> --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

### Flux reconciliation failures

```bash
flux get all -A
flux logs --level=error
flux get kustomizations   # look for False/Unknown ready state
```

### SealedSecret not decrypting

```bash
kubectl describe sealedsecret <name> -n <namespace>
kubectl logs -n kube-system -l app.kubernetes.io/name=sealed-secrets
```

Common causes: wrong namespace in the template, cluster rebuilt without restoring the key.

### Cilium connectivity

```bash
cilium status
cilium connectivity test
kubectl get ciliumnodes -o wide   # all INTERNALIP must be 172.16.20.x (not 100.80.x.x)
kubectl get endpointslices -n default   # all endpoints must be 172.16.20.x
```

### CNPG pod stuck

```bash
kubectl get cluster -n postgres
kubectl describe cluster postgres -n postgres
kubectl logs -n postgres -l cnpg.io/cluster=postgres -c postgres
```

### Stale VolumeAttachment blocking pod scheduling

```bash
kubectl get volumeattachments
kubectl delete volumeattachment <name>
```

Occurs after failed Job pods that mounted PVCs.

### PVC stuck in Terminating

```bash
kubectl patch pvc <name> -n <namespace> -p '{"metadata":{"finalizers":null}}'
```

### Gotify-bootstrap Job immutable field error

Job spec is immutable while completed Job is within 24h TTL window:
```bash
kubectl delete job gotify-bootstrap -n monitoring
flux reconcile kustomization gotify-bootstrap
```
