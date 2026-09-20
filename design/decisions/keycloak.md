# Keycloak

**Read before editing:** `kubernetes/apps/keycloak/`

## Current state

Keycloak at `sso.blackcats.cc` is the identity provider for every application on SSO.
It replaced Zitadel in a single cutover; Zitadel survives only to serve Joplin's SAML
application and is retired when Joplin is deleted (`design/decisions/zitadel.md`).

Deployed by the **official Keycloak Operator**, installed from
`github.com/keycloak/keycloak-k8s-resources` at a pinned tag, filtered to
`/kubernetes` — the same GitRepository pattern as the Gateway API CRD bundle.

Considered and rejected: the Bitnami chart (images moved to `bitnamilegacy` and
Keycloak was dropped from Bitnami Secure, so it no longer resolves); a community chart
such as codecentric keycloakx (a third-party dependency for the component that gates
all authentication — the same bet that just failed with Bitnami); `bjw-s/app-template`
with the upstream image (cheaper, but hands every Keycloak sharp edge to us by hand:
probes, hostname semantics, proxy headers, upgrade-time schema migration).

**Namespace is `keycloak`, not `auth`.** The upstream kustomization hardcodes
`namespace: keycloak` and runs a `NamespaceTransformer` with
`setRoleBindingSubjects: allServiceAccounts`. Installing there means nothing is
rewritten and the operator's RBAC subjects resolve. The operator's Flux Kustomization
must therefore **not** set `targetNamespace`.

**Database** is a `keycloak` `Database` CR on the shared CNPG cluster, owned by a
`keycloak` managed role whose password is a SealedSecret in `postgres` mirrored into
`keycloak` by Reflector — identical in shape to Zitadel's, and in the
`postgres-backup-databases` list. Keycloak crash-loops with
`FATAL: password authentication failed` if the managed role loses its race with the
`Database` CR; see `design/decisions/cnpg.md`.

**Hostname `sso.blackcats.cc` is product-neutral on purpose.** It lands in the callback
URI of every application, so a product-named host would have to be rewritten again the
next time the IdP changes — which is exactly the cost this migration just paid.

**Realm** `homelab` comes from a `KeycloakRealmImport` CR. Pure GitOps, no Job and no
second OpenTofu provider lockfile to keep in sync.

**Clients** are `KeycloakOIDCClient` CRs under `kubernetes/apps/keycloak/clients/`, one
per application, each paired with a sealed client secret that git owns. OpenTofu was
considered and rejected: it would have meant a second Terraform apply-by-hand loop for
something the operator already models.

## Client provisioning

**It depends on an EXPERIMENTAL server feature.** `KeycloakOIDCClient` cannot reconcile
unless `client-admin-api:v2` is in `Keycloak.spec.features.enabled`, and Keycloak's own
feature registry types `CLIENT_ADMIN_API_V2` as EXPERIMENTAL — a tier *below* PREVIEW,
changeable or removable with no deprecation cycle. Since a Renovate bump of
`keycloak-k8s-resources` is a Keycloak upgrade here, treat every such bump as able to
break client reconciliation. Logins would survive a break: clients live in the realm
database as ordinary rows, and the CRs only converge them.

**The operator authenticates to itself.** Client reconciliation needs a
`<cr-name>-admin` Secret (so `keycloak-admin`) holding `client-id` and `client-secret`,
used for a client-credentials grant against the **master** realm. Ours is a
`keycloak-operator` service account client scoped to `manage-clients`, `view-clients`,
`query-clients` and `view-realm` on the `homelab-realm` client — not a realm admin.

**There is no `clientId` field on the CRD.** It defines none and sets no
`x-kubernetes-preserve-unknown-fields`, so the apiserver silently prunes one if you
write it: the manifest looks like it names the client and does nothing. The client id
is `metadata.name`.

`KeycloakOIDCClient` is `v2alpha1` and exposes no protocol mappers and no client
attributes. Anything outside its handful of fields is a console click. `client.roles`
takes bare strings only — no descriptions, no composites.

**Groups, group membership and group→role mappings are deliberately manual.** No CRD
expresses them, nor does one express the flow overrides below. This section is the only
record — git has none.

## Per-service entitlement

A realm user can otherwise log in to every client. Access is gated per service by a
**base role** that grants login, plus optional roles that grant privilege. Every group
for a client carries the base role, admin groups included — `KeycloakOIDCClient.roles`
takes bare strings, so composite roles are not expressible and the group carries both.

