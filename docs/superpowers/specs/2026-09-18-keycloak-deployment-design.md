# Keycloak alongside Zitadel — deployment design

Status: implemented 2026-09-18. Platform only; no application is wired to Keycloak.

## Goal

Stand up Keycloak as a second identity provider so that Zitadel can be replaced
later, one application at a time. The driver is Keycloak's client-level roles: the
same user needs `admin` in Gitea and `user` in Immich, which Zitadel's project-level
role model does not express without custom trigger actions.

End state chosen: **full replacement, staged.** Keycloak eventually becomes the only
IdP and Zitadel is retired. This deploy is step one.

## Scope

In scope: Keycloak running, healthy, on its own database, reachable over HTTPS, with
the realm provisioned declaratively.

Out of scope, deliberately: no application moved, no OIDC client registered, Zitadel
untouched and still serving all ten registered applications.

## Decisions

**Vehicle — the official Keycloak Operator, from a tag-pinned GitRepository.**
Considered and rejected: the Bitnami chart (images moved to `bitnamilegacy` on
2025-08-28 and Keycloak was excluded from Bitnami Secure, so the chart no longer
resolves); a community chart such as codecentric keycloakx (a third-party dependency
for the component that gates all authentication — the same class of bet that just
failed with Bitnami); `bjw-s/app-template` with the upstream image (viable and
cheaper, but hands every Keycloak-specific sharp edge to us by hand, including
probes, hostname semantics, proxy headers and upgrade-time schema migration).

The operator is installed from `github.com/keycloak/keycloak-k8s-resources` at a
pinned tag, filtered to `/kubernetes`. This reuses the pattern already proven for the
Gateway API CRD bundle.

**Namespace — `keycloak`, not `auth`.** The upstream kustomization hardcodes
`namespace: keycloak` and runs a `NamespaceTransformer` with
`setRoleBindingSubjects: allServiceAccounts`. Installing into that namespace means
nothing is rewritten and the operator's RBAC subjects resolve correctly. Consequently
the operator's Flux Kustomization must **not** set `targetNamespace`. This also
satisfies the repo convention that operators live in their own namespace.

**Database — the shared CNPG cluster.** A `keycloak` `Database` CR owned by a
`keycloak` managed role, with the role password as a SealedSecret in `postgres`
mirrored into `keycloak` by Reflector. Identical in shape to Zitadel's. The database
is added to the `postgres-backup-databases` list.

**Hostname — `sso.blackcats.cc`.** Product-neutral on purpose: under a full
replacement this ends up in the callback URI of every application, and a
product-named host would have to be rewritten again if Keycloak is ever replaced.

**Realm provisioning — `KeycloakRealmImport` CR.** Pure GitOps, no Job and no second
OpenTofu provider lockfile to keep in sync. The cost is that import is not a
reconciling operation: it creates the realm when absent but does not correct drift on
an existing one.

**Client provisioning — deferred, deliberately.** The operator now ships
`KeycloakOIDCClient` and `KeycloakSAMLClient` CRDs. `KeycloakOIDCClient` has
`client.roles` (the per-application roles that motivated this work) and
`client.auth.secretRef`, which lets the client secret come from a SealedSecret we own
rather than being generated and ferried between namespaces by Terraform — a better
fit than the Zitadel arrangement. It is `v2alpha1`, so the choice between it and
OpenTofu is made at first-app migration, when there is something real to test it
against. Client CRs are additive and namespaced to `keycloak`, so deferring costs no
rework.

## Sizing

The operator defaults to requests 1700Mi and limits 2Gi per instance. Both are
overridden to 768Mi / 1536Mi. Measured headroom at the time of writing: control-plane
memory *requests* were at 44–47% of allocatable (~15Gi free per node), while *limits*
were already overcommitted at 91–127%. Requests are therefore not the constraint;
accepting the operator's default request would have wasted ~1Gi per node for nothing.

Re-derive from a 30-day window once it has run, per the platform sizing convention,
pinning `metrics_path="/metrics/cadvisor"`.

## Gotchas worth keeping

- **A Renovate bump of the `keycloak-k8s-resources` tag is a Keycloak upgrade, not an
  operator bump.** The `Keycloak` CR carries no image field, so the operator deploys
  the server version it ships with, and the rollout migrates the database schema. A
  `packageRules` entry labels these for manual review at every update type.
- **`hostname.hostname` is set to the full URL**, not a bare host. TLS terminates at
  the Gateway, so without the scheme Keycloak advertises `http://` in its OIDC
  discovery document and every `redirect_uri` breaks.
- **Health probes are on management port 9000**, not 8080. The operator wires them;
  this matters when hand-debugging or if the vehicle is ever changed.
- **`db-url-properties: "?sslmode=disable"`** matches the cluster convention of
  cleartext Postgres on the internal VLAN. Without it the JDBC driver defaults to
  `sslmode=prefer` and negotiates TLS it does not verify.
- **The Service is `keycloak-service`** (`<cr-name>-service`), which is what the
  HTTPRoute targets.
- **The initial admin is generated**, into a `keycloak-initial-admin` Secret on first
  boot against an empty database — it is not in git. `spec.bootstrapAdmin` could point
  at a SealedSecret instead if a known-in-advance admin credential is wanted for
  disaster recovery; not done here.

## Migration path from here

1. Pick the pilot (Gitea — it keeps a local admin, so a mistake does not lock you out).
2. Decide `KeycloakOIDCClient` vs OpenTofu against that one app.
3. Re-register the app, re-link the account, verify client roles land in the token.
4. Repeat per app. `design/decisions/oidc-apps.md` and the per-app decision files hold
   the callback URIs and auth-method pins that each migration rewrites.
5. Retire Zitadel only once every app is moved.

**The `sub` claim changes for every application when its IdP changes**, so per-app
account re-linking is a cost of every route — brokering to Zitadel would not avoid it,
since applications see Keycloak's `sub`. This bit during the Immich migration already.

## Follow-up

`design/decisions/keycloak.md` is the proper permanent home for the gotcha list above,
and `design/docs/services.md` needs a row. Both are blocked: a concurrent session owns
`design/` until its restructure lands.
