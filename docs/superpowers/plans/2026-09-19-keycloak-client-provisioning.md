# Keycloak Client Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all nine OIDC applications from Zitadel to Keycloak in a single cutover, with clients, client roles and client secrets declared in git.

**Architecture:** Nine `KeycloakOIDCClient` CRs in the `keycloak` namespace, each pointing at a SealedSecret we generate, so client credentials are owned by git rather than issued by the identity provider. The same secret value is sealed a second time into each application's namespace in the exact key shape that application already consumes, which makes the cutover a value change rather than a restructure. Realm email moves to the Proton Mail Bridge behind a socat sidecar, because the bridge's certificate only matches `127.0.0.1`.

**Tech Stack:** Keycloak Operator CRDs (`k8s.keycloak.org/v2alpha1`, `v2beta1`), Sealed Secrets, Reflector, FluxCD Kustomizations, bjw-s app-template, OpenTofu (removal only).

**Spec:** `docs/superpowers/specs/2026-09-19-keycloak-client-provisioning-design.md`

## Global Constraints

- **Never write a raw `Secret`.** Author the plaintext as `<name>-secret.yml` (gitignored by the `*-secret.yml` rule) and seal it: `mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < <name>-secret.yml > <name>-sealed.yml`. Commit only the `-sealed.yml`.
- **Never use `Ingress`.** `HTTPRoute` with `parentRefs: [{name: shared, namespace: gateway}]`.
- **Never `kubectl apply` a config change.** Config goes through git and Flux. `kubectl` is for diagnostics and for the two explicitly-marked destructive admin-API steps in Task 4.
- **All k8s tooling is behind mise:** `mise exec -- kubectl|flux|kubeseal|talosctl|helm|kubeconform`.
- **After editing any manifest, run** `.agents/scripts/validate-manifests.sh <path>`. A schema violation that reaches `main` wedges the whole Flux Kustomization.
- **Realm name:** `homelab`. **Issuer:** `https://sso.blackcats.cc/realms/homelab`. **Discovery:** `https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration`. Endpoints are `<issuer>/protocol/openid-connect/{auth,token,userinfo}`.
- **`KeycloakOIDCClient` requires the server feature `client-admin-api:v2`**, enabled via `Keycloak.spec.features.enabled`. Keycloak classifies `CLIENT_ADMIN_API_V2` as **EXPERIMENTAL** — its lowest tier, below `PREVIEW`. Without it the operator refuses every client CR with *"Cannot create/update because the server does not have client-admin-api:v2 enabled"*. Accepted knowingly: clients already written are ordinary realm rows, so losing the feature degrades reconciliation, not logins.
- **`KeycloakOIDCClient` is `v2alpha1`** and exposes only `appUrl`, `auth`, `description`, `displayName`, `enabled`, `loginFlows`, `redirectUris`, `roles`, `serviceAccountRoles`, `webOrigins`. No protocol mappers, no client attributes. Anything outside that list is a console click.
- **`client.roles` is a list of bare strings.** Declare only roles the application actually reads.
- **Generate every client secret with `openssl rand -hex 32`, never base64.** A base64 secret contains `+` and `/`, and in an `application/x-www-form-urlencoded` body an unescaped `+` decodes as a SPACE — a consumer that does not percent-encode then sends the wrong secret. This is not hypothetical: it broke the operator's own admin login here with `invalid_client_credentials` while the stored secret matched byte for byte. Several of these apps post their secret in exactly that form (Open WebUI and Paperless are pinned to `client_secret_post`). Hex carries 256 bits and encodes identically everywhere.
- **Joplin is out of scope** and stays on Zitadel. Never remove `zitadel_application_saml.joplin`, `zitadel_action.joplin_saml_attributes` or `zitadel_trigger_actions.joplin_saml_attributes`.

## File Structure

**Created:**
- `kubernetes/apps/keycloak/clients/ks.yml` — Flux Kustomization `keycloak-clients`
- `kubernetes/apps/keycloak/clients/app/kustomization.yml` — resource list
- `kubernetes/apps/keycloak/clients/app/<app>.yml` × 9 — one `KeycloakOIDCClient` each
- `kubernetes/apps/keycloak/clients/app/<app>-client-sealed.yml` × 9 — client secret for the CR
- `kubernetes/apps/keycloak/keycloak/app/bridge-cert-mirror.yml` — Reflector-sourced cert mirror
- `kubernetes/apps/keycloak/keycloak/app/smtp-sealed.yml` — bridge SMTP credentials
- `kubernetes/apps/<ns>/<app>/app/oidc-sealed.yml` × 9 — the application-side secret

**Modified:**
- `kubernetes/apps/paperless/protonmail-bridge/app/helmrelease.yml:97-101` — publish SMTP
- `kubernetes/apps/keycloak/keycloak/app/keycloak.yml` — truststore + socat sidecar
- `kubernetes/apps/keycloak/realm/app/realmimport.yml` — final realm settings
- `kubernetes/apps/keycloak/kustomization.yml` — add `./clients/ks.yml`
- nine application HelmReleases and their `kustomization.yml` files
- `kubernetes/apps/auth/bootstrap/app/tofu/main.tf` — delete nine apps and nine secrets
- six `ks.yml` files carrying `dependsOn: zitadel-bootstrap`

**Deleted:**
- `kubernetes/apps/auth/bootstrap-rbac/` — entire directory, once no app secret is Terraform-owned

---

### Task 1: Publish SMTP on the Proton Mail Bridge Service

The bridge pod's entrypoint already runs socat to republish the loopback-bound SMTP listener on the pod IP as `:25`. Only the Service withholds it.

**Files:**
- Modify: `kubernetes/apps/paperless/protonmail-bridge/app/helmrelease.yml:97-101`

**Interfaces:**
- Produces: `protonmail-bridge.paperless.svc.cluster.local:25`, plain TCP, STARTTLS negotiated by the client.

- [ ] **Step 1: Verify SMTP is unreachable today**

```bash
mise exec -- kubectl run smtp-probe --rm -i --restart=Never -n keycloak \
  --image=busybox:1.37 --command -- \
  timeout 5 nc -z protonmail-bridge.paperless.svc.cluster.local 25
```

Expected: non-zero exit / no output. This is the failing check.

- [ ] **Step 2: Publish the port**

Replace lines 97-101 of `kubernetes/apps/paperless/protonmail-bridge/app/helmrelease.yml`:

```yaml
        ports:
          imap:
            port: 143
          # Keycloak sends realm mail (password reset, verify address) through
          # the bridge. The container already listens on :25 — its entrypoint
          # socat republishes the loopback-bound SMTP listener on the pod IP.
          smtp:
            port: 25
```

- [ ] **Step 3: Validate**

```bash
.agents/scripts/validate-manifests.sh kubernetes/apps/paperless/protonmail-bridge/app/helmrelease.yml
```

Expected: exit 0.

- [ ] **Step 4: Commit and reconcile**

```bash
git add kubernetes/apps/paperless/protonmail-bridge/app/helmrelease.yml
git commit -m "feat(protonmail-bridge): publish SMTP for Keycloak realm mail"
git push
mise exec -- flux reconcile kustomization protonmail-bridge --with-source
```

- [ ] **Step 5: Re-run the probe — it must now pass**

```bash
mise exec -- kubectl run smtp-probe --rm -i --restart=Never -n keycloak \
  --image=busybox:1.37 --command -- \
  timeout 5 nc -z protonmail-bridge.paperless.svc.cluster.local 25
```

Expected: exit 0.

---

### Task 2: Trust the bridge certificate and add the socat sidecar