| Client | Base role (the gate) | Additional |
|---|---|---|
| gitea, immich, paperless, freshrss, kavita, romm, openwebui, proxmox | `user` | `admin` |
| grafana | `viewer` | `editor`, `admin` |

Groups mirror it: `/<service>/user`, `/<service>/admin` (and `/grafana/editor`). The
parent group holds no roles — children inherit from parents, not siblings.

**Keycloak has no "only users with a role may use this client" setting.** Enforcement is
a per-client browser-flow override, `browser-<service>`, ending in a CONDITIONAL
sub-flow of *Condition - user role* (`<client>.<base role>`, **negate on**) followed by
*Deny access*.

**The flow cannot be a plain copy of `browser` with the gate appended.** Keycloak
ignores every ALTERNATIVE execution that shares a level with a REQUIRED one — the gate
counts as REQUIRED, so appending it silently disables `auth-cookie`,
`identity-provider-redirector` and `forms`, and **nobody can authenticate at all**. The
server says so in the log and the user only sees "Invalid username or password":

```
REQUIRED and ALTERNATIVE elements at same level! Those alternative
executions will be ignored: [auth-cookie, identity-provider-redirector, ...]
```

So each flow nests the realm browser flow's executions inside a REQUIRED
`<service>-authenticate` sub-flow, leaving the gate as its only sibling:

```
browser-<svc>
├── <svc>-authenticate            REQUIRED
│   ├── auth-cookie                   ALTERNATIVE
│   ├── identity-provider-redirector  ALTERNATIVE
│   ├── <svc>-organization            ALTERNATIVE  → conditional org sub-flow
│   └── <svc>-forms                   ALTERNATIVE  → username-password + conditional 2FA
└── <svc>-gate                    CONDITIONAL
    ├── conditional-user-role         REQUIRED   (condUserRole=<svc>.<base>, negate=true)
    └── deny-access-authenticator     REQUIRED
```

**The gate belongs at the top level, not inside `forms`.** A user holding an SSO cookie
from another client never reaches `forms`, so a gate placed there is bypassed by exactly
the case it exists to stop. Verified: with a live session and the role removed, the
gated client still answers *Access denied*.

Setting an execution's requirement **resets its priority**, so build the whole flow
first, set every requirement, and only then fix the order with
`raise-priority`/`lower-priority`. Ordering within a CONDITIONAL sub-flow is cosmetic —
Keycloak evaluates its conditions before its actions regardless of position.

`kcadm.sh` cannot be used for any of this from inside the pod: the image has no
`curl`, `awk`, `jq` or `python3`, and `kcadm get --fields` silently omits nested objects
such as `authenticationFlowBindingOverrides`, which reads as "the change did not apply".
Drive the admin REST API over `https://sso.blackcats.cc` instead, and re-read with a
full GET. Clearing a flow override needs a full client PUT with the field set to
`{"browser": "", "direct_grant": ""}` — sending `{}` is treated as no change.

**Client secrets are owned by git**, sealed, named `keycloak/<app>-client-secret`, and
handed to Keycloak through `client.auth.secretRef`. A realm rebuild therefore does not
reissue credentials — the opposite of the Zitadel arrangement, where a DB reset
silently rotated every app's secret.

## Account re-linking — the cost of changing IdP

**Every application keys its SSO link on the `sub` claim, and `sub` changes when the
IdP changes.** Brokering to Zitadel would not have avoided this, since applications see
Keycloak's `sub`. This bit during the Immich-from-Swarm migration and bit again, in
nine places, here.

The symptom differs per app and none of them say "the subject changed":

| App | Where the subject is stored | Symptom when stale |
|---|---|---|
| Gitea | `external_login_user.external_id` (+ `user.login_name`) | offers to link a new account |
| Immich | `user.oauthId` | "OAuth authentication failed" |
| Paperless | `socialaccount_socialaccount.uid`, with `provider` = the allauth `provider_id` | offers to link |
| Kavita | `AspNetUsers.OidcId` (SQLite on the config PVC) | "Email already in use" |
| Grafana | `user_auth.auth_id` (SQLite) | "user sync failed … user not found" |
| RomM | nothing — matches on the `email` claim | none; it just works |

Rewriting that one value per app is the whole fix. Two traps:

