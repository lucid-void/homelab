# Watch-State Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Converge watch history across Plex, Jellyfin and Trakt, and turn the Trakt watchlist into a self-draining Seerr request queue.

**Architecture:** One CrossWatch deployment in the `media` namespace runs two sync pairs — Plex ↔ Jellyfin and Plex ↔ Trakt — forming a chain with Plex as the transit point and no cycle. A separate `trakt-seerr-queue` CronJob reads the Trakt watchlist, requests each item in Seerr, and deletes it from the watchlist on acceptance. All manifests are delivered by Flux from `main`; nothing is applied by hand.

**Tech Stack:** FluxCD, `bjw-s/app-template` 3.7.3, Sealed Secrets, Gateway API `HTTPRoute`, Keycloak `KeycloakOIDCClient`, `nfs-client` storage class, Python 3.13 stdlib.

**Spec:** `docs/superpowers/specs/2026-09-21-watch-sync-design.md`

## Global Constraints

These apply to every task. They are repo hard rules; violating one wedges Flux or silently no-ops.

- **Never write a raw `Secret`.** Seal it: `mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < /tmp/secret.yaml > <name>-sealed.yml`
- **Never use `Ingress`.** Use `HTTPRoute` with `parentRefs: [{name: shared, namespace: gateway, sectionName: https}]`
- **Never `kubectl apply` a config change.** Everything goes through git and Flux. `kubectl` is for diagnostics only.
- **Never set `spec.targetNamespace`** on a Kustomization whose resources span namespaces. The CrossWatch ks spans only `media`, so `targetNamespace: media` is correct there; the Keycloak client lives in its own existing kustomization.
- **Never use a rolling image tag.** Pin `ghcr.io/cenodude/crosswatch:v0.12.3` and `python:3.13-alpine`.
- **Never trust `optional: true` on an app-template `envFrom`** — chart 3.7.3 strips it silently.
- **Never put Python at 0-indent inside a YAML `|` block scalar** — it breaks the kustomize parser. Indent the script body four spaces, matching `kromgo-badge-push-script`.
- **All k8s tooling is via mise:** `mise exec -- kubectl|flux|kubeseal|helm|kubeconform`
- **Validate before pushing:** `.agents/scripts/validate-manifests.sh <path>` on every new or changed manifest. A schema violation reaching `main` wedges the whole Flux Kustomization, not just that file.
- **CrossWatch pairs must never sync watchlists.** A watchlist pair re-adds whatever `trakt-seerr-queue` removed, looping forever and filling Seerr with duplicate requests.
- **Never auto-merge a CrossWatch Renovate bump.** v0.12.3 is pre-1.0 with eight releases in the preceding month.

---

### Task 0: Branch

**Files:** none

- [ ] **Step 1: Create the working branch**

The repo's default branch is `main` and Flux deploys from it. Do not commit this work directly to `main`.

```bash
cd /home/void/repos/Homelab
git checkout -b feat/watch-sync
```

- [ ] **Step 2: Confirm the branch is clean of unrelated staged work**

```bash
git status --short
```

Expected: the five pre-existing modified files (`design/decisions/gotify.md`, `design/decisions/jellyfin-ui.md`, `design/decisions/jellyfin.md`, `design/docs/storage.md`, `kubernetes/apps/monitoring/gotify-bootstrap/app/bootstrap.sh`) may appear as modified. Leave them alone — do not stage them in any commit in this plan.

---

### Task 1: CrossWatch PVC and deployment (no route yet)

Deploys CrossWatch with no ingress. The UI is reachable only by port-forward at this stage, which is deliberate: it holds no credentials yet and OIDC is not configured until Task 3.

**Files:**
- Create: `kubernetes/apps/media/crosswatch/ks.yml`
- Create: `kubernetes/apps/media/crosswatch/app/kustomization.yml`
- Create: `kubernetes/apps/media/crosswatch/app/pvc.yml`
- Create: `kubernetes/apps/media/crosswatch/app/helmrelease.yml`
- Create: `kubernetes/apps/media/crosswatch/app/config-key-sealed.yml`
- Modify: `kubernetes/apps/media/kustomization.yml`

**Interfaces:**
- Produces: a `Deployment` named `crosswatch` and a `Service` `crosswatch:8787` in namespace `media`; a PVC `crosswatch-config` mounted at `/config`. Task 3 adds an `HTTPRoute` pointing at that service. Task 4's CronJob does not touch either.

- [ ] **Step 1: Generate and seal the config encryption key**

`CW_CONFIG_KEY` encrypts credentials inside `/config/config.json`. Generate it once; losing it means re-entering every provider login.