The bridge certificate is `CN=127.0.0.1` with exactly one SAN, `IP:127.0.0.1`, valid to 2046, generated inside the bridge's encrypted vault and not reissuable. Any client verifying the hostname `protonmail-bridge.paperless.svc.cluster.local` fails. Paperless solved this with a loopback socat relay; Keycloak needs the same.

**Files:**
- Modify: `kubernetes/apps/keycloak/keycloak/app/keycloak.yml`
- No new manifest. The bridge cert Secret is created by the bridge's bootstrap, not by git, so it reaches the `keycloak` namespace by annotating the live source Secret for Reflector (step 2) — there is nothing to commit for it.

**Interfaces:**
- Consumes: `protonmail-bridge.paperless.svc.cluster.local:25` from Task 1.
- Produces: `127.0.0.1:1025` inside the Keycloak pod, STARTTLS-capable and certificate-valid.

- [ ] **Step 1: Confirm the certificate really is loopback-only**

```bash
mise exec -- kubectl get secret -n paperless protonmail-bridge-cert \
  -o jsonpath='{.data.cert\.pem}' | base64 -d | openssl x509 -noout -subject -ext subjectAltName
```

Expected: `subject=...CN=127.0.0.1` and `IP Address:127.0.0.1`. If this ever shows a DNS SAN covering the Service name, skip the sidecar and dial the Service directly.

- [ ] **Step 2: Allow the cert Secret to be reflected into `keycloak`**

The cert Secret is created by the bridge's bootstrap, not by git, so annotate it in place — this is state, not config, and is the one Secret this plan does not seal.

```bash
mise exec -- kubectl annotate secret -n paperless protonmail-bridge-cert \
  reflector.v1.k8s.emberstack.com/reflection-allowed=true \
  reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces=keycloak \
  reflector.v1.k8s.emberstack.com/reflection-auto-enabled=true \
  reflector.v1.k8s.emberstack.com/reflection-auto-namespaces=keycloak --overwrite
```

- [ ] **Step 3: Verify the mirror landed**

```bash
mise exec -- kubectl get secret -n keycloak protonmail-bridge-cert \
  -o jsonpath='{.metadata.annotations.reflector\.v1\.k8s\.emberstack\.com/reflects}{"\n"}'
```

Expected: `paperless/protonmail-bridge-cert`. If empty, wait 30s and retry; Reflector is event-driven but the source annotation change must propagate.

- [ ] **Step 4: Add the truststore and sidecar to the Keycloak CR**

Append to `spec` in `kubernetes/apps/keycloak/keycloak/app/keycloak.yml`:

```yaml
  # The Proton Mail Bridge certificate is CN=127.0.0.1 with a single
  # IP:127.0.0.1 SAN. Keycloak must both trust it and reach the bridge at an
  # address the certificate matches — hence the truststore here and the socat
  # sidecar below. Mirrored from paperless/protonmail-bridge-cert by Reflector.
  truststores:
    protonmail-bridge:
      secret:
        name: protonmail-bridge-cert

  unsupported:
    podTemplate:
      spec:
        containers:
          # Index 0 is the operator's own keycloak container; listing a second
          # entry appends a sidecar. It relays 127.0.0.1:1025 to the bridge as
          # plain TCP — it terminates no TLS, so STARTTLS still negotiates end
          # to end against the certificate Keycloak now trusts.
          - name: smtp-relay
            image: alpine/socat:1.8.0.3
            args:
              - TCP-LISTEN:1025,fork,reuseaddr,bind=127.0.0.1
              - TCP:protonmail-bridge.paperless.svc.cluster.local:25
            resources:
              requests:
                cpu: 10m
                memory: 16Mi
              limits:
                memory: 64Mi
            securityContext:
              allowPrivilegeEscalation: false
              runAsNonRoot: true
              runAsUser: 65534
              capabilities:
                drop: ["ALL"]
```

- [ ] **Step 5: Validate and commit**

```bash
.agents/scripts/validate-manifests.sh kubernetes/apps/keycloak/keycloak/app/keycloak.yml
git add kubernetes/apps/keycloak/keycloak/app/keycloak.yml
git commit -m "feat(keycloak): trust the Proton Bridge cert and relay SMTP over loopback"
git push
mise exec -- flux reconcile kustomization keycloak --with-source
```

- [ ] **Step 6: Verify the pod rolled with two containers and the handshake verifies**

```bash
mise exec -- kubectl get pod -n keycloak keycloak-0 \
  -o jsonpath='{range .spec.containers[*]}{.name}{"\n"}{end}'
```

Expected: `keycloak` and `smtp-relay`.

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c smtp-relay -- \
  timeout 10 nc -z 127.0.0.1 1025
```

Expected: exit 0. If the pod does not roll, the operator rejected the podTemplate merge — check `mise exec -- kubectl get keycloak -n keycloak keycloak -o jsonpath='{.status.conditions}'`.

---

### Task 3: Seal the bridge SMTP credentials

The bridge generates its own SMTP password; it is not chosen here. It is read from the running bridge's CLI.

**Files:**
- Create: `kubernetes/apps/keycloak/keycloak/app/smtp-secret.yml` (gitignored)
- Create: `kubernetes/apps/keycloak/keycloak/app/smtp-sealed.yml`
- Modify: `kubernetes/apps/keycloak/keycloak/app/kustomization.yml`

**Interfaces:**
- Produces: Secret `keycloak/keycloak-smtp` with keys `username`, `password`, consumed by the realm import in Task 4 via `placeholders`.

- [ ] **Step 1: Read the bridge's SMTP credentials**

```bash
mise exec -- kubectl exec -n paperless deploy/protonmail-bridge -- \
  /bin/sh -c 'echo "info" | bridge --cli'
```

Expected: the account block, including the bridge username (the Proton address) and the generated bridge password. If the CLI is not present in the image or the vault is locked, ask the operator for the password rather than guessing — it is the same credential Paperless's mail account already uses.

- [ ] **Step 2: Author the plaintext template**

`kubernetes/apps/keycloak/keycloak/app/smtp-secret.yml`:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: keycloak-smtp
  namespace: keycloak
type: Opaque
stringData:
  username: <bridge username from step 1>
  password: <bridge password from step 1>
```

- [ ] **Step 3: Seal it**

```bash
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < kubernetes/apps/keycloak/keycloak/app/smtp-secret.yml \
  > kubernetes/apps/keycloak/keycloak/app/smtp-sealed.yml
```

- [ ] **Step 4: Reference it**

Add `- ./smtp-sealed.yml` to `resources` in `kubernetes/apps/keycloak/keycloak/app/kustomization.yml`.

- [ ] **Step 5: Confirm the plaintext is not stageable**

```bash
git status --porcelain kubernetes/apps/keycloak/keycloak/app/smtp-secret.yml
```

Expected: empty output — the `*-secret.yml` gitignore rule covers it. If it shows up, stop and fix `.gitignore` before committing anything.

- [ ] **Step 6: Validate, commit, verify the Secret materialises**

```bash
.agents/scripts/validate-manifests.sh kubernetes/apps/keycloak/keycloak/app/smtp-sealed.yml
git add kubernetes/apps/keycloak/keycloak/app/smtp-sealed.yml kubernetes/apps/keycloak/keycloak/app/kustomization.yml
git commit -m "feat(keycloak): seal Proton Bridge SMTP credentials"
git push
mise exec -- flux reconcile kustomization keycloak --with-source
mise exec -- kubectl get secret -n keycloak keycloak-smtp -o jsonpath='{.data}' | jq 'keys'
```

Expected: `["password","username"]`.

---

### Task 4: Reshape the realm