- **Gitea and Kavita cannot be relinked through their UIs.** A Gitea user created by
  OAuth has `login_type=6` and no password, so the link page has nothing to
  authenticate against; Kavita refuses edits on OIDC-managed users outright ("Users
  managed by OIDC cannot be edited"). Both need the row rewritten directly. Kavita's
  `kavita.db` is in rollback-journal mode, not WAL, so scale the deployment to 0 first.
- **Grafana will not fall back to email.** `oauth_allow_insecure_email_lookup` defaults
  false, so a pre-existing user with the same email collides rather than linking.

RomM is the one app that links cleanly, because it matches on email and stores no
subject at all. The same design means **RomM roles cannot be driven from Keycloak** —
`users.role` and `permission_group_id` are local columns with no OIDC mapping. Manage
them in RomM.

## Realm mail

Realm mail goes through the Proton Bridge over a socat sidecar on `127.0.0.1:1025`,
because the bridge certificate's only SAN is `IP:127.0.0.1`
(`design/decisions/protonmail-bridge.md`). SMTP credentials reach the realm through
`KeycloakRealmImport.spec.placeholders`, which resolve `${SMTP_USERNAME}` /
`${SMTP_PASSWORD}` from a Secret.

**The `from:` address must be one Proton owns.** Proton rejects anything else at DATA,
not at MAIL FROM, with `554 5.0.0 Error: The sender or recipient address is not valid`
— so an SMTP connection test can pass while every real mail silently fails. Adding
`blackcats.cc` as a Proton custom domain would allow a service address here.

## Rules

- **Never generate a client secret with `openssl rand -base64`** — use
  `openssl rand -hex 32`. Base64 emits `+`, which decodes as a SPACE in the
  form-encoded client-credentials body, and Keycloak answers `401
  invalid_client_credentials` while the stored secret matches byte-for-byte.
- **Never write `spec.client.clientId`** — the field does not exist and is pruned in
  silence. `metadata.name` is the client id.
- **Never trust `validate-manifests.sh` on a `KeycloakOIDCClient`** — kubeconform has no
  schema for it and exits 0 for any content, including fields that will be pruned.
  Check the live object instead.
- **Never add an ordinary container to `Keycloak.spec.unsupported.podTemplate`** — the
  operator applies that template to the realm-import Job too, where a sidecar that never
  exits wedges the Job at 1/2 forever. Use a native sidecar (`initContainers` with
  `restartPolicy: Always`).
- **Never set `hostname.hostname` to a bare host** — it takes the full URL. TLS
  terminates at the Gateway, so without the scheme Keycloak advertises `http://` in its
  discovery document and every `redirect_uri` breaks.
- **Treat a `keycloak-k8s-resources` bump as a server upgrade** — the `Keycloak` CR
  carries no image field, so the operator deploys the server version it ships with and
  the rollout migrates the database schema. A `packageRules` entry labels these for
  manual review at every update type.
- **Do not record the deployed Keycloak version anywhere in `design/`.**

## Other gotchas

- **The realm import is one-shot.** The CRD has no `strategy` field: it creates the
  realm when absent and does not correct drift on an existing one. Realm-level settings
  are effectively frozen once a user exists, because rebuilding the realm means deleting
  it.
- **Health probes are on management port 9000**, not 8080. Matters when hand-debugging.
- **`db-url-properties: "?sslmode=disable"`** matches the cluster convention of
  cleartext Postgres on the internal VLAN. Without it the JDBC driver defaults to
  `sslmode=prefer` and negotiates TLS it does not verify.
- **The Service is `keycloak-service`** (`<cr-name>-service`) — that is what the
  HTTPRoute targets.
- **The initial admin is generated** into `keycloak-initial-admin` on first boot against
  an empty database; it is not in git. `spec.bootstrapAdmin` could point at a
  SealedSecret if a known-in-advance recovery credential is wanted.
- The operator's default requests/limits (1700Mi/2Gi per instance) are overridden. Note
  that there is no metrics-server, so do not resize from Goldilocks.

## Verify

```bash
mise exec -- kubectl get keycloaks,keycloakrealmimports,keycloakoidcclients -n keycloak
mise exec -- kubectl get keycloakoidcclient -n keycloak \
  -o custom-columns='NAME:.metadata.name,ERRORS:.status.conditions[?(@.type=="HasErrors")].status'
mise exec -- kubectl get keycloak keycloak -n keycloak -o jsonpath='{.spec.features.enabled}'
curl -s https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration | head -c 200
```