```bash
CW_KEY=$(head -c 32 /dev/urandom | base64)
echo "SAVE THIS IN YOUR PASSWORD MANAGER: ${CW_KEY}"
cat > /tmp/crosswatch-config-key.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: crosswatch-config-key
  namespace: media
type: Opaque
stringData:
  CW_CONFIG_KEY: ${CW_KEY}
EOF
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < /tmp/crosswatch-config-key.yaml \
  > kubernetes/apps/media/crosswatch/app/config-key-sealed.yml
rm /tmp/crosswatch-config-key.yaml
```

- [ ] **Step 2: Write the PVC**

Create `kubernetes/apps/media/crosswatch/app/pvc.yml`:

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: crosswatch-config
  namespace: media
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: nfs-client
  resources:
    requests:
      storage: 2Gi
```

- [ ] **Step 3: Write the HelmRelease**

Create `kubernetes/apps/media/crosswatch/app/helmrelease.yml`:

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: crosswatch
  namespace: media
spec:
  interval: 30m
  chart:
    spec:
      chart: app-template
      version: "3.7.3"
      sourceRef:
        kind: HelmRepository
        name: bjw-s
        namespace: flux-system
  install:
    remediation:
      retries: 3
  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3
  values:
    controllers:
      app:
        replicas: 1
        # config.json is a single SQLite-adjacent JSON document on an RWO NFS
        # volume; two replicas would interleave writes and lose pair definitions.
        strategy: Recreate
        pod:
          securityContext:
            runAsUser: 2202
            runAsGroup: 2200
            fsGroup: 2200
            runAsNonRoot: true
        containers:
          app:
            image:
              repository: ghcr.io/cenodude/crosswatch
              # v0.12.3 released 2026-09-16. Pre-1.0 with eight releases in the
              # preceding month — pin exactly, never auto-merge a bump.
              tag: "v0.12.3"
            env:
              TZ: Europe/Brussels
            envFrom:
              # No `optional: true` — chart 3.7.3 strips it silently, so a missing
              # secret would surface as an unexplained runtime failure instead of a
              # Flux error.
              - secretRef:
                  name: crosswatch-config-key
            probes:
              liveness: &probes
                enabled: true
                custom: true
                spec:
                  httpGet:
                    path: /
                    port: 8787
                  initialDelaySeconds: 30
                  periodSeconds: 30
                  timeoutSeconds: 5
                  failureThreshold: 5
              readiness: *probes
            resources:
              requests:
                memory: 256Mi
                cpu: 25m
              limits:
                memory: 1Gi

    service:
      app:
        controller: app
        ports:
          http:
            port: 8787

    persistence:
      config:
        existingClaim: crosswatch-config
        globalMounts:
          - path: /config
```

- [ ] **Step 4: Write the app kustomization**

Create `kubernetes/apps/media/crosswatch/app/kustomization.yml`. The route is added in Task 3, not here.

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./pvc.yml
  - ./config-key-sealed.yml
  - ./helmrelease.yml
```

- [ ] **Step 5: Write the Flux Kustomization**

Create `kubernetes/apps/media/crosswatch/ks.yml`. No `dependsOn: shared-gateway` yet — there is no route until Task 3.

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app crosswatch
  namespace: flux-system
spec:
  targetNamespace: media
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  path: ./kubernetes/apps/media/crosswatch/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  wait: false
  interval: 30m
  timeout: 10m
```

- [ ] **Step 6: Register it with the media kustomization**

In `kubernetes/apps/media/kustomization.yml`, add `./crosswatch/ks.yml` after the `./jellyfin/ks.yml` line:

```yaml
  - ./plex/ks.yml
  - ./jellyfin/ks.yml
  - ./crosswatch/ks.yml
  - ./suwayomi/ks.yml
```

- [ ] **Step 7: Validate every new manifest**

This is the test step. A schema violation that reaches `main` wedges the entire `media` Flux Kustomization.

```bash
for f in kubernetes/apps/media/crosswatch/ks.yml \
         kubernetes/apps/media/crosswatch/app/*.yml; do
  echo "--- $f"
  .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f"
done
```

Expected: no `FAILED:` lines.

- [ ] **Step 8: Commit**

```bash
git add kubernetes/apps/media/crosswatch kubernetes/apps/media/kustomization.yml
git commit -m "feat(crosswatch): deploy CrossWatch for Plex/Jellyfin/Trakt sync"
```

- [ ] **Step 9: Push, let Flux reconcile, and verify the pod runs**

```bash
git push -u origin feat/watch-sync
```

Merge to `main` (Flux deploys from `main` only), then:

```bash
mise exec -- flux reconcile kustomization crosswatch --with-source
mise exec -- kubectl get pods -n media -l app.kubernetes.io/name=crosswatch
mise exec -- kubectl logs -n media deploy/crosswatch --tail=30
```