`KeycloakRealmImport` is one-shot — the CRD has no `strategy` or `ifResourceExists` field. The realm currently has zero clients and zero users, so this is the last moment its settings are free to change. **Do not start this task after anyone has logged in.**

**Files:**
- Modify: `kubernetes/apps/keycloak/realm/app/realmimport.yml`

**Interfaces:**
- Consumes: `keycloak/keycloak-smtp` from Task 3; `127.0.0.1:1025` from Task 2.
- Produces: realm `homelab` able to send mail.

- [ ] **Step 1: Prove the realm is still empty**

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 \
  --realm master --user "$(mise exec -- kubectl get secret -n keycloak keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)" \
  --password "$(mise exec -- kubectl get secret -n keycloak keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)"
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /opt/keycloak/bin/kcadm.sh get users -r homelab --fields id
```

Expected: `[ ]`. If any user exists, STOP — re-shaping the realm would delete them. Escalate to the operator.

- [ ] **Step 2: Rewrite the realm import with its final shape**

Replace the `smtpServer` block in `kubernetes/apps/keycloak/realm/app/realmimport.yml` and update the stale header comment:

```yaml
    # Mail goes through the Proton Mail Bridge, not mailrise/Gotify: other
    # people use this realm and need self-service password reset to reach a
    # real inbox. host is 127.0.0.1 because the bridge certificate's only SAN
    # is IP:127.0.0.1 — the socat sidecar on the Keycloak pod relays to the
    # bridge Service. See design/decisions/protonmail-bridge.md.
    smtpServer:
      host: 127.0.0.1
      port: "1025"
      from: keycloak@blackcats.cc
      fromDisplayName: Keycloak
      ssl: "false"
      starttls: "true"
      auth: "true"
      user: ${SMTP_USERNAME}
      password: ${SMTP_PASSWORD}
```

And add the placeholder wiring to `spec`, above `realm:`:

```yaml
  placeholders:
    SMTP_USERNAME:
      secret:
        name: keycloak-smtp
        key: username
    SMTP_PASSWORD:
      secret:
        name: keycloak-smtp
        key: password
```

Also replace the "Clients are deliberately absent" paragraph at the top of the file with:

```yaml
# Clients are NOT defined here. They are KeycloakOIDCClient CRs under
# kubernetes/apps/keycloak/clients/, which reconcile; this import does not.
# Groups, group membership and group->role mappings are created by hand in the
# admin console — no CRD expresses them and OpenTofu was rejected for clients.
```

- [ ] **Step 3: Validate**

```bash
.agents/scripts/validate-manifests.sh kubernetes/apps/keycloak/realm/app/realmimport.yml
```

Expected: exit 0.

- [ ] **Step 4: Delete the realm and the import CR**

This is one of the two deliberate destructive `kubectl` steps in this plan. It is safe only because step 1 proved the realm is empty.

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /opt/keycloak/bin/kcadm.sh delete realms/homelab
mise exec -- kubectl delete keycloakrealmimport -n keycloak homelab
```

- [ ] **Step 5: Commit and let Flux recreate the import**

```bash
git add kubernetes/apps/keycloak/realm/app/realmimport.yml
git commit -m "feat(keycloak): send realm mail through the Proton Bridge"
git push
mise exec -- flux reconcile kustomization keycloak-realm --with-source
```

- [ ] **Step 6: Verify the import completed**

```bash
mise exec -- kubectl get keycloakrealmimport -n keycloak homelab \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'
```

Expected: `Done=True`, `HasErrors=False`.

- [ ] **Step 7: Prove mail actually sends**

In the admin console, Realm settings → Email → **Test connection**. Expected: success, and a message in the target inbox.

If it fails with a TLS error, the truststore did not load — check `mise exec -- kubectl logs -n keycloak keycloak-0 -c keycloak | grep -i truststore`. If it fails with connection refused, the sidecar is not listening — recheck Task 2 step 6.

- [ ] **Step 8: Commit nothing further; record the result**

No file changes. Note in the PR description whether the test mail arrived.

---

### Task 5: Scaffold `keycloak-clients` and prove it with Gitea

One client end-to-end before writing eight more. Gitea is the pilot because it keeps a local admin, so a mistake cannot lock anyone out.

**Files:**
- Create: `kubernetes/apps/keycloak/clients/ks.yml`
- Create: `kubernetes/apps/keycloak/clients/app/kustomization.yml`
- Create: `kubernetes/apps/keycloak/clients/app/gitea.yml`
- Create: `kubernetes/apps/keycloak/clients/app/gitea-client-secret.yml` (gitignored)
- Create: `kubernetes/apps/keycloak/clients/app/gitea-client-sealed.yml`
- Modify: `kubernetes/apps/keycloak/kustomization.yml`

**Interfaces:**
- Produces: Flux Kustomization `keycloak-clients`; the convention every later client follows — CR named `<app>` (the CRD has no `clientId` field, so `metadata.name` is what becomes the client id), secret `keycloak/<app>-client-secret` key `secret`.

- [ ] **Step 1: Generate and seal the client secret**

```bash
CS=$(openssl rand -hex 32)
cat > kubernetes/apps/keycloak/clients/app/gitea-client-secret.yml <<EOF
---
apiVersion: v1
kind: Secret
metadata:
  name: gitea-client-secret
  namespace: keycloak
type: Opaque
stringData:
  secret: ${CS}
EOF
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < kubernetes/apps/keycloak/clients/app/gitea-client-secret.yml \
  > kubernetes/apps/keycloak/clients/app/gitea-client-sealed.yml
```

The plaintext file must survive until Task 7, which seals the **same** value into the `gitea` namespace. It stays on disk at that path, uncommitted — the `*-secret.yml` gitignore rule covers it. Task 9 deletes it. Do not regenerate the value in Task 7: a mismatch between the two seals is an authentication failure that looks exactly like a misconfigured client.

- [ ] **Step 2: Write the client CR**

`kubernetes/apps/keycloak/clients/app/gitea.yml`:

```yaml
---
apiVersion: k8s.keycloak.org/v2alpha1
kind: KeycloakOIDCClient
metadata:
  name: gitea
  namespace: keycloak
spec:
  keycloakCRName: keycloak
  realm: homelab
  client:
    # NOTE: there is no `clientId` field. The v2alpha1 CRD does not define one
    # anywhere, and it sets no x-kubernetes-preserve-unknown-fields, so the
    # apiserver silently PRUNES any clientId you write here — the manifest
    # would read as though it set the client's identity while doing nothing.
    # The client id comes from metadata.name, which is why the CR is named
    # after the app. Step 6 verifies this rather than assuming it.
    displayName: Gitea
    enabled: true
    loginFlows: [STANDARD]
    # The provider-name segment is case-sensitive and moves Zitadel -> Keycloak
    # at cutover, which changes the callback path. It must match the
    # oauth[].name that Task 7 writes into gitea-oidc-secret exactly.
    redirectUris:
      - https://gitea.blackcats.cc/user/oauth2/Keycloak/callback
    webOrigins:
      - https://gitea.blackcats.cc
    roles:
      - admin
      - user
    auth:
      method: client-secret
      secretRef:
        name: gitea-client-secret
        key: secret
```

- [ ] **Step 3: Write the kustomization and the Flux Kustomization**

`kubernetes/apps/keycloak/clients/app/kustomization.yml`:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./gitea-client-sealed.yml
  - ./gitea.yml
