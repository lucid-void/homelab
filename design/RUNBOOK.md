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
| llm-1 | `BC:24:11:01:23:00` | 172.16.20.14 |

```bash
just plan && just apply
```

VMs boot into maintenance mode. Talos API reachable on port 50000 (unauthenticated).

### Phase 6 — Apply Talos machine configs

```bash
talosctl apply-config \
  --nodes 172.16.20.102 --endpoints 172.16.20.102 --insecure \
  --file kubernetes/talos/clusterconfig/homelab-k8s-llm-1.yaml
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

### Grow the node disks

Needed when `EPHEMERAL` fills — usually from container images or `openebs-hostpath`
PVCs, which have no quota (see `docs/storage.md`). Done once already: 60 GB → 100 GB.

**The trap:** growing the Proxmox virtual disk is *not enough*. Talos expands the
`EPHEMERAL` partition to fill the disk **only at boot**, and there is no online-grow
command. Until each node reboots, `talosctl get discoveredvolumes` shows a larger
`sda` but an unchanged `sda4`, and workloads see the old filesystem size.

1. **Check the hypervisor has room.** `local-lvm` is a thin pool; growing 3 nodes by
   40 GB costs 120 GB. Query it rather than guess:
   ```bash
   # via the in-cluster metrics (proxmox-monitoring scrapes the host)
   kubectl run q --rm -i --restart=Never -n monitoring --image=curlimages/curl -- \
     curl -s -G "http://vmsingle-vm-stack-victoria-metrics-k8s-stack.monitoring.svc.cluster.local:8428/api/v1/query" \
     --data-urlencode 'query=node_lvm_thinpool_data_percent'
   ```
   Keep the result comfortably under the 85% `LvmThinPoolDataHigh` alert.

2. **Bump the size in OpenTofu** — three `disk_gb` entries in
   `infra/terraform/kubernetes.tf` (one per node), then `tofu apply`. Proxmox grows
   the virtual disks online; nothing in the guest changes yet.

3. **Roll the nodes one at a time**, waiting for `Ready` + etcd 3/3 between each:
   ```bash
   export TALOSCONFIG=$PWD/kubernetes/talos/clusterconfig/talosconfig
   for n in 172.16.20.11 172.16.20.12 172.16.20.13; do
     talosctl -n $n reboot
     # wait for the node to come back Ready before continuing
     kubectl wait --for=condition=Ready node/<name> --timeout=10m
     talosctl -n 172.16.20.11 etcd members     # expect 3
   done
   ```

4. **Verify the partition actually grew** — this is the step that proves it worked:
   ```bash
   talosctl -n 172.16.20.12 get discoveredvolumes | grep sda4   # EPHEMERAL
   ```

**Note:** workloads on `openebs-hostpath` are node-pinned and cannot reschedule, so
they are down for the duration of their own node's reboot (~3–5 min). Check nobody is
mid-session first — e.g. `rcon-cli list` for the Minecraft servers.

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

### Upgrade Flux

Flux upgrades itself. Both pinned versions must be bumped **together**, or the
bootstrap path drifts from the running cluster:

| File | What it drives |
|---|---|
| `kubernetes/flux/config/flux.yml` | `OCIRepository` tag — the running cluster |
| `kubernetes/bootstrap/flux/kustomization.yml` | `?ref=` — only used when bootstrapping from zero |

The `flux` Kustomization then applies the new manifests over the live controllers.

**Before bumping across a minor version, check for removed APIs.** Flux v2.9.0
removed `image.toolkit.fluxcd.io/v1beta2` and `notification.toolkit.fluxcd.io/v1beta2`.
Having no *objects* on the dead version is not sufficient — the CRD also has to
have stopped *storing* it:

```bash
kubectl get crd -o json | jq -r '.items[]
  | select(.spec.group|test("toolkit.fluxcd.io"))
  | select(.status.storedVersions[]?=="v1beta2")
  | "\(.metadata.name) stored=\(.status.storedVersions|join(","))"'