Expected: pod `Running`, `1/1` ready, logs showing the server bound to 8787.

- [ ] **Step 10: Confirm the config volume is writable and the key took**

```bash
mise exec -- kubectl exec -n media deploy/crosswatch -- ls -la /config
```

Expected: `/config` exists and is writable by uid 2202. `config.json` may or may not exist yet — it is created on first UI save.

---

### Task 2: Provider credentials and pair A (Plex ↔ Jellyfin)

Delivers the first working sync: watch history converging between Plex and Jellyfin, directly. This is the phase-1 deliverable and is independently useful.

**Files:** none in git — this task is UI configuration, deliberately. Its output is recorded in prose in Task 6's decision doc.

**Interfaces:**
- Consumes: the running `crosswatch` deployment from Task 1.
- Produces: a configured pair whose id (e.g. `pair_07c3`) is needed by Task 5's verification commands. Record the id.

- [ ] **Step 1: Snapshot the Plex config PVC**

**Do not skip this.** `plex-config-local` is an `openebs-hostpath` PVC on cp-1 with no backup CronJob (`design/decisions/jellyfin.md`). Everything from here on writes to Plex's library database, and there is no restore path.

```bash
mise exec -- kubectl get pod -n media -l app.kubernetes.io/name=plex -o wide
```

Note the node. Then, from that node's hostpath, take a copy of the Plex config directory to the Synology share before proceeding. Verify the copy is non-empty and contains `Library/Application Support/Plex Media Server/Plug-in Support/Databases/`.

- [ ] **Step 2: Port-forward the CrossWatch UI**

```bash
mise exec -- kubectl port-forward -n media deploy/crosswatch 8787:8787
```

Open `http://localhost:8787`.

- [ ] **Step 3: Connect Plex**

In Settings → Connections, add Plex. Server URL is the in-cluster address:

```
http://plex.media.svc.cluster.local:32400
```

Complete the Plex account link. CrossWatch stores `plex.server_url` and `plex.account_token` encrypted in `/config/config.json`.

- [ ] **Step 4: Connect Jellyfin**

Same screen, add Jellyfin. Server URL:

```
http://jellyfin.media.svc.cluster.local:8096
```

Authenticate with a Jellyfin API key (Jellyfin Dashboard → API Keys → new key named `crosswatch`). CrossWatch stores `jellyfin.server`, `jellyfin.access_token` and `jellyfin.user_id`.

- [ ] **Step 5: Create pair A with watchlist sync OFF**

Settings → Pairs → new pair, source Plex, destination Jellyfin, two-way.

Enable: **history**, **progress**.
Disable: **watchlist**, ratings, collections.

Watchlist must be off. This is a global constraint — a watchlist pair fights `trakt-seerr-queue` in Task 4 and loops forever. Verify the toggle is genuinely per-pair; if it is global, that is a blocker and must be reported before Task 4.

- [ ] **Step 6: Record the pair id**

```bash
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync list
```

Expected: pair A listed with an id like `pair_07c3` and route `PLEX -> JELLYFIN`. Write the id down — Task 5 and Task 6 both need it.

- [ ] **Step 7: Run pair A once, manually, and read the result**

```bash
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync once --pair <pair-a-id>
```

Expected exit code 0. Codes mean: 1 command failed, 2 invalid usage, 3 cannot reach CrossWatch, 4 not allowed, 5 not found, 6 busy.

- [ ] **Step 8: Verify convergence with a real item**

Pick a single episode marked watched in Jellyfin but not in Plex. After the sync run, confirm it is now watched in Plex, and pick one the other direction and confirm the same. This is the actual test — exit code 0 only means the run completed.

- [ ] **Step 9: Enable the internal schedule for pair A**

Settings → Scheduling. Set pair A to run every 15 minutes. CrossWatch's own scheduler drives syncs; `cw sync once` is a client tool for manual runs, not the scheduling mechanism.

---

### Task 3: Keycloak client and the HTTPRoute

Puts the UI behind SSO and gives it a hostname. Done after Task 2 on purpose — the UI now holds live Plex and Jellyfin credentials, so it must not get a public hostname until OIDC is enforcing.

**Files:**
- Create: `kubernetes/apps/keycloak/clients/app/crosswatch.yml`
- Create: `kubernetes/apps/keycloak/clients/app/crosswatch-client-secret.yml` (local only, never committed)
- Create: `kubernetes/apps/keycloak/clients/app/crosswatch-client-sealed.yml`
- Create: `kubernetes/apps/media/crosswatch/app/route.yml`
- Modify: `kubernetes/apps/keycloak/clients/app/kustomization.yml`
- Modify: `kubernetes/apps/media/crosswatch/app/kustomization.yml`
- Modify: `kubernetes/apps/media/crosswatch/ks.yml`