```

`kubernetes/apps/keycloak/clients/ks.yml`:

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app keycloak-clients
  namespace: flux-system
spec:
  targetNamespace: keycloak
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  dependsOn:
    - name: keycloak-realm
    - name: sealed-secrets
  path: ./kubernetes/apps/keycloak/clients/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  # wait: false — KeycloakOIDCClient is v2alpha1 and reports no health status
  # Flux can gate on; waiting would stall the Kustomization indefinitely.
  wait: false
  interval: 30m
  timeout: 10m
```

Add `- ./clients/ks.yml` to `resources` in `kubernetes/apps/keycloak/kustomization.yml`.

- [ ] **Step 4: Validate every new manifest**

```bash
for f in kubernetes/apps/keycloak/clients/ks.yml \
         kubernetes/apps/keycloak/clients/app/gitea.yml \
         kubernetes/apps/keycloak/clients/app/gitea-client-sealed.yml; do
  .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f"
done
```

Expected: no `FAILED` lines.

- [ ] **Step 5: Commit and reconcile**

```bash
git add kubernetes/apps/keycloak/clients kubernetes/apps/keycloak/kustomization.yml
git commit -m "feat(keycloak): add keycloak-clients Kustomization with the Gitea client"
git push
mise exec -- flux reconcile kustomization keycloak-clients --with-source
```

- [ ] **Step 6: Verify the client exists in Keycloak with its roles**

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /opt/keycloak/bin/kcadm.sh get clients -r homelab -q clientId=gitea --fields id,clientId,enabled
```

Expected: one entry, `clientId: gitea`, `enabled: true`.

**This is a hard gate, not a formality.** There is no `clientId` field on the CRD, so this
value is whatever the operator derives — the convention assumes `metadata.name`. Every
application's OIDC configuration in Task 7 hardcodes this string. If the query returns no
match, or returns a client whose id is a UUID or anything other than `gitea`, STOP: Task 6
and Task 7 both need rework, and the eight-client fan-out must not proceed until the real
naming rule is known.

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /bin/sh -c 'ID=$(/opt/keycloak/bin/kcadm.sh get clients -r homelab -q clientId=gitea --fields id --format csv --noquotes); /opt/keycloak/bin/kcadm.sh get clients/$ID/roles -r homelab --fields name'
```

Expected: `admin` and `user`.

- [ ] **Step 7: Verify the client secret is the one we sealed**

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /bin/sh -c 'ID=$(/opt/keycloak/bin/kcadm.sh get clients -r homelab -q clientId=gitea --fields id --format csv --noquotes); /opt/keycloak/bin/kcadm.sh get clients/$ID/client-secret -r homelab --fields value'
```

Expected: the same value as `grep 'secret:' kubernetes/apps/keycloak/clients/app/gitea-client-secret.yml`. **If it differs, the `secretRef` was ignored and the whole git-owned-secret premise has failed** — stop and escalate before writing eight more clients.

---

### Task 6: The remaining eight clients

Mechanical repeat of Task 5 now that the pattern is proven. Redirect URIs are taken from `design/docs/services.md`, "OIDC Callback URIs (non-obvious)".

**Files:**
- Create: `kubernetes/apps/keycloak/clients/app/{immich,paperless,freshrss,kavita,romm,grafana,openwebui,proxmox}.yml`
- Create: the matching `*-client-sealed.yml` for each
- Modify: `kubernetes/apps/keycloak/clients/app/kustomization.yml`

**Interfaces:**
- Consumes: the conventions established in Task 5.
- Produces: nine clients total, each with a git-owned secret at `keycloak/<app>-client-secret` key `secret`.

- [ ] **Step 1: Generate and seal eight client secrets**

```bash
for app in immich paperless freshrss kavita romm grafana openwebui proxmox; do
  CS=$(openssl rand -hex 32)
  cat > kubernetes/apps/keycloak/clients/app/${app}-client-secret.yml <<EOF
---
apiVersion: v1
kind: Secret
metadata:
  name: ${app}-client-secret
  namespace: keycloak
type: Opaque
stringData:
  secret: ${CS}
EOF
  mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
    < kubernetes/apps/keycloak/clients/app/${app}-client-secret.yml \
    > kubernetes/apps/keycloak/clients/app/${app}-client-sealed.yml
done
```

- [ ] **Step 2: Write the eight CRs**

A complete example follows the table. Every field that varies between the eight is in the table, so the example plus one table row fully determines each file — do not go looking for another task to copy from.

| File | `metadata.name` (becomes the client id) | `displayName` | `redirectUris` | `webOrigins` | `roles` |
|---|---|---|---|---|---|
| `immich.yml` | `immich` | Immich | `https://immich.blackcats.cc/auth/login`, `https://immich.blackcats.cc/user-settings`, `https://immich.blackcats.cc/api/oauth/mobile-redirect` | `https://immich.blackcats.cc` | `[]` |
| `paperless.yml` | `paperless` | Paperless-ngx | `https://paperless.blackcats.cc/accounts/oidc/keycloak/login/callback/` | `https://paperless.blackcats.cc` | `[]` |
| `freshrss.yml` | `freshrss` | FreshRSS | `https://rss.blackcats.cc/i/oidc/` | `https://rss.blackcats.cc` | `[]` |
| `kavita.yml` | `kavita` | Kavita | `https://kavita.blackcats.cc/signin-oidc` | `https://kavita.blackcats.cc` | `[]` |
| `romm.yml` | `romm` | RomM | `https://romm.blackcats.cc/api/oauth/openid` | `https://romm.blackcats.cc` | `[]` |
| `grafana.yml` | `grafana` | Grafana | `https://grafana.blackcats.cc/login/generic_oauth` | `https://grafana.blackcats.cc` | `admin`, `editor`, `viewer` |
| `openwebui.yml` | `openwebui` | Open WebUI | `https://chat.blackcats.cc/oauth/oidc/callback` | `https://chat.blackcats.cc` | `[]` |
| `proxmox.yml` | `proxmox` | Proxmox VE | `https://pve.blackcats.cc:8006`, `https://pve.blackcats.cc` | `https://pve.blackcats.cc:8006` | `[]` |

Grafana is the only one with roles, because it is the only one of the nine that maps an OIDC claim onto its own permission model via `role_attribute_path`. The rest get `roles: []` — declaring an `admin` role no application reads creates a permission nobody has and a claim nobody checks. Add roles later, per application, when something consumes them.

Template, with `immich` substituted:

```yaml
---
apiVersion: k8s.keycloak.org/v2alpha1
kind: KeycloakOIDCClient
metadata:
  name: immich
  namespace: keycloak
spec:
  keycloakCRName: keycloak
  realm: homelab
  client:
    # No `clientId` field exists on this CRD — see Task 5. The client id is
    # taken from metadata.name, verified against the live cluster in Task 5
    # step 6 before this fan-out was written.
    displayName: Immich
    enabled: true
    loginFlows: [STANDARD]
    redirectUris:
      - https://immich.blackcats.cc/auth/login
      - https://immich.blackcats.cc/user-settings
      - https://immich.blackcats.cc/api/oauth/mobile-redirect
    webOrigins:
      - https://immich.blackcats.cc
    roles: []
    auth:
      method: client-secret
      secretRef:
        name: immich-client-secret
        key: secret
```

- [ ] **Step 3: Extend the kustomization**

`kubernetes/apps/keycloak/clients/app/kustomization.yml`:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./freshrss-client-sealed.yml
  - ./freshrss.yml
  - ./gitea-client-sealed.yml
  - ./gitea.yml
  - ./grafana-client-sealed.yml
  - ./grafana.yml
  - ./immich-client-sealed.yml
  - ./immich.yml
  - ./kavita-client-sealed.yml
  - ./kavita.yml
  - ./openwebui-client-sealed.yml
  - ./openwebui.yml
  - ./paperless-client-sealed.yml
  - ./paperless.yml
  - ./proxmox-client-sealed.yml
  - ./proxmox.yml
  - ./romm-client-sealed.yml
  - ./romm.yml