```

Any CRD listed here blocks the upgrade, and the failure is a dry-run error on the
`flux` Kustomization rather than anything visibly broken:

```
CustomResourceDefinition/imagepolicies.image.toolkit.fluxcd.io dry-run failed (Invalid):
  status.storedVersions[0]: Invalid value: "v1beta2": missing from spec.versions;
  v1beta2 was previously a storage version, and must remain in spec.versions until a
  storage migration ensures no data remains persisted in v1beta2
```

Kubernetes refuses to drop a version that is still in `status.storedVersions`, even
with zero objects of that kind. Confirm there is genuinely no data, then clear the
stale marker:

```bash
# 1. must print "No resources found" for every affected kind
kubectl get imagepolicies,imagerepositories,imageupdateautomations -A

# 2. drop the dead version from the stored list
for c in imagepolicies imagerepositories imageupdateautomations; do
  kubectl patch crd $c.image.toolkit.fluxcd.io --subresource=status \
    --type=merge -p '{"status":{"storedVersions":["v1"]}}'
done

# 3. retry
flux reconcile kustomization flux
```

If objects *do* exist on the dead version, rewrite them first
(`kubectl get <kind> -A -o yaml | kubectl apply -f -`) so they are re-persisted at
the current storage version, then patch. This was needed once, going 2.8.8 → 2.9.2;
the `notification.toolkit.fluxcd.io` CRDs were already storing only `v1beta3`/`v1`
and needed nothing.

Verify:

```bash
flux version          # distribution should match the new tag
kubectl get pods -n flux-system
```

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

The Job logs one line per token showing which path it took:

```
  immich-backup: reused (app 4)     # app exists and the Secret still holds its token
  falco: rotated (app 8)            # app exists but the Secret was gone → new token
  gatus: created                    # app did not exist → new token
  admin client: reused (client 1)
```

After a Gotify **DB reset** every line reads `created` and every token changes.
Long-running consumers that hold a token in their environment need a restart —
`gotify-telegram` carries `reloader.stakater.com/auto`, but `falcosidekick` does
not, so restart it manually:

```bash
kubectl rollout restart deployment/falco-falcosidekick -n security
```

Backup CronJobs need nothing: they read the Secret at job start.

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

### Bootstrap Proton Mail Bridge (one-time, and after a vault loss)

Proton Mail is end-to-end encrypted, so Paperless can only read it through
Proton Mail Bridge, which logs in and re-serves the mailbox as local IMAP.
Login needs an account password and a 2FA code typed by a human, so unlike the
Zitadel/Gotify/Kavita bootstraps **there is no Job for this** — it is manual,
and it is only re-run if the `protonmail-bridge-config` volume is lost.

Requires a **paid** Proton plan; bridge refuses to log in on free accounts.

**1. Stop the running bridge.** Only one instance may hold the vault, and the
container's entrypoint starts one automatically. Do not try to log in inside the
live pod: `pkill bridge` there ends the entrypoint pipeline and the container
dies out from under your exec session. Flux would also undo a bare `kubectl
scale` at its next reconcile, so suspend it first.

```bash
flux suspend helmrelease protonmail-bridge -n paperless
kubectl scale deploy/protonmail-bridge -n paperless --replicas=0
kubectl wait --for=delete pod -l app.kubernetes.io/name=protonmail-bridge -n paperless --timeout=2m
```

**2. Start a throwaway pod on the same volume**, with the command overridden to
`sleep` so no bridge starts on its own and the vault stays free.

Create it **detached** — deliberately no `-it --rm`. With `--rm` the pod is
deleted the moment you leave the attached session, which would destroy the
`/tmp/cert.pem` that step 4 has to read back out.

```bash
kubectl run protonmail-bridge-init -n paperless --restart=Never \
  --image=ghcr.io/videocurio/proton-mail-bridge:v3.24.2 \
  --overrides='{"spec":{"containers":[{"name":"init","image":"ghcr.io/videocurio/proton-mail-bridge:v3.24.2","command":["sleep","infinity"],"volumeMounts":[{"name":"config","mountPath":"/root"}]}],"volumes":[{"name":"config","persistentVolumeClaim":{"claimName":"protonmail-bridge-config"}}]}}'