**Interfaces:**
- Consumes: the `crosswatch` Service from Task 1.
- Produces: `https://crosswatch.blackcats.cc`, gated by the Keycloak `user` role.

- [ ] **Step 1: Generate and seal the client secret**

```bash
CW_SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-32)
cat > /tmp/crosswatch-client-secret.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: crosswatch-client-secret
  namespace: keycloak
type: Opaque
stringData:
  secret: ${CW_SECRET}
EOF
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < /tmp/crosswatch-client-secret.yaml \
  > kubernetes/apps/keycloak/clients/app/crosswatch-client-sealed.yml
echo "CLIENT SECRET (needed in the CrossWatch UI at Step 6): ${CW_SECRET}"
rm /tmp/crosswatch-client-secret.yaml
```

- [ ] **Step 2: Write the Keycloak client**

Create `kubernetes/apps/keycloak/clients/app/crosswatch.yml`:

```yaml
---
apiVersion: k8s.keycloak.org/v2alpha1
kind: KeycloakOIDCClient
metadata:
  # There is no `clientId` field on this CRD — the apiserver prunes one in silence.
  # metadata.name IS the client id.
  name: crosswatch
  namespace: keycloak
spec:
  keycloakCRName: keycloak
  realm: homelab
  client:
    displayName: CrossWatch
    enabled: true
    loginFlows: [STANDARD]
    redirectUris:
      # Confirm this path against CrossWatch's OIDC settings screen before the
      # first login attempt — api/authOidcAPI.py owns the callback route.
      - https://crosswatch.blackcats.cc/auth/oidc/callback
    webOrigins:
      - https://crosswatch.blackcats.cc
    # `user` is the entitlement gate, matching every other client in this realm.
    roles: [user]
    auth:
      method: client-secret
      secretRef:
        name: crosswatch-client-secret
        key: secret
```

- [ ] **Step 3: Register the client**

Add both files to `kubernetes/apps/keycloak/clients/app/kustomization.yml`, keeping the existing alphabetical grouping (`crosswatch` sorts before `freshrss`):

```yaml
  - ./crosswatch-client-sealed.yml
  - ./crosswatch.yml
```

- [ ] **Step 4: Write the HTTPRoute**

Create `kubernetes/apps/media/crosswatch/app/route.yml`:

```yaml
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: crosswatch
  namespace: media
  annotations:
    external-dns.alpha.kubernetes.io/enabled: "true"
spec:
  parentRefs:
    - name: shared
      namespace: gateway
      sectionName: https
  hostnames:
    - crosswatch.blackcats.cc
  rules:
    - backendRefs:
        - name: crosswatch
          port: 8787
```

- [ ] **Step 5: Wire the route into both kustomizations**

Add `./route.yml` to `kubernetes/apps/media/crosswatch/app/kustomization.yml`:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./pvc.yml
  - ./config-key-sealed.yml
  - ./helmrelease.yml
  - ./route.yml
```

And add the gateway dependency to `kubernetes/apps/media/crosswatch/ks.yml`, between `commonMetadata` and `path`:

```yaml
  dependsOn:
    - name: shared-gateway
```

- [ ] **Step 6: Enable OIDC in the CrossWatch UI**

OIDC is configured in the UI, not by env var. While still on the port-forward, go to Settings → Authentication and set:

- Issuer: `https://keycloak.blackcats.cc/realms/homelab`
- Client ID: `crosswatch`
- Client secret: the value printed in Step 1

Save, then confirm the callback URL the UI reports matches the `redirectUris` entry in Step 2. If it differs, correct `crosswatch.yml` before committing.

- [ ] **Step 7: Validate**

```bash
for f in kubernetes/apps/media/crosswatch/ks.yml \
         kubernetes/apps/media/crosswatch/app/route.yml \
         kubernetes/apps/keycloak/clients/app/crosswatch.yml \
         kubernetes/apps/keycloak/clients/app/crosswatch-client-sealed.yml; do
  echo "--- $f"
  .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f"
done
```

Expected: no `FAILED:` lines.

- [ ] **Step 8: Commit**

```bash
git add kubernetes/apps/media/crosswatch kubernetes/apps/keycloak/clients/app
git commit -m "feat(crosswatch): put the UI behind Keycloak on crosswatch.blackcats.cc"
```

- [ ] **Step 9: Verify SSO end to end**

After merge and reconcile:

```bash
mise exec -- kubectl get httproute -n media crosswatch
mise exec -- kubectl get keycloakoidcclient -n keycloak crosswatch
```