```

- [ ] **Step 4: Validate all of them**

```bash
for f in kubernetes/apps/keycloak/clients/app/*.yml; do
  .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f"
done
```

Expected: no `FAILED` lines.

- [ ] **Step 5: Commit and reconcile**

```bash
git add kubernetes/apps/keycloak/clients
git commit -m "feat(keycloak): add the remaining eight OIDC clients"
git push
mise exec -- flux reconcile kustomization keycloak-clients --with-source
```

- [ ] **Step 6: Verify all nine exist**

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /opt/keycloak/bin/kcadm.sh get clients -r homelab --fields clientId \
  | grep -cE 'gitea|immich|paperless|freshrss|kavita|romm|grafana|openwebui|proxmox'
```

Expected: `9`.

- [ ] **Step 7: Check RomM's ID token carries userinfo claims**

RomM needed "User Info inside ID Token" under Zitadel, and the CRD cannot express protocol mappers. Confirm Keycloak's default `profile` and `email` client scopes already put `email` and `preferred_username` in the ID token:

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /bin/sh -c 'ID=$(/opt/keycloak/bin/kcadm.sh get clients -r homelab -q clientId=romm --fields id --format csv --noquotes); /opt/keycloak/bin/kcadm.sh get clients/$ID/default-client-scopes -r homelab --fields name'
```

Expected: includes `email` and `profile`. If either is missing, add it in the admin console and record it in the follow-up doc as a known manual step — this is the console-click bucket the spec warned about.

---

### Task 7: Author the nine application-side secrets, unreferenced

These files are written and sealed but **deliberately not added to any `kustomization.yml`**. Flux therefore ignores them, so they sit inert in git while Zitadel keeps serving. Task 8 activates all nine at once. This is what makes the cutover a single atomic commit instead of a race with the Zitadel bootstrap.

**Files:**
- Create: `kubernetes/apps/gitea/gitea/app/oidc-sealed.yml`
- Create: `kubernetes/apps/immich/immich/app/oidc-sealed.yml`
- Create: `kubernetes/apps/paperless/paperless/app/oidc-sealed.yml`
- Create: `kubernetes/apps/freshrss/freshrss/app/oidc-sealed.yml`
- Create: `kubernetes/apps/media/kavita/app/oidc-sealed.yml`
- Create: `kubernetes/apps/media/romm/app/oidc-sealed.yml`
- Create: `kubernetes/apps/monitoring/vm-stack/app/oidc-sealed.yml`
- Create: `kubernetes/apps/ai/open-webui/app/oidc-sealed.yml`
- Create: `kubernetes/apps/auth/bootstrap/app/proxmox-oidc-sealed.yml`

**Interfaces:**
- Consumes: the nine gitignored plaintext files `kubernetes/apps/keycloak/clients/app/<app>-client-secret.yml` written by Tasks 5 and 6. Read each value with:
  `yq '.stringData.secret' kubernetes/apps/keycloak/clients/app/<app>-client-secret.yml`
- Produces: nine SealedSecrets whose Secret names and keys are byte-identical to what the Zitadel bootstrap writes today, so no consumer changes.

- [ ] **Step 1: Record the shapes being replaced**

Each plaintext template below reproduces the current Secret's keys exactly, with `zitadel.blackcats.cc` swapped for the Keycloak issuer and the opaque numeric client IDs swapped for our chosen ones. Write each as `<dir>/oidc-secret.yml` (gitignored), substituting the `<app CS>` placeholder with that app's value read from its Task 5/6 plaintext file:

```bash
yq '.stringData.secret' kubernetes/apps/keycloak/clients/app/<app>-client-secret.yml
```

**Gitea** — `kubernetes/apps/gitea/gitea/app/oidc-secret.yml`. Note `name: Keycloak`: Gitea derives its callback path from this, and it must match the CR's `redirectUris`.

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: gitea-oidc-secret
  namespace: gitea
type: Opaque
stringData:
  values.yaml: |
    "gitea":
      "oauth":
      - "autoDiscoverUrl": "https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration"
        "key": "gitea"
        "name": "Keycloak"
        "provider": "openidConnect"
        "scopes": "openid email profile"
        "secret": "<gitea CS>"
```

**Immich** — `kubernetes/apps/immich/immich/app/oidc-secret.yml`. `passwordLogin.enabled` is flipped to `true`: Immich currently has **no local login at all**, so a failed cutover locks everyone out of it permanently. Task 9 turns it back off once OIDC is confirmed working.

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: immich-oidc-config
  namespace: immich
type: Opaque
stringData:
  immich.json: |
    {
      "oauth": {
        "autoRegister": true,
        "buttonText": "Login with SSO",
        "clientId": "immich",
        "clientSecret": "<immich CS>",
        "enabled": true,
        "issuerUrl": "https://sso.blackcats.cc/realms/homelab",
        "mobileOverrideEnabled": true,
        "mobileRedirectUri": "https://immich.blackcats.cc/api/oauth/mobile-redirect",
        "scope": "openid email profile",
        "signingAlgorithm": "RS256"
      },
      "passwordLogin": {
        "enabled": true
      }
    }
```

**Paperless** — `kubernetes/apps/paperless/paperless/app/oidc-secret.yml`. `provider_id` moves `zitadel` → `keycloak`, which rewrites the allauth callback path; it must match the CR.

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: paperless-oidc-secret
  namespace: paperless
type: Opaque
stringData:
  PAPERLESS_SOCIALACCOUNT_PROVIDERS: |
    {"openid_connect":{"APPS":[{"client_id":"paperless","name":"Keycloak","provider_id":"keycloak","secret":"<paperless CS>","settings":{"server_url":"https://sso.blackcats.cc/realms/homelab","token_auth_method":"client_secret_post"}}]}}
```

**FreshRSS** — `kubernetes/apps/freshrss/freshrss/app/oidc-secret.yml`:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: freshrss-oidc-secret
  namespace: freshrss
type: Opaque
stringData:
  OIDC_CLIENT_ID: freshrss
  OIDC_CLIENT_SECRET: "<freshrss CS>"
```

**Kavita** — `kubernetes/apps/media/kavita/app/oidc-secret.yml`:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: kavita-oidc-secret
  namespace: media
type: Opaque
stringData:
  OIDC_CLIENT_ID: kavita
  OIDC_CLIENT_SECRET: "<kavita CS>"
```

**RomM** — `kubernetes/apps/media/romm/app/oidc-secret.yml`:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: romm-oidc-secret
  namespace: media
type: Opaque
stringData:
  OIDC_CLIENT_ID: romm
  OIDC_CLIENT_SECRET: "<romm CS>"
  OIDC_ENABLED: "true"
  OIDC_PROVIDER: Keycloak
  OIDC_REDIRECT_URI: https://romm.blackcats.cc/api/oauth/openid
  OIDC_SERVER_APPLICATION_URL: https://sso.blackcats.cc/realms/homelab
```

**Grafana** — `kubernetes/apps/monitoring/vm-stack/app/oidc-secret.yml`:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: grafana-oidc-secret
  namespace: monitoring
type: Opaque
stringData:
  GF_AUTH_GENERIC_OAUTH_CLIENT_ID: grafana
  GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET: "<grafana CS>"
```

**Open WebUI** — `kubernetes/apps/ai/open-webui/app/oidc-secret.yml`:

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: openwebui-oidc-secret
  namespace: ai
type: Opaque
stringData:
  OAUTH_CLIENT_ID: openwebui
  OAUTH_CLIENT_SECRET: "<openwebui CS>"
```

**Proxmox** — `kubernetes/apps/auth/bootstrap/app/proxmox-oidc-secret.yml`. Not consumed by any pod; it exists so the credentials can be copied into Proxmox by hand with `pveum`.

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: proxmox-oidc-secret
  namespace: auth
type: Opaque
stringData:
  ISSUER_URL: https://sso.blackcats.cc/realms/homelab
  OIDC_CLIENT_ID: proxmox
  OIDC_CLIENT_SECRET: "<proxmox CS>"
```

- [ ] **Step 2: Seal all nine**

```bash
for p in kubernetes/apps/gitea/gitea/app kubernetes/apps/immich/immich/app \
         kubernetes/apps/paperless/paperless/app kubernetes/apps/freshrss/freshrss/app \
         kubernetes/apps/media/kavita/app kubernetes/apps/media/romm/app \
         kubernetes/apps/monitoring/vm-stack/app kubernetes/apps/ai/open-webui/app; do
  mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
    < "$p/oidc-secret.yml" > "$p/oidc-sealed.yml"
done
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < kubernetes/apps/auth/bootstrap/app/proxmox-oidc-secret.yml \
  > kubernetes/apps/auth/bootstrap/app/proxmox-oidc-sealed.yml
```

- [ ] **Step 3: Prove no plaintext is stageable**

```bash
git status --porcelain | grep -E 'oidc-secret\.yml|client-secret\.yml' && echo "LEAK" || echo "clean"
```

Expected: `clean`. If `LEAK`, stop and fix `.gitignore` before committing.

- [ ] **Step 4: Validate the sealed files**

```bash
for f in $(git status --porcelain --untracked-files=all | awk '/oidc-sealed\.yml/ {print $2}'); do
  .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f"
done
```

Expected: no `FAILED` lines.

- [ ] **Step 5: Commit them, still unreferenced**

```bash
git add $(git status --porcelain --untracked-files=all | awk '/oidc-sealed\.yml/ {print $2}')
git commit -m "feat(keycloak): stage the nine app-side OIDC secrets, not yet wired"
git push
```

- [ ] **Step 6: Prove they are inert**

```bash
mise exec -- flux reconcile kustomization gitea --with-source
mise exec -- kubectl get secret -n gitea gitea-oidc-secret \
  -o jsonpath='{.data.values\.yaml}' | base64 -d | grep -c zitadel
```

Expected: `1` — still the Zitadel value. Nothing has changed for users. If this returns `0`, a `kustomization.yml` picked the file up early; revert and fix before Task 8.

---

### Task 8: The cutover

One commit. It activates the nine staged secrets, repoints the inline issuer configuration, and removes Terraform's ownership of those secrets in the same breath. Splitting it would let the hourly Zitadel bootstrap overwrite secrets SealedSecrets now owns.

**Files:**
- Modify: nine `kustomization.yml` files, to add `./oidc-sealed.yml`
- Modify: `kubernetes/apps/freshrss/freshrss/app/helmrelease.yml:84`
- Modify: `kubernetes/apps/media/kavita/app/helmrelease.yml:65`
- Modify: `kubernetes/apps/monitoring/vm-stack/app/helmrelease.yml:173-181`
- Modify: `kubernetes/apps/ai/open-webui/app/helmrelease.yml:103-105`
- Modify: `kubernetes/apps/auth/bootstrap/app/tofu/main.tf`
- Modify: six `ks.yml` files with `dependsOn: zitadel-bootstrap`
- Delete: `kubernetes/apps/auth/bootstrap-rbac/`
- Modify: `kubernetes/apps/auth/kustomization.yml`

**Interfaces:**
- Consumes: everything from Tasks 5-7.
- Produces: nine applications authenticating against Keycloak.

**Rollback — read before starting.** Zitadel is untouched by this task and still holds all nine clients, so recovery is a revert:

```bash
mise exec -- flux suspend kustomization zitadel-bootstrap   # if not already suspended
git revert --no-edit <cutover commit>
git push
mise exec -- flux resume kustomization zitadel-bootstrap
mise exec -- flux reconcile kustomization zitadel-bootstrap --with-source
```

The revert restores the Terraform resource blocks and removes the `removed` blocks. Because those removals only dropped the resources from state and left the real Zitadel clients and Secrets intact, the next bootstrap run re-imports nothing but re-adopts them — it rewrites all nine `*-oidc-secret` Secrets with the Zitadel values. If a client was somehow destroyed, the apply recreates it with a NEW client secret, and the rewritten Secret carries that new value, so the applications still converge; only the Zitadel-side client IDs change. Then repeat step 10's rollout restart so the applications pick them up. The Keycloak client CRs are additive and can be left in place.

The one thing a revert does **not** undo is Task 4's realm rebuild — which is why that task happens while the realm is empty, and why it is a separate commit from this one.

- [ ] **Step 1: Reference the nine staged secrets**

Add `- ./oidc-sealed.yml` to `resources` in each of:
`kubernetes/apps/gitea/gitea/app/kustomization.yml`,
`kubernetes/apps/immich/immich/app/kustomization.yml`,
`kubernetes/apps/paperless/paperless/app/kustomization.yml`,
`kubernetes/apps/freshrss/freshrss/app/kustomization.yml`,
`kubernetes/apps/media/kavita/app/kustomization.yml`,
`kubernetes/apps/media/romm/app/kustomization.yml`,
`kubernetes/apps/monitoring/vm-stack/app/kustomization.yml`,
`kubernetes/apps/ai/open-webui/app/kustomization.yml`.

And `- ./proxmox-oidc-sealed.yml` to `kubernetes/apps/auth/bootstrap/app/kustomization.yml`.

- [ ] **Step 2: Repoint the four inline issuer configurations**

`kubernetes/apps/freshrss/freshrss/app/helmrelease.yml:84`:

```yaml
              OIDC_PROVIDER_METADATA_URL: "https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration"
```

`kubernetes/apps/media/kavita/app/helmrelease.yml:65`:

```yaml
              OIDC_AUTHORITY: https://sso.blackcats.cc/realms/homelab
```

`kubernetes/apps/monitoring/vm-stack/app/helmrelease.yml`, replacing lines 173 and 178-180:

```yaml
          name: Keycloak
```

```yaml
          auth_url: https://sso.blackcats.cc/realms/homelab/protocol/openid-connect/auth
          token_url: https://sso.blackcats.cc/realms/homelab/protocol/openid-connect/token
          api_url: https://sso.blackcats.cc/realms/homelab/protocol/openid-connect/userinfo
          # Keycloak puts client roles at resource_access.<clientId>.roles.
          # Without this every SSO user lands on auto_assign_org_role (Admin).
          role_attribute_path: "contains(resource_access.grafana.roles[*], 'admin') && 'Admin' || contains(resource_access.grafana.roles[*], 'editor') && 'Editor' || 'Viewer'"
          role_attribute_strict: "false"
```

`kubernetes/apps/ai/open-webui/app/helmrelease.yml:103` and `:105`:

```yaml
              OPENID_PROVIDER_URL: https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration
```

```yaml
              OAUTH_PROVIDER_NAME: Keycloak
```

Leave `OAUTH_TOKEN_ENDPOINT_AUTH_METHOD: client_secret_post` as it is — Keycloak's `client-secret` authenticator accepts both `client_secret_basic` and `client_secret_post`, and the pin still protects against an upstream default flipping underneath the app.

- [ ] **Step 3: Remove the nine applications from the Zitadel bootstrap**

**Do not simply delete the resource blocks.** Terraform state holds all eighteen; a resource removed from config is DESTROYED on the next apply. That would delete the nine Zitadel clients — destroying the rollback path this task depends on — and delete the nine `*-oidc-secret` Secrets that SealedSecrets now owns. OpenTofu 1.12.6 is in use (`ghcr.io/opentofu/opentofu:1.12.6` in `job.yml`), so use `removed` blocks, which drop a resource from state while leaving the real object alone.

Replace each of these eighteen resource blocks:

```
zitadel_application_oidc.immich       kubernetes_secret_v1.immich_oidc_config
zitadel_application_oidc.freshrss     kubernetes_secret_v1.freshrss_oidc_secret
zitadel_application_oidc.paperless    kubernetes_secret_v1.paperless_oidc_secret
zitadel_application_oidc.gitea        kubernetes_secret_v1.gitea_oidc_secret
zitadel_application_oidc.grafana      kubernetes_secret_v1.grafana_oidc_secret
zitadel_application_oidc.kavita       kubernetes_secret_v1.kavita_oidc_secret
zitadel_application_oidc.romm         kubernetes_secret_v1.romm_oidc_secret
zitadel_application_oidc.proxmox      kubernetes_secret_v1.proxmox_oidc_secret
zitadel_application_oidc.openwebui    kubernetes_secret_v1.openwebui_oidc_secret
```

...with an equivalent `removed` block. Eighteen in total, one per deleted resource:

```hcl
removed {
  from = zitadel_application_oidc.immich
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.immich_oidc_config
  lifecycle {
    destroy = false
  }
}
```

...and the same pair for `freshrss`/`freshrss_oidc_secret`, `paperless`/`paperless_oidc_secret`, `gitea`/`gitea_oidc_secret`, `grafana`/`grafana_oidc_secret`, `kavita`/`kavita_oidc_secret`, `romm`/`romm_oidc_secret`, `proxmox`/`proxmox_oidc_secret`, `openwebui`/`openwebui_oidc_secret`. Note the Immich secret resource is named `immich_oidc_config`, not `immich_oidc_secret`.

The `removed` blocks stay in `main.tf` until Zitadel is retired; deleting them once state no longer holds those addresses is a no-op cleanup, not part of this task.

Keep `zitadel_project.homelab`, `data.zitadel_orgs.default`, `locals`, `variable.zitadel_pat`, the `terraform`/`provider` blocks, and all three Joplin resources.

Confirm nothing was missed:

```bash
grep -cE '^resource "zitadel_application_oidc"' kubernetes/apps/auth/bootstrap/app/tofu/main.tf
grep -cE '^resource "kubernetes_secret_v1"' kubernetes/apps/auth/bootstrap/app/tofu/main.tf
grep -cE '^resource "zitadel_application_saml"' kubernetes/apps/auth/bootstrap/app/tofu/main.tf
```

Expected: `0`, `0`, `1`. And confirm the removals are declared:

```bash
grep -c '^removed {' kubernetes/apps/auth/bootstrap/app/tofu/main.tf
```

Expected: `18`.

- [ ] **Step 4: Drop the now-dead cross-namespace RBAC**

With no `kubernetes_secret_v1` left, the bootstrap Job writes into no other namespace.

```bash
git rm -r kubernetes/apps/auth/bootstrap-rbac
```

Remove `- ./bootstrap-rbac/ks.yml` from `kubernetes/apps/auth/kustomization.yml`, and remove the `- name: zitadel-bootstrap-rbac` entry from `dependsOn` in `kubernetes/apps/auth/bootstrap/ks.yml`.

- [ ] **Step 5: Cut the `zitadel-bootstrap` dependency from the migrated apps**

In each of `kubernetes/apps/ai/open-webui/ks.yml`, `kubernetes/apps/freshrss/freshrss/ks.yml`, `kubernetes/apps/gitea/gitea/ks.yml`, `kubernetes/apps/immich/immich/ks.yml`, `kubernetes/apps/paperless/paperless/ks.yml`: replace the `- name: zitadel-bootstrap` line in `dependsOn` with:

```yaml
    - name: keycloak-clients
```

**Do not touch `kubernetes/apps/joplin/joplin/ks.yml`** — Joplin stays on Zitadel and still needs the bootstrap.

- [ ] **Step 6: Validate everything touched**

```bash
for f in $(git diff --name-only HEAD; git status --porcelain --untracked-files=all | awk '{print $2}'); do
  case "$f" in kubernetes/*.yml) .agents/scripts/validate-manifests.sh "$f" || echo "FAILED: $f";; esac
done
```

Expected: no `FAILED` lines.

- [ ] **Step 7: Suspend the Zitadel bootstrap before pushing**

Belt and braces: the Job self-heals hourly, and Flux applies the Kustomizations in no guaranteed order.

```bash
mise exec -- flux suspend kustomization zitadel-bootstrap
```

- [ ] **Step 8: Commit and push the cutover**

```bash
git add -A
git commit -m "feat(sso): cut all nine OIDC apps over from Zitadel to Keycloak"
git push
mise exec -- flux reconcile kustomization flux-system --with-source
```

- [ ] **Step 9: Confirm every secret flipped**

```bash
for s in gitea/gitea-oidc-secret immich/immich-oidc-config paperless/paperless-oidc-secret \
         freshrss/freshrss-oidc-secret media/kavita-oidc-secret media/romm-oidc-secret \
         monitoring/grafana-oidc-secret ai/openwebui-oidc-secret auth/proxmox-oidc-secret; do
  ns=${s%%/*}; n=${s##*/}
  printf '%-32s %s\n' "$n" "$(mise exec -- kubectl get secret -n $ns $n -o json | jq -r '.data|to_entries|map(.value|@base64d)|join(" ")' | grep -oE 'zitadel|sso\.blackcats\.cc' | sort -u | tr '\n' ' ')"
