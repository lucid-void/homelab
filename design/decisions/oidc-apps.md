# FreshRSS and Paperless OIDC

**Read before editing:** `kubernetes/apps/freshrss/`, `kubernetes/apps/paperless/paperless/`

## Current state

**FreshRSS** uses Apache mod_auth_openidc (`OIDC_ENABLED=1`). Redirect URI is
`https://rss.blackcats.cc/i/oidc/` — not `/i/?get=oidc`, which is the old PHP lib path.
Required env vars: `OIDC_CLIENT_CRYPTO_KEY` (session passphrase),
`OIDC_REMOTE_USER_CLAIM`, `OIDC_X_FORWARDED_HEADERS` (uses real header names like
`X-Forwarded-Host`, not PHP var names). Client/secret come from the sealed
`freshrss-oidc-secret`.

**Paperless** goes via django-allauth 65.x: `PAPERLESS_SOCIALACCOUNT_PROVIDERS` JSON env
var with `openid_connect.APPS[].provider_id = "keycloak"`. Callback URI:
`https://paperless.blackcats.cc/accounts/oidc/keycloak/login/callback/` — allauth 65.x's
path shape is `/accounts/oidc/<provider_id>/`, not `/accounts/<provider_id>/`. Must also
set `PAPERLESS_APPS: "allauth.socialaccount.providers.openid_connect"`, or the provider
is never registered in `INSTALLED_APPS` and the login button never appears. Also needs
`PAPERLESS_ACCOUNT_DEFAULT_HTTP_PROTOCOL=https` and
`PAPERLESS_ACCOUNT_EMAIL_VERIFICATION=none`. Config lives in the sealed
`paperless-oidc-secret`.

**`provider_id` is part of the callback URI**, so changing it changes the URI that must
be registered on the IdP side, and it also keys the `socialaccount_socialaccount` rows
already in the database — existing links do not follow a rename.

`settings.token_auth_method` is pinned to `client_secret_post`. Left unset, allauth
infers it from the discovery document — and that inference rule changed in allauth
`65.16` (shipped in paperless v3.0): the prior version returned `client_secret_basic`
whenever it was advertised; `65.16` instead returns
`"client_secret_post" not in methods and "client_secret_basic" in methods`. Keycloak,
like Zitadel before it, advertises both, so the inferred value differs across that
version boundary — which is why it is pinned rather than left to inference.

## Rules

- **Pin `token_auth_method` to whatever the client is actually registered as, never to
  what a migration guide recommends** — allauth's generic upgrade advice is to pin
  `client_secret_basic`, which would break this setup. The `65.16` inference-rule change
  is exactly what makes this non-obvious: an unset value can silently flip which method
  gets used on an otherwise-unrelated paperless upgrade.
- **Always set `PAPERLESS_APPS` alongside `PAPERLESS_SOCIALACCOUNT_PROVIDERS`** — without
  it the OIDC provider class is never registered in `INSTALLED_APPS`, and the failure
  mode is silent: no error, just no login button.
- **Use FreshRSS's `/i/oidc/` redirect path, never `/i/?get=oidc`** — the latter is the
  old PHP OIDC library's path and no longer exists.

## Verify

```bash
mise exec -- kubectl get secret freshrss-oidc-secret -n freshrss -o jsonpath='{.data}' | tr ',' '\n'
mise exec -- kubectl get secret paperless-oidc-secret -n paperless -o jsonpath='{.data}' | tr ',' '\n'
```