Then open `https://crosswatch.blackcats.cc` in a browser. Expected: redirect to Keycloak, login, redirect back into the UI. A user without the `user` role must be denied.

---

### Task 4: Pair B (Plex ↔ Trakt)

Merges the Hobi/Showly archive. This is the highest-risk step in the plan.

**Files:** none in git — UI configuration, recorded in prose in Task 6.

**Interfaces:**
- Consumes: the running CrossWatch from Task 1, Plex connection from Task 2.
- Produces: a second pair id, needed by Task 6's decision doc.

- [ ] **Step 1: Confirm the Plex snapshot from Task 2 Step 1 still exists and is current**

If Task 2 and Task 4 are separated by days of viewing, retake it. Everything below writes to Plex's library database and there is no restore path.

- [ ] **Step 2: Connect Trakt**

In Settings → Connections, add Trakt. This needs a Trakt API application — create one at `https://trakt.tv/oauth/applications` with redirect URI `urn:ietf:wg:oauth:2.0:oob` (or whatever CrossWatch's screen specifies), then enter `client_id` and `client_secret` and complete the device-code link.

CrossWatch stores `trakt.client_id`, `trakt.client_secret`, `trakt.access_token` and `trakt.refresh_token` encrypted, and refreshes the token itself.

- [ ] **Step 3: Create pair B with watchlist sync OFF**

Source Plex, destination Trakt, two-way.

Enable: **history**.
Disable: **watchlist**, ratings, collections, progress.

Watchlist off is the hard rule. With Task 5's CronJob live, a watchlist-syncing pair B re-adds every item the job removes.

- [ ] **Step 4: Preview before running, if CrossWatch offers it**

Check the pair screen for a dry-run or preview action. If one exists, run it and read the diff: count how many items it intends to mark watched in Plex, and spot-check a handful against memory. Old Showly data marking an abandoned show complete is the specific failure to look for.

If no preview exists, the Step 1 snapshot is the only safety net. Say so explicitly before continuing.

- [ ] **Step 5: Run pair B once**

```bash
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync list
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync once --pair <pair-b-id>
```

Expected: exit code 0.

- [ ] **Step 6: Verify the merge in both directions**

- Pick a show watched years ago in Hobi/Showly. Confirm it now reads watched in Plex.
- Pick something watched recently on Plex only. Confirm it now appears in the Trakt history at `https://trakt.tv/users/me/history`.
- Wait one pair-A interval, then confirm the Hobi/Showly show also reads watched in Jellyfin. That confirms the two-hop chain works.

- [ ] **Step 7: Enable the internal schedule for pair B**

Set pair B to every 15 minutes, same as pair A.

---

### Task 5: `trakt-seerr-queue` CronJob

The watchlist queue. Reads the Trakt watchlist, requests each item in Seerr, removes it on acceptance.

**Files:**
- Create: `kubernetes/apps/media/trakt-seerr-queue/ks.yml`
- Create: `kubernetes/apps/media/trakt-seerr-queue/app/kustomization.yml`
- Create: `kubernetes/apps/media/trakt-seerr-queue/app/pvc.yml`
- Create: `kubernetes/apps/media/trakt-seerr-queue/app/secrets-sealed.yml`
- Create: `kubernetes/apps/media/trakt-seerr-queue/app/script-configmap.yml`
- Create: `kubernetes/apps/media/trakt-seerr-queue/app/cronjob.yml`
- Modify: `kubernetes/apps/media/kustomization.yml`

**Interfaces:**
- Consumes: Seerr at `http://seerr.media.svc.cluster.local:5055`, Trakt API at `https://api.trakt.tv`. Independent of CrossWatch — it shares no volume and no credentials with it.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Gather the credentials and seal them**

Reuse the Trakt application created in Task 4 Step 2 — a second app is unnecessary. Obtain a Trakt OAuth access and refresh token for your own account, and a Seerr API key from Seerr → Settings → General → API Key.

```bash
cat > /tmp/trakt-queue-secrets.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: trakt-queue-secrets
  namespace: media
type: Opaque
stringData:
  TRAKT_CLIENT_ID: "<client id>"
  TRAKT_CLIENT_SECRET: "<client secret>"
  TRAKT_ACCESS_TOKEN: "<access token>"
  TRAKT_REFRESH_TOKEN: "<refresh token>"
  SEERR_API_KEY: "<seerr api key>"
EOF
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < /tmp/trakt-queue-secrets.yaml \
  > kubernetes/apps/media/trakt-seerr-queue/app/secrets-sealed.yml
rm /tmp/trakt-queue-secrets.yaml
```

- [ ] **Step 2: Write the token-state PVC**

Trakt refresh rotates the refresh token. A SealedSecret is immutable, so rotated tokens need somewhere writable to live.

Create `kubernetes/apps/media/trakt-seerr-queue/app/pvc.yml`:

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: trakt-queue-state
  namespace: media
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: nfs-client
  resources:
    requests:
      storage: 100Mi
```

- [ ] **Step 3: Write the script ConfigMap**

Create `kubernetes/apps/media/trakt-seerr-queue/app/script-configmap.yml`. The Python body is indented four spaces — Python at 0-indent inside a `|` block scalar breaks the kustomize parser.

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: trakt-seerr-queue-script
  namespace: media
data:
  queue.py: |
    #!/usr/bin/env python3
    """Drain the Trakt watchlist into Seerr.

    For each watchlist item with a TMDb id: request it in Seerr, and on
    acceptance remove it from the Trakt watchlist. The list therefore empties
    as it is fulfilled, which is what keeps it under Trakt's 100-item
    free-tier cap without any pruning.

    Only the standard library is used, so the image needs no `apk add` — which
    is also why this pod can run non-root with a read-only root filesystem.
    """
    import json
    import os
    import sys
    import urllib.error
    import urllib.request

    TRAKT = "https://api.trakt.tv"
    SEERR = "http://seerr.media.svc.cluster.local:5055"
    STATE = "/state/token.json"

    CLIENT_ID = os.environ["TRAKT_CLIENT_ID"]
    CLIENT_SECRET = os.environ["TRAKT_CLIENT_SECRET"]
    SEERR_API_KEY = os.environ["SEERR_API_KEY"]

    def log(msg):
        print(msg, flush=True)

    def load_tokens():
        """Prefer the rotated tokens on the PVC; fall back to the sealed seed.

        The seed is only ever read into an empty volume. Once Trakt rotates the
        refresh token the seed is stale, and that is correct — it is not a
        source of truth after first use.
        """
        try:
            with open(STATE) as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "access_token": os.environ["TRAKT_ACCESS_TOKEN"],
                "refresh_token": os.environ["TRAKT_REFRESH_TOKEN"],
            }

    def save_tokens(tokens):
        with open(STATE, "w") as fh:
            json.dump(tokens, fh)

    def request(url, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)

    def trakt_headers(tokens):
        return {
            "Authorization": "Bearer " + tokens["access_token"],
            "trakt-api-version": "2",
            "trakt-api-key": CLIENT_ID,
        }

    def refresh(tokens):
        log("trakt token rejected, refreshing")
        _, payload = request(
            TRAKT + "/oauth/token",
            method="POST",
            body={
                "refresh_token": tokens["refresh_token"],
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                "grant_type": "refresh_token",
            },
        )
        fresh = {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
        }
        save_tokens(fresh)
        return fresh

    def trakt_get(path, tokens):
        """GET with one transparent refresh-and-retry on 401."""
        try:
            _, payload = request(TRAKT + path, headers=trakt_headers(tokens))
            return payload, tokens
        except urllib.error.HTTPError as err:
            if err.code != 401:
                raise
            tokens = refresh(tokens)
            _, payload = request(TRAKT + path, headers=trakt_headers(tokens))
            return payload, tokens

    def seerr_request(media_type, tmdb_id):
        """Return True when the item is in Seerr's pipeline.

        409 means Seerr already has a request for it, which is the same
        end state as a fresh 201 — both mean the watchlist can let it go.
        """
        body = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            body["seasons"] = "all"
        try:
            status, _ = request(
                SEERR + "/api/v1/request",
                method="POST",
                body=body,
                headers={"X-Api-Key": SEERR_API_KEY},
            )
            return status in (200, 201)
        except urllib.error.HTTPError as err:
            if err.code == 409:
                log("  already requested in seerr")
                return True
            log("  seerr rejected: HTTP %d" % err.code)
            return False

    def trakt_remove(kind, trakt_id, tokens):
        request(
            TRAKT + "/sync/watchlist/remove",
            method="POST",
            body={kind: [{"ids": {"trakt": trakt_id}}]},
            headers=trakt_headers(tokens),
        )

    def drain(kind, media_type, tokens):
        """kind is trakt's plural noun ('movies'/'shows'); media_type is seerr's."""
        items, tokens = trakt_get("/sync/watchlist/" + kind, tokens)
        singular = kind[:-1]
        drained = 0
        for item in items or []:
            entry = item.get(singular) or {}
            title = entry.get("title", "?")
            ids = entry.get("ids") or {}
            tmdb_id = ids.get("tmdb")
            trakt_id = ids.get("trakt")
            if not tmdb_id or not trakt_id:
                # Left on the watchlist on purpose: a stuck item should stay
                # visible rather than vanish without ever being requested.
                log("skip (no tmdb id): %s" % title)
                continue
            log("requesting: %s (tmdb %s)" % (title, tmdb_id))
            if seerr_request(media_type, tmdb_id):
                trakt_remove(kind, trakt_id, tokens)
                log("  removed from watchlist")
                drained += 1
        return drained, tokens

    def main():
        tokens = load_tokens()
        total = 0
        for kind, media_type in (("movies", "movie"), ("shows", "tv")):
            drained, tokens = drain(kind, media_type, tokens)
            total += drained
        log("done: %d item(s) drained" % total)
        return 0

    if __name__ == "__main__":
        try:
            sys.exit(main())
        except Exception as exc:  # noqa: BLE001 - surface the cause in pod logs
            log("FAILED: %s" % exc)
            sys.exit(1)
```

- [ ] **Step 4: Write the CronJob**

Create `kubernetes/apps/media/trakt-seerr-queue/app/cronjob.yml`. No `apk add`, so none of the alpine-bootstrap annotations or root overrides are needed.

```yaml
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: trakt-seerr-queue
  namespace: media
spec:
  # The watchlist is edited by hand on a phone; 30m is well inside the latency
  # anyone would notice, and keeps Trakt API calls to 96/day.
  schedule: "*/30 * * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      activeDeadlineSeconds: 300
      backoffLimit: 0
      ttlSecondsAfterFinished: 3600
      template:
        spec:
          restartPolicy: Never
          securityContext:
            runAsUser: 2202
            runAsGroup: 2200
            fsGroup: 2200
            runAsNonRoot: true
          volumes:
            - name: script
              configMap:
                name: trakt-seerr-queue-script
                defaultMode: 0755
            - name: state
              persistentVolumeClaim:
                claimName: trakt-queue-state
          containers:
            - name: queue
              image: python:3.13-alpine
              command: ["python3", "/scripts/queue.py"]
              envFrom:
                # No `optional: true` — chart 3.7.3 strips it, and a missing
                # secret here would fail with a bare KeyError.
                - secretRef:
                    name: trakt-queue-secrets
              volumeMounts:
                - name: script
                  mountPath: /scripts
                - name: state
                  mountPath: /state
              securityContext:
                readOnlyRootFilesystem: true
                allowPrivilegeEscalation: false
                capabilities:
                  drop: ["ALL"]
              resources:
                requests:
                  memory: 64Mi
                  cpu: 25m
                limits:
                  memory: 256Mi
```

- [ ] **Step 5: Write the kustomization and Flux Kustomization**

Create `kubernetes/apps/media/trakt-seerr-queue/app/kustomization.yml`:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./pvc.yml
  - ./secrets-sealed.yml
  - ./script-configmap.yml
  - ./cronjob.yml
```

Create `kubernetes/apps/media/trakt-seerr-queue/ks.yml`:

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app trakt-seerr-queue
  namespace: flux-system
spec:
  targetNamespace: media
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  path: ./kubernetes/apps/media/trakt-seerr-queue/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  wait: false
  interval: 30m
  timeout: 10m
```

Add to `kubernetes/apps/media/kustomization.yml`, after the crosswatch line:

```yaml
  - ./crosswatch/ks.yml
  - ./trakt-seerr-queue/ks.yml
```

- [ ] **Step 6: Validate**

The ConfigMap is the one most likely to fail here — a kustomize parse error from the embedded Python shows up as a build failure, not a schema error.

```bash
for f in kubernetes/apps/media/trakt-seerr-queue/ks.yml \
         kubernetes/apps/media/trakt-seerr-queue/app/*.yml; do
  echo "--- $f"
  .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f"
done
mise exec -- kubectl kustomize kubernetes/apps/media/trakt-seerr-queue/app > /dev/null \
  && echo "kustomize build OK"
```

Expected: no `FAILED:` lines, and `kustomize build OK`.

- [ ] **Step 7: Commit**

```bash
git add kubernetes/apps/media/trakt-seerr-queue kubernetes/apps/media/kustomization.yml
git commit -m "feat(trakt-seerr-queue): drain the Trakt watchlist into Seerr requests"
```

- [ ] **Step 8: Test with one item, before the schedule fires**

Put exactly one movie on the Trakt watchlist that is not already in the library, then:

```bash
mise exec -- kubectl create job -n media --from=cronjob/trakt-seerr-queue queue-manual
mise exec -- kubectl logs -n media job/queue-manual -f
```

Expected log shape:

```
requesting: <title> (tmdb 12345)
  removed from watchlist
done: 1 item(s) drained
```

- [ ] **Step 9: Verify both sides**

- Seerr shows a new request for that title.
- `https://trakt.tv/users/me/watchlist` no longer lists it.

If the item was requested but not removed, the Trakt OAuth token lacks write scope — re-issue it. If it was removed but not requested, that is a bug and the item must be re-added by hand.

```bash
mise exec -- kubectl delete job -n media queue-manual
```

---

### Task 6: Documentation

The repo's rule is that design files describe the implemented state. This task is what makes the UI-authored configuration recoverable.

**Files:**
- Create: `design/decisions/watch-sync.md`
- Modify: `.claude/CLAUDE.md`
- Modify: `design/docs/services.md`
- Modify: `design/decisions/jellyfin-ui.md`
- Modify: `.github/renovate.json`

- [ ] **Step 1: Write the decision doc**

Create `design/decisions/watch-sync.md` following the shape of `design/decisions/plex.md` — a `**Read before editing:**` line, `## Why it exists`, `## Current state`, `## Rules`, `## Verify`.

It must contain, at minimum:

- The chain topology and why it is a chain: CrossWatch pairs are one source → one destination, so a mesh is not expressible, and the chain has no cycle.
- **Both pair definitions written out in full prose** — source, destination, direction, and exactly which features are enabled. This is the only record; `config.json` is on a PVC and not in git.
- Rule: **no CrossWatch pair may sync watchlists**, because it re-adds what `trakt-seerr-queue` removes, looping forever and filling Seerr with duplicates.
- Rule: **the Jellyfin Trakt plugin stays installed but unauthorized**, because authorizing it closes the Jellyfin→Trakt→Plex→Jellyfin cycle and inflates Trakt rewatch counts.
- Rule: **never auto-merge a CrossWatch bump** — pre-1.0, eight releases in the month before v0.12.3.
- The credential model: encrypted in `/config/config.json`, key from `crosswatch-config-key`; losing the key means re-entering every provider login.
- That `trakt-seerr-queue` removes on request-acceptance, not download-completion, and that a later failure leaves the item in Seerr's request list rather than the watchlist.
- No deployed version numbers beyond the durable constraint (pre-1.0, pin exactly).

- [ ] **Step 2: Add the routing table row**

In `.claude/CLAUDE.md`, add a row to the "Which file to read" table, after the `jellyfin-ui.md` row:

```
| watch history sync, CrossWatch, the Trakt→Seerr queue | design/decisions/watch-sync.md |
```

- [ ] **Step 3: Update the service inventory**

Add CrossWatch to `design/docs/services.md` with hostname `crosswatch.blackcats.cc` and auth model Keycloak OIDC (`user` role), matching the surrounding entries' format.

- [ ] **Step 4: Record the Jellyfin plugin decision**

In `design/decisions/jellyfin-ui.md`, amend the Trakt plugin entry so it reads as installed **and deliberately unauthorized**, with a pointer to `watch-sync.md`. Without this, six months from now "installed but not configured" is indistinguishable from an oversight.

- [ ] **Step 5: Add the Renovate entry**

In `.github/renovate.json`, add a package rule for `ghcr.io/cenodude/crosswatch` that disables automerge and requires a dependency dashboard approval, matching how the other pinned media images are handled.

- [ ] **Step 6: Commit**

```bash
git add design .claude/CLAUDE.md .github/renovate.json
git commit -m "docs(watch-sync): record the sync topology, pair definitions and rules"
```

Do not stage the five pre-existing modified files noted in Task 0 Step 2.

---

## Verification (whole system)

- [ ] Watch something on Plex → within 15 min it reads watched in Jellyfin and appears in Trakt history.
- [ ] Watch something on Jellyfin → within 30 min (two hops) it appears in Trakt history.
- [ ] Add a movie to the Trakt watchlist → within 30 min it is requested in Seerr and gone from the watchlist.
- [ ] `https://crosswatch.blackcats.cc` requires Keycloak login and denies a user without the `user` role.
- [ ] Neither pair has watchlist sync enabled:

```bash
mise exec -- kubectl exec -n media deploy/crosswatch -- cw sync list
mise exec -- kubectl get cronjob -n media trakt-seerr-queue
mise exec -- kubectl get pods -n media -l app.kubernetes.io/name=crosswatch
```

## Known blockers to report rather than work around

- **Watchlist sync is a global toggle, not per-pair** (checked in Task 2 Step 5). Then pair B cannot coexist with Task 5 as designed. Stop and report — the fix is a design change, not a workaround.
- **No dry-run/preview exists** (Task 4 Step 4). Not a blocker, but the Plex PVC snapshot becomes the only safety net and must be confirmed present before the first pair-B run.
- **The OIDC callback path differs from the guess in Task 3 Step 2.** Correct `crosswatch.yml` before committing rather than after; a wrong `redirectUris` fails the login with an opaque Keycloak error.