done
```

Expected: every row shows `sso.blackcats.cc` and none shows `zitadel`. FreshRSS, Kavita, Grafana and Open WebUI hold only IDs and secrets, so they will show neither — check those four by confirming their client ID is now the plain app name.

- [ ] **Step 10: Restart the applications that do not auto-reload**

```bash
for d in gitea/gitea immich/immich paperless/paperless freshrss/freshrss \
         media/kavita media/romm monitoring/vm-stack-grafana ai/open-webui; do
  mise exec -- kubectl rollout restart deploy/${d##*/} -n ${d%%/*} 2>/dev/null \
    || mise exec -- kubectl rollout restart statefulset/${d##*/} -n ${d%%/*} 2>/dev/null
done
```

Then wait for all pods Ready:

```bash
mise exec -- kubectl get pods -A | grep -vE 'Running|Completed'
```

Expected: no application rows.

- [ ] **Step 11: Re-enable the Zitadel bootstrap and confirm it no longer fights**

```bash
mise exec -- flux resume kustomization zitadel-bootstrap
mise exec -- flux reconcile kustomization zitadel-bootstrap --with-source
mise exec -- kubectl logs -n auth job/zitadel-bootstrap -c tofu --tail=40
```

Expected: the apply reports eighteen resources **forgotten** (removed from state, not destroyed) and no changes beyond Joplin.

**If the log shows any `zitadel_application_oidc` or `kubernetes_secret_v1` being _destroyed_ rather than forgotten, the `removed` blocks did not take effect.** Suspend the Kustomization again immediately — the Zitadel clients are the rollback path, and losing them strands the migration with no way back. Restore them from `main.tf` history and re-apply before anyone logs in.

---

### Task 9: Verify every login and re-link accounts

**Files:** none until the last step.

**Interfaces:**
- Consumes: the completed cutover.

- [ ] **Step 1: Create the human users**

In the admin console, realm `homelab`, create one user per person with a verified email address matching their old Zitadel account. Email matching is what lets most applications auto-link instead of creating a duplicate.

- [ ] **Step 2: Create groups and assign Grafana's roles**

Groups are manual by design — no CRD expresses them. Create whatever groups the household needs, and map `grafana:admin` / `grafana:editor` / `grafana:viewer` onto them. Record the resulting layout in the decision doc in Task 10, since git has no other record of it.

- [ ] **Step 3: Log in to all nine, as a real user**

Gitea, Immich, Paperless, FreshRSS, Kavita, RomM, Grafana, Open WebUI, and Proxmox (after step 5). For each, confirm: the SSO button appears, the redirect completes, and you land authenticated on an account that owns your existing data rather than a fresh empty one.

Where a duplicate account is created instead, re-link in that application's own settings — the `sub` claim changed, so this is expected, not a bug.

- [ ] **Step 4: Confirm a client role reaches the token**

```bash
mise exec -- kubectl exec -n keycloak keycloak-0 -c keycloak -- \
  /bin/sh -c 'ID=$(/opt/keycloak/bin/kcadm.sh get clients -r homelab -q clientId=grafana --fields id --format csv --noquotes); /opt/keycloak/bin/kcadm.sh get clients/$ID/roles -r homelab --fields name'
```

Expected: `admin`, `editor`, `viewer`. Then log into Grafana as a user in the admin group and confirm the account has the Admin org role rather than the `auto_assign_org_role` default.

- [ ] **Step 5: Re-point Proxmox by hand**

Proxmox is not in the cluster. Read the new values and update its OIDC realm:

```bash
mise exec -- kubectl get secret -n auth proxmox-oidc-secret \
  -o jsonpath='{.data.ISSUER_URL}' | base64 -d; echo
```

Then on 172.16.20.3, update the realm with `pveum realm modify <name> --issuer-url ... --client-id proxmox --client-key ...`. See `design/runbook.md`.

- [ ] **Step 6: Close Immich's password login**

Task 7 opened it as a cutover safety net. Once Immich SSO is confirmed working for every user, set `"passwordLogin": {"enabled": false}` in `kubernetes/apps/immich/immich/app/oidc-secret.yml`, re-seal, and commit.

```bash
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml \
  < kubernetes/apps/immich/immich/app/oidc-secret.yml \
  > kubernetes/apps/immich/immich/app/oidc-sealed.yml
.agents/scripts/validate-manifests.sh kubernetes/apps/immich/immich/app/oidc-sealed.yml
git add kubernetes/apps/immich/immich/app/oidc-sealed.yml
git commit -m "fix(immich): close password login now that Keycloak SSO is verified"
git push
```

- [ ] **Step 7: Destroy the plaintext client secrets**

```bash
find kubernetes/apps \( -name 'oidc-secret.yml' -o -name '*-client-secret.yml' -o -name 'smtp-secret.yml' -o -name 'proxmox-oidc-secret.yml' \) -delete
git status --porcelain --untracked-files=all | grep -E 'secret\.yml' && echo "STILL PRESENT" || echo "clean"
```

Expected: `clean`.

The sealed copies in git and the live Secrets are the only remaining holders.

---

### Task 10: Documentation

The repo convention: design files describe the **implemented** state, and a gotcha belongs in a decision file, not in `AGENTS.md`.

**Files:**
- Create: `design/decisions/keycloak.md`
- Modify: `design/docs/services.md`
- Modify: `design/decisions/zitadel.md`
- Modify: `design/decisions/protonmail-bridge.md`
- Modify: `.claude/CLAUDE.md`
- Modify: `design/TODO.md`

- [ ] **Step 1: Write `design/decisions/keycloak.md`**

Fold in the gotcha list from `docs/superpowers/specs/2026-09-18-keycloak-deployment-design.md` (which was blocked on a `design/` restructure at the time), and add what this migration established:

- **Client management depends on an EXPERIMENTAL Keycloak feature.** `KeycloakOIDCClient` cannot reconcile unless `client-admin-api:v2` is enabled in `Keycloak.spec.features.enabled`, and Keycloak types `CLIENT_ADMIN_API_V2` as EXPERIMENTAL — below PREVIEW, changeable or removable with no deprecation cycle. Because a Renovate bump of `keycloak-k8s-resources` is a Keycloak upgrade here, treat every such bump as able to break client reconciliation. Logins would survive: clients live in the realm database as ordinary rows.
- `KeycloakOIDCClient` is `v2alpha1` and exposes no protocol mappers and no client attributes; anything outside its ten fields is a console click.
- **There is no `clientId` field.** The CRD defines none and sets no `x-kubernetes-preserve-unknown-fields`, so the apiserver silently prunes one if written — it looks like it names the client and does nothing. `metadata.name` is the client id.
- `client.roles` takes bare strings only — no descriptions, no composites.
- Groups, group membership and group→role mappings are **deliberately manual**. No CRD expresses them; OpenTofu was considered and rejected. Record the actual group layout here, because git holds no other record of it.
- Client secrets are owned by git (`keycloak/<app>-client-secret`, sealed), not issued by Keycloak. A realm rebuild therefore does not reissue credentials — the opposite of the Zitadel arrangement.
- The realm import is one-shot; the CRD has no `strategy` field. Realm-level settings are effectively frozen once a user exists.
- Realm mail goes through the Proton Bridge over a socat sidecar on `127.0.0.1:1025`, because the bridge certificate's only SAN is `IP:127.0.0.1`.
- Do **not** record the deployed Keycloak version.

- [ ] **Step 2: Fix `design/docs/services.md`**

- Add a Keycloak row (`keycloak` namespace, `sso.blackcats.cc`, operator-managed).
- Change the auth column for the nine migrated applications from Zitadel to Keycloak.
- Rewrite the "OIDC Callback URIs (non-obvious)" table: Gitea's segment is now `Keycloak`, Paperless's `provider_id` is now `keycloak`.
- **Correct the Gatus and Goldilocks rows.** Both are listed as "Zitadel OIDC" and neither has ever had a client or a secret. They are not on SSO.
- Keep Joplin's SAML section as-is.

- [ ] **Step 3: Update `design/decisions/zitadel.md`**

Zitadel now serves exactly one application. Say so, and say that it is retained only until Joplin is deleted. Correct the claim that `main.tf` owns every `client_id`/`client_secret` in the cluster — it owns Joplin's SAML configuration and nothing else. Note that `bootstrap-rbac` is gone.

- [ ] **Step 4: Update `design/decisions/protonmail-bridge.md`**

Add Keycloak as a second consumer, and generalise the certificate trap: the `CN=127.0.0.1`/`IP:127.0.0.1`-only certificate forces every in-cluster consumer onto a loopback relay. Paperless uses a socat sidecar at `127.0.0.1:1143`; Keycloak uses one at `127.0.0.1:1025` declared through `Keycloak.spec.unsupported.podTemplate`. The Service now publishes both 143 and 25.

- [ ] **Step 5: Add the routing row to `.claude/CLAUDE.md`**

In the "Which file to read" table, after the Zitadel row:

```
| Keycloak, SSO clients, realm, client roles           | design/decisions/keycloak.md          |
```

- [ ] **Step 6: Update `design/TODO.md`**

Add: retire Zitadel once Joplin is deleted — HelmRelease, database, managed role, bootstrap Job, Terraform, DNS and the `auth` namespace.

- [ ] **Step 7: Commit**

```bash
git add design .claude/CLAUDE.md
git commit -m "docs: record the Keycloak migration and correct the Zitadel-era claims"
git push
```
