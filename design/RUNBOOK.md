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
grep "metal-installer" clusterconfig/homelab-k8s-k8s-cp-1.yaml
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
| k8s-cp-1 | `BC:24:11:00:20:00` | 172.16.20.20 |
| k8s-cp-2 | `BC:24:11:00:21:00` | 172.16.20.21 |
| k8s-cp-3 | `BC:24:11:00:22:00` | 172.16.20.22 |

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
  --nodes 172.16.20.20 \
  --endpoints 172.16.20.20 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

**Run exactly once.** Re-running corrupts etcd. The other two CPs join automatically.

```bash
talosctl health \
  --nodes 172.16.20.20,172.16.20.21,172.16.20.22 \
  --endpoints 172.16.20.20 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

### Phase 8 — kubeconfig

```bash
talosctl kubeconfig \
  --nodes 172.16.20.20 \
  --endpoints 172.16.20.19 \
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
  --nodes 172.16.20.21 \
  --file kubernetes/talos/clusterconfig/homelab-k8s-k8s-cp-2.yaml \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --mode=auto
```

### Upgrade Talos

Roll one node at a time. etcd quorum is maintained throughout.

```bash
# 1. Update talosVersion in talconfig.yaml
talhelper genconfig

# 2. Get installer image URL
grep "metal-installer" kubernetes/talos/clusterconfig/homelab-k8s-k8s-cp-1.yaml
# use "installer" not "metal-installer" in the upgrade command

# 3. Upgrade one node at a time
talosctl upgrade \
  --nodes 172.16.20.20 \
  --image factory.talos.dev/installer/<schematic-id>:<version> \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --drain=false   # --drain=false required: CNPG PodDisruptionBudget blocks eviction

# Wait for node to rejoin, then repeat for .21 and .22

# 4. Apply config to deliver ExtensionServiceConfig documents
talosctl apply-config \
  --nodes 172.16.20.20 \
  --file kubernetes/talos/clusterconfig/homelab-k8s-k8s-cp-1.yaml \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig
```

**Before any upgrade:** run kubent to check for deprecated APIs:
```bash
kubectl create job --from=cronjob/kubent kubent-precheck -n security
kubectl logs -n security -l job-name=kubent-precheck -f
```

### Upgrade Kubernetes

Run after all nodes are on the new Talos version:

```bash
talosctl upgrade-k8s \
  --nodes 172.16.20.20 \
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

---

## Recovery Procedures

### Recreate one control plane (cluster has quorum)

With 3 CPs, losing one keeps etcd quorum. Cluster continues running throughout.

```bash
# 1. Reprovision via Tofu
just apply

# 2. Apply Talos config to rebuilt node (--insecure: no PKI on fresh VM)
talosctl apply-config \
  --nodes 172.16.20.21 --insecure \
  --file kubernetes/talos/clusterconfig/homelab-k8s-k8s-cp-2.yaml

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
  --nodes 172.16.20.20 \
  --endpoints 172.16.20.20 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig

# 4. Get fresh kubeconfig
talosctl kubeconfig \
  --nodes 172.16.20.20 \
  --endpoints 172.16.20.19 \
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
  --nodes 172.16.20.21,172.16.20.22 \
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
