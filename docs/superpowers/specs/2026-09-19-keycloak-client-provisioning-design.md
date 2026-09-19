# Keycloak client provisioning and big-bang cutover from Zitadel

Status: designed 2026-09-19. Not implemented.

Predecessor: `2026-09-18-keycloak-deployment-design.md`, which stood the platform up.
That document deferred one decision — how OIDC clients get provisioned — to "first-app
migration". This document makes that decision and covers the migration itself.

## Goal

Move all nine OIDC applications from Zitadel to Keycloak in a single cutover, with
clients, client roles and client secrets managed declaratively in git.

Joplin stays on Zitadel. It is scheduled for deletion, and porting its SAML
integration — a `KeycloakSAMLClient` plus protocol mappers replacing Zitadel's
attribute-rename action — is work with a known expiry date. Zitadel therefore survives
this migration and is retired in a follow-up once Joplin is gone.

## Scope

In scope: nine `KeycloakOIDCClient` CRs, their client roles, nine git-owned client
secrets, realm SMTP through the Proton Mail Bridge, the cutover commit that repoints
every application, and removal of those nine applications from the Zitadel bootstrap.

Out of scope, deliberately:

- **Joplin and SAML**, and with them Zitadel's retirement.
- **Group and role *assignment* automation.** Roles are declared per client in git;
  groups, group membership and group→role mappings are created by hand in the admin
  console. See "Mechanism" for why.
- **Gatus and Goldilocks.** `design/docs/services.md` lists both as "Zitadel OIDC".
  Neither has a Terraform client or an OIDC secret — they are not on SSO at all. That
  is a documentation error to fix separately, not a migration target.

## Mechanism

**Chosen: `KeycloakOIDCClient` CRs with client secrets owned by git.**

Each application gets a CR in the `keycloak` namespace whose `client.auth.secretRef`
points at a SealedSecret we generate. The operator's controller reconciles the client
continuously.

Rejected: an OpenTofu bootstrap Job cloned from `auth/bootstrap`. It would have covered
groups and group→role mappings as well, which the CRDs cannot. It was rejected in
favour of keeping client provisioning native to the operator and keeping client
credentials out of Terraform state — accepting manual group administration as the
price. This is a deliberate trade, not an oversight.

Rejected: declaring clients inside `KeycloakRealmImport`. The import is one-shot (the
CRD has no `strategy` or `ifResourceExists` field; its only knobs are `keycloakCRName`,
`labels`, `placeholders` and `resources`), so no client would ever be corrected after
the realm has users.

### Consequences to accept

- **Client management depends on an EXPERIMENTAL Keycloak feature.** Discovered by
  applying the first client, not by reading the CRD: the operator refuses every
  `KeycloakOIDCClient` with *"Cannot create/update because the server does not have
  client-admin-api:v2 enabled"*. The server's own feature registry types
  `CLIENT_ADMIN_API_V2` as `EXPERIMENTAL` — Keycloak's lowest tier, below `PREVIEW` —
  and it must be turned on explicitly in `Keycloak.spec.features.enabled`.

  This was put to the operator against the alternative of switching to the rejected
  OpenTofu approach, which uses the stable admin REST API, and enabling it was chosen
  deliberately. The mitigating fact: clients, once written, are ordinary rows in the
  realm database. If the feature changes or disappears — and in this repo a Renovate
  bump of `keycloak-k8s-resources` *is* a Keycloak upgrade — logins keep working and
  only reconciliation of further changes breaks.

- **There is no `clientId` field.** The CRD defines none anywhere and sets no
  `x-kubernetes-preserve-unknown-fields`, so the apiserver silently prunes one if
  written: the manifest reads as though it names the client while doing nothing.
  `metadata.name` is what becomes the client id.

- **No protocol mappers and no client attributes.** `KeycloakOIDCClient.client` exposes
  `appUrl`, `auth`, `description`, `displayName`, `enabled`, `loginFlows`,
  `redirectUris`, `roles`, `serviceAccountRoles` and `webOrigins` — and nothing else.
  Custom claims, PKCE enforcement, token lifespans and post-logout redirect URIs beyond
  the defaults are console clicks. RomM is the application most likely to need one; it
  required "User Info inside ID Token" under Zitadel. Verify it first.