kubectl wait --for=condition=Ready pod/protonmail-bridge-init -n paperless --timeout=2m
kubectl exec -it protonmail-bridge-init -n paperless -- /bin/bash
```

**3. Log in and export the certificate.** Inside that shell:

```bash
# Bridge seals its vault key with gpg/pass, so the keyring must exist first.
# This is exactly what the image entrypoint does on a first run — done directly
# so that no bridge, socat or faketty is started and the vault stays free.
[ -d /root/.password-store ] || {
  gpg --generate-key --batch /app/GPGparams.txt && pass init ProtonMailBridge
}

/usr/bin/bridge --cli
```

Do **not** run `/app/entrypoint.sh` here. It starts a bridge of its own, and
racing two instances against one vault is the thing this whole procedure exists
to avoid.

At the `>>>` prompt:

```
login                       # username, password, 2FA — then wait for the sync
info                        # copy the generated IMAP password (NOT your Proton password)
cert export                 # answer /tmp  (see note below)
exit
```

`info` prints `Address: 127.0.0.1, IMAP port: 1143, Security: STARTTLS` and a
**generated** password unique to bridge. That password is what Paperless uses.
Ignore the address/port it prints — see step 5 for what to actually enter.

`cert export` prompts for a **directory that already exists**, not a filename —
it validates with `os.Stat()` + `IsDir()`, so `/root/cert.pem` is rejected and
it silently re-prompts. It then writes **two** files into that directory:
`cert.pem` *and* `key.pem`, the private key, both mode 0600.

Answer **`/tmp`**, not `/root`. `/root` is the PVC mount, so exporting there
leaves the private key sitting in plaintext on the volume for the life of the
deployment, right beside the vault whose whole job is to keep it encrypted.
`/tmp` lives in the throwaway pod and is destroyed with it. Only `cert.pem` is
needed downstream; `key.pem` never leaves the pod.

**4. Seal and commit the certificate.** Bridge keeps its TLS certificate inside
the encrypted vault, so this export is the only way to get it.

```bash
kubectl exec protonmail-bridge-init -n paperless -- cat /tmp/cert.pem \
  > /tmp/bridge-cert.pem

D=kubernetes/apps/paperless/protonmail-bridge/app

kubectl create secret generic protonmail-bridge-cert -n paperless \
  --from-file=cert.pem=/tmp/bridge-cert.pem \
  --dry-run=client -o yaml > $D/app-secret.yml

kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < $D/app-secret.yml > $D/app-sealed.yml
```

**Commit `app-sealed.yml`, never `app-secret.yml`.** `**/*secret.yml` is
gitignored (`kubernetes/.gitignore`), which is deliberate — it is what stops a
plaintext template ever reaching the remote. The trap is that it fails
*silently* in this direction too: name the sealed output `app-secret.yml` and
`git add` accepts it without complaint, the file never leaves the workstation,
Flux never sees a Secret, and the only symptom is mail quietly failing to fetch.
Verify with `git status --short $D` before committing.

A certificate is public and needs no sealing on its own merits. It is sealed
anyway so that everything an app is handed follows one convention and nobody has
to work out which credential-shaped file is the exception.

`PAPERLESS_EMAIL_CERTIFICATE_LOCATION` is already set in
`kubernetes/apps/paperless/paperless/app/helmrelease.yml` and stays set — it is
**coupled to this SealedSecret**. Paperless registers a Django system check that
raises an **Error**, `Email cert <path> is not a file`, when the variable is set
but the file is absent, and an Error-level check aborts startup with
`SystemCheckError`: a crash loop, not a deferred mail failure. The pod never goes
Ready, the Helm upgrade times out, and Flux rolls the release back. `optional:
true` on the mount governs only the *kubelet*; it does nothing about the check.
So if you ever remove the SealedSecret, remove that variable in the same commit.

**Do not try to verify this before pushing.** Running

```bash
kubectl exec deploy/paperless-app -n paperless -c app -- \
  env PAPERLESS_EMAIL_CERTIFICATE_LOCATION=/etc/ssl/protonmail/cert.pem \
  python manage.py check
```

against the *currently running* pod fails with `Email cert … is not a file`, and
that is correct rather than a fault: the Secret does not exist in the cluster
until the commit is pushed, so the optional mount is an empty directory. The
check is a post-push verification — see the end of this procedure. It is only
meaningful as a pre-commit gate in the reverse direction, when *removing* the
SealedSecret while leaving the variable behind.

Then clean up and bring the bridge back:

```bash
kubectl delete pod protonmail-bridge-init -n paperless
flux resume helmrelease protonmail-bridge -n paperless
kubectl scale deploy/protonmail-bridge -n paperless --replicas=1
```

Commit and push. Paperless mounts the Secret `optional: true` and reads the file
per mail fetch, so it needs no restart once the mount populates.

**5. Add the mail account in the Paperless UI** (Settings → Mail, runtime app
state, not GitOps):

| Field | Value |
|---|---|
| IMAP server | `127.0.0.1` |
| IMAP port | `1143` |
| IMAP security | **STARTTLS** |
| Username | your Proton address |
| Password | the generated password from `info` |

`127.0.0.1` is not a typo and not a shortcut. Bridge's certificate carries a
single SAN, `IP:127.0.0.1`, and Paperless verifies hostnames unconditionally, so
the Service name would fail the handshake even with the certificate trusted. The
`bridge` socat sidecar in the Paperless pod listens on `127.0.0.1:1143` and
forwards to the bridge Service. See `design/decisions/protonmail-bridge.md`.

**Verify**, after the push has landed and a new Paperless pod is Running. The
volume source changes `configMap` -> `secret` in that commit, so the rollout
creates a fresh pod with the certificate mounted from the start:

```bash
kubectl get secret protonmail-bridge-cert -n paperless      # must exist first
kubectl exec deploy/paperless-app -n paperless -c app -- \
  ls -l /etc/ssl/protonmail/cert.pem                        # must be a file
kubectl exec deploy/paperless-app -n paperless -c app -- python manage.py check
```

`System check identified no issues` means the variable and the Secret agree.
Then end to end:

```bash
kubectl exec deploy/paperless-app -n paperless -c app -- \
  python -c "import ssl,imaplib; c=ssl.create_default_context(cafile='/etc/ssl/protonmail/cert.pem'); \
m=imaplib.IMAP4('127.0.0.1',1143); m.starttls(c); print(m.noop())"
```

`('OK', [b'...'])` means trust, hostname and reachability are all correct. A
`CERTIFICATE_VERIFY_FAILED` means the SealedSecret is missing or stale; an
`IP address mismatch` means something is dialling a name instead of `127.0.0.1`.

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

### Rebuild a CNPG replica that cannot rejoin (pg_rewind deadlock)

Symptom: one instance never goes Ready, the `Cluster` sits on
`Instance Status Extraction Error: HTTP communication issue`, and the replica logs

```
pg_rewind: error: could not find common ancestor of the source and target cluster's timelines
```

Its data directory is unrecoverable — repeated crash/promote cycles advanced the
primary's timeline past any shared ancestor. **This deadlocks image rollouts:** CNPG
will not roll pods while it cannot read every instance's status, so a bad-image fix
committed to git applies to `status.image` and then goes nowhere. Check with
`kubectl get cluster postgres -n postgres -o jsonpath='{.status.image}'` — if that
already shows the good tag but the pods do not, this is why.

Confirm which copy is authoritative first (higher timeline + higher NextXID wins, and it
should be `currentPrimary`):

```bash
mise exec -- kubectl get cluster postgres -n postgres \
  -o jsonpath='{.status.instancesReportedState}{"\n"}'
```

Then rebuild the replica. **Order matters, and both steps are required:**

```bash
# 1. Move PGDATA aside from INSIDE the pod. Renaming, not deleting, keeps a fallback.
#    Safe because the replica's postmaster is already down (no socket in /controller/run).
mise exec -- kubectl exec -n postgres postgres-2 -c postgres -- \
  mv /var/lib/postgresql/data/pgdata /var/lib/postgresql/data/pgdata.broken

# 2. Delete BOTH the pod and the PVC. The PVC is what matters: CNPG only runs
#    pg_basebackup from a `<cluster>-<n>-join` Job, and it only creates that Job when
#    the instance's PVC is ABSENT. Emptying PGDATA alone does NOT trigger it — the
#    instance manager just dies on `stat .../pgdata: no such file or directory`.
mise exec -- kubectl delete pod postgres-2 -n postgres --wait=false
mise exec -- kubectl delete pvc postgres-2 -n postgres --wait=false

# 3. Watch the join Job, then the primary roll
mise exec -- kubectl get jobs -n postgres -w    # postgres-2-join → Complete
mise exec -- kubectl get cluster postgres -n postgres -w
```

Step 1 is load-bearing and is the non-obvious part. `nfs-client` derives its share path
from `<namespace>-<pvcname>` (`idTemplate` in the democratic-csi values) and the class is
`Retain`, so the **recreated PVC re-adopts the exact same directory** — verified: the
renamed `pgdata.broken` reappeared alongside the fresh `pgdata` in the new PVC. Skip the
rename and `pg_basebackup` finds a populated PGDATA and you are back where you started.
Same trap as the Gitea valkey rebuild; see `docs/storage.md`.

The first join attempt may `Error` if the primary crashes mid-basebackup — the Job
retries on its own. At ~500 MB the basebackup takes seconds, so it completes even
between crashes of a sick primary. Once the replica is Ready the cluster reaches
`Cluster in healthy state` and CNPG immediately performs the deferred primary update
(`Primary instance is being restarted without a switchover`).

Verify before cleaning up, then reclaim the space:

```bash
mise exec -- kubectl exec -n postgres postgres-1 -c postgres -- psql -U postgres -xtAc \
  "select application_name, state, replay_lag, sent_lsn, replay_lsn from pg_stat_replication;"
# want: state=streaming, sent_lsn == replay_lsn, sub-second replay_lag

mise exec -- kubectl exec -n postgres postgres-2 -c postgres -- \
  rm -rf /var/lib/postgresql/data/pgdata.broken
```

The old PV is left `Released` (reclaim policy `Retain`) and holds no unique data once
the directory is re-adopted; deleting that PV object is optional housekeeping.

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

This rebuilds the cluster **from git**, which is the primary recovery path — every
manifest is in this repo, so Flux reconstructs the workloads. It does *not* restore
etcd itself. Use the snapshot path below only when you need in-cluster state that
git does not hold.

### Restore etcd from a snapshot

The `kube-system/etcd-snapshot` CronJob uploads a daily `talosctl etcd snapshot` to
`rclone:filen:backups/restic/etcd-snapshot` (30-day retention). Restoring it replaces
cluster state wholesale, so prefer the rebuild-from-git path above unless you
specifically need resources that were never in git.

```bash
# 1. Pull the snapshot out of restic (run anywhere with the restic + rclone creds;
#    RESTIC_PASSWORD / RESTIC_REPOSITORY match kube-system/restic-secret)
export RESTIC_REPOSITORY=rclone:filen:backups/restic/etcd-snapshot
restic snapshots                       # pick the one you want
restic restore <snapshot-id> --target /tmp/etcd-restore
# → /tmp/etcd-restore/tmp/etcd.snap

# 2. Reprovision the VMs and apply Talos configs, but DO NOT run talosctl bootstrap
just apply
talhelper apply

# 3. Bootstrap the single node directly from the snapshot instead
talosctl bootstrap \
  --nodes 172.16.20.11 \
  --endpoints 172.16.20.11 \
  --talosconfig kubernetes/talos/clusterconfig/talosconfig \
  --recover-from /tmp/etcd-restore/tmp/etcd.snap

# 4. Continue from step 4 of the full-wipe procedure (kubeconfig onwards)
```

`--recover-from` replaces the plain `talosctl bootstrap` in step 3 of the full wipe —
running both re-initialises etcd and discards the snapshot.

> **Untested.** No etcd snapshot has been restore-tested end-to-end (see TODO.md →
> "Backup restore actually works"). Treat the steps above as the intended procedure,
> not a verified one.

### Restore an application backup from a given date

Every nightly run writes an independent point-in-time restic snapshot, so any retained
day can be restored on its own. Retention per repo is **30 dailies, then one per month
for 12 months** — so "3 days ago" and "5 days ago" are distinct restore points, but
beyond 30 days you land on whatever day of that month the monthly happened to keep.

Restore reads the repo directly and touches nothing in the cluster, so it is safe to
run against a live system as long as you restore to a scratch `--target`.

```bash
# Run inside a throwaway pod that already has the repo credentials mounted.
# Swap the namespace/secret names for the app you want (repo = one per job).
mise exec -- kubectl run restic-restore -n paperless --rm -it --restart=Never \
  --image=ghcr.io/lucid-void/backup-tools:v1.0.2 \
  --overrides='{"spec":{"securityContext":{"runAsUser":65534,"fsGroup":65534},
    "containers":[{"name":"r","image":"ghcr.io/lucid-void/backup-tools:v1.0.2",
    "stdin":true,"tty":true,"command":["/bin/bash"],
    "envFrom":[{"secretRef":{"name":"restic-secret"}}],
    "env":[{"name":"RCLONE_CONFIG","value":"/rclone-config/rclone.conf"},
           {"name":"HOME","value":"/tmp"}],
    "volumeMounts":[{"name":"rc","mountPath":"/rclone-config","readOnly":true}]}],
    "volumes":[{"name":"rc","secret":{"secretName":"rclone-secret","defaultMode":288}}]}}'

# then, inside the pod:
restic snapshots                          # list every restore point with its date
restic restore <snapshot-id> --target /tmp/r     # whole snapshot
restic restore <snapshot-id> --target /tmp/r --include /db/paperless.pgdump  # just the DB
```

`restic restore latest --time "2026-07-15 04:00:00"` picks the newest snapshot at or
before a timestamp, which is easier than copying an ID. `restic dump <id> <path>` streams
a single file to stdout — handy for piping a `.pgdump` straight into `pg_restore`.

> **`HOME=/tmp` is required.** The image runs as uid 65534 with a non-writable `/`, and
> restic aborts its cache setup with `unable to open cache: mkdir /.cache: permission
> denied` otherwise. Note also that `backup-tools` has **no `python3` and no `jq`**, and
> its `find` is busybox (no `-printf`, no `-readable`).

> **Untested.** No application snapshot has been restore-tested end-to-end either (see
> TODO.md → "Backup restore actually works").

### Rebuild the paperless search index

`paperless-backup` deliberately excludes `/data/index` — it is the derived full-text
search index, not source data. After restoring a paperless snapshot, rebuild it:

```bash
mise exec -- kubectl exec -n paperless deploy/paperless-app -c app -- \
  document_index reindex
```

Until that runs, documents are present and readable but full-text search returns
nothing. The classification model (`/data/classification_model.pickle`) *is* in the
snapshot, but also regenerates on its own schedule, so a stale one is harmless.

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