- **`client.roles` is a list of bare strings.** No descriptions, no composite roles.
- **`v2alpha1`.** In this repo a Renovate bump of `keycloak-k8s-resources` is a Keycloak
  upgrade, so CR schema churn arrives with server upgrades. Treat a bump as able to
  break these manifests.

## Design

### 1. Reshape the realm before anything else

Realm `homelab` currently has zero clients and zero users. Because the import is
one-shot, this is the last moment at which realm-level settings are free to change.
Delete the realm and re-import with its final shape — SMTP included — as the first
step. After a single user exists, every one of these settings becomes a console click.

Mechanically: delete the realm through the admin API (authenticating with the
generated `keycloak-initial-admin` Secret), then delete and recreate the
`KeycloakRealmImport` CR so the operator reruns its import Job against the now-absent
realm. The realm's `smtpServer` block must already carry its **final** values at this
point even though the sidecar and truststore from section 4 may land in the same
commit — the import only stores the configuration, it does not dial the server, so the
two can be committed together but the values cannot be filled in later.

### 2. One secret value per application, sealed twice

For each application generate one random client secret and seal it into two places:

- `keycloak/<app>-client-secret`, key `secret` — consumed by `client.auth.secretRef`.
- `<app-namespace>/<app>-oidc-secret` — written **in the shape the application already
  consumes**, replacing the Secret the Zitadel bootstrap writes today.

Because we own the value, the second Secret can be authored directly in each
application's existing format. No consumption path changes anywhere:

| Application | Namespace | Secret | Keys (unchanged) |
|---|---|---|---|
| Gitea | `gitea` | `gitea-oidc-secret` | `values.yaml` (Helm `valuesFrom` fragment) |
| Immich | `immich` | `immich-oidc-config` | `immich.json` |
| Paperless | `paperless` | `paperless-oidc-secret` | `PAPERLESS_SOCIALACCOUNT_PROVIDERS` |
| RomM | `media` | `romm-oidc-secret` | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_ENABLED`, `OIDC_PROVIDER`, `OIDC_REDIRECT_URI`, `OIDC_SERVER_APPLICATION_URL` |
| Kavita | `media` | `kavita-oidc-secret` | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` |
| FreshRSS | `freshrss` | `freshrss-oidc-secret` | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` |
| Grafana | `monitoring` | `grafana-oidc-secret` | `GF_AUTH_GENERIC_OAUTH_CLIENT_ID`, `GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET` |
| Open WebUI | `ai` | `openwebui-oidc-secret` | `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` |
| Proxmox VE | `auth` | `proxmox-oidc-secret` | `ISSUER_URL`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` |

Client IDs become deterministic values chosen in git rather than opaque strings issued
by the identity provider — a second reason a realm rebuild stops being an incident.

Proxmox is not in-cluster. Its Secret exists so the credentials can be copied into a
Proxmox OIDC realm by hand with `pveum`; that manual step survives unchanged, and the
issuer URL it carries changes with everything else.

### 3. Nine client CRs

New Flux Kustomization `keycloak-clients` at `kubernetes/apps/keycloak/clients/`,
depending on `keycloak` and `sealed-secrets`. One CR per application:

```yaml
apiVersion: k8s.keycloak.org/v2alpha1
kind: KeycloakOIDCClient
metadata:
  name: gitea
  namespace: keycloak
spec:
  keycloakCRName: keycloak
  realm: homelab
  client:
    clientId: gitea
    displayName: Gitea
    enabled: true
    loginFlows: [STANDARD]
    redirectUris: ["https://gitea.blackcats.cc/user/oauth2/Keycloak/callback"]
    webOrigins: ["https://gitea.blackcats.cc"]
    roles: [admin, user]
    auth:
      method: client-secret
      secretRef:
        name: gitea-client-secret
        key: secret
```

Roles are declared per client, and the rule is narrow: **declare only roles the
application actually consumes from the token.** An application that does not read
roles gets an empty list rather than a speculative `admin`/`user` pair, because an
unread role is a claim nobody checks and a permission nobody has. The per-application
set is established during implementation by reading each application's own role
handling; `roles: [admin, user]` above is Gitea's illustrative case, not a template.

### 4. Realm SMTP through the Proton Mail Bridge

The realm currently points at `mailrise.auth.svc.cluster.local`, which forwards to
Gotify — fine for a single operator, useless for the other people who now need
self-service password reset.

The bridge already runs in `paperless` and works for IMAP. Four things are needed:

1. **Publish SMTP.** The bridge pod already republishes SMTP on `:25` via its
   entrypoint socat; the Service publishes IMAP `143` only. Add port 25.
2. **A socat sidecar in the Keycloak pod.** The bridge's certificate carries
   `CN=127.0.0.1` and exactly one SAN, `IP:127.0.0.1` (valid to 2046, generated inside
   the bridge's encrypted vault and therefore not reissuable with better names). Any
   client that verifies the hostname of `protonmail-bridge.paperless.svc.cluster.local`
   fails. This is the same wall Paperless hit, and the same fix applies: a socat
   sidecar listening on `127.0.0.1:1025` and relaying to the bridge Service as plain
   TCP, so STARTTLS still negotiates end to end and Keycloak dials an address the
   certificate matches. The operator has no sidecar field, so the sidecar goes in
   `Keycloak.spec.unsupported.podTemplate`.
3. **Trust the certificate.** `paperless/protonmail-bridge-cert` (key `cert.pem`) is
   mirrored into `keycloak` by Reflector and referenced from
   `Keycloak.spec.truststores`, which the CRD supports natively.
4. **Bridge SMTP credentials** as a SealedSecret in `keycloak`. The password is
   generated by the bridge and read from the running pod's CLI, not invented here.

The realm's `smtpServer` then targets host `127.0.0.1`, port `1025`, `starttls: true`,
`auth: true`.

A simpler alternative exists and is rejected: plain SMTP with no TLS over the internal
VLAN, consistent with the cleartext Postgres convention. It is rejected because it
would send the bridge password — which unlocks a real mailbox — in clear, and because
the Paperless integration already established the socat-plus-truststore pattern for
this exact certificate.

### 5. Cutover, in one commit

Every application's issuer becomes `https://sso.blackcats.cc/realms/homelab`.

Two applications embed the provider's name in their callback path, so their redirect
URI changes and must match the CR exactly:

- **Gitea** — `/user/oauth2/Zitadel/callback` becomes `/user/oauth2/Keycloak/callback`.
  The segment is case-sensitive.
- **Paperless** — django-allauth's `provider_id` moves from `zitadel` to `keycloak`,
  which rewrites `/accounts/oidc/<provider_id>/login/callback/`.

The same commit deletes the nine `zitadel_application_oidc` resources and their
`kubernetes_secret_v1` counterparts from
`kubernetes/apps/auth/bootstrap/app/tofu/main.tf`. This is not optional and cannot be
deferred: the Zitadel bootstrap Job self-heals hourly and would otherwise overwrite all
nine `*-oidc-secret` Secrets that SealedSecrets now owns. `main.tf` keeps the Joplin
SAML application, its action and its trigger.

Applications carrying `reloader.stakater.com/auto` pick the new credentials up on their
own; the rest are restarted by hand as part of the cutover.

### 6. Rollback

Revert the commit. Zitadel is untouched throughout and still holds all nine clients, so
the next hourly bootstrap run rewrites the original Secrets. The Keycloak client CRs
are additive and harmless if left in place. Nothing in this migration destroys state
that the rollback needs — the one irreversible act is the realm rebuild in step 1, and
it happens while the realm is empty.

### 7. Account re-linking

The `sub` claim is issued by the identity provider, so every account link breaks. Each
of the two to five people re-links in each of the nine applications. Where an
application can auto-link on a verified email address, enabling that for the cutover
window removes most of the work; that setting is enumerated per application during
implementation. This cost is unavoidable and is not specific to a big-bang cutover —
it bit the Immich migration already.

## Testing

- `.agents/scripts/validate-manifests.sh` over every changed path.
- Each client reachable in Keycloak with the expected `clientId` and roles.
- The discovery document at `https://sso.blackcats.cc/realms/homelab/.well-known/openid-configuration`
  advertises `https://` throughout.
- A password-reset mail actually arrives in a real mailbox.
- One genuine interactive login per application, all nine.
- A client role present in a decoded access token for at least one application.

## Follow-ups this creates

- Retire Zitadel once Joplin is deleted: its HelmRelease, database, managed role,
  bootstrap Job, RBAC, Terraform and DNS.
- `design/decisions/keycloak.md` and a row in `design/docs/services.md` — still owed
  from the deployment design.
- Correct the Gatus and Goldilocks rows in `design/docs/services.md`.
