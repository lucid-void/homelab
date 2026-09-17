# FreshRSS and Paperless OIDC

**Read before editing:** `kubernetes/apps/freshrss/`, `kubernetes/apps/paperless/paperless/`

## Current state

**FreshRSS** uses Apache mod_auth_openidc (`OIDC_ENABLED=1`). Redirect URI is
`https://rss.blackcats.cc/i/oidc/` — not `/i/?get=oidc`, which is the old PHP lib path.
Required env vars: `OIDC_CLIENT_CRYPTO_KEY` (session passphrase),
`OIDC_REMOTE_USER_CLAIM`, `OIDC_X_FORWARDED_HEADERS` (uses real header names like
`X-Forwarded-Host`, not PHP var names). Client/secret come from `freshrss-oidc-secret`,
written by Terraform bootstrap.

**Paperless** goes via django-allauth 65.x: `PAPERLESS_SOCIALACCOUNT_PROVIDERS` JSON env
var with `openid_connect.APPS[].provider_id = "zitadel"`. Callback URI:
`https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/` — allauth 65.x's
path shape is `/accounts/oidc/<provider_id>/`, not `/accounts/<provider_id>/`. Must also
set `PAPERLESS_APPS: "allauth.socialaccount.providers.openid_connect"`, or the provider
is never registered in `INSTALLED_APPS` and the login button never appears. Also needs
`PAPERLESS_ACCOUNT_DEFAULT_HTTP_PROTOCOL=https` and
`PAPERLESS_ACCOUNT_EMAIL_VERIFICATION=none`. Config is written by Terraform into
`paperless-oidc-secret`.

`settings.token_auth_method` is pinned to `client_secret_post`, matching the Zitadel
app's `auth_method_type = OIDC_AUTH_METHOD_TYPE_POST`. Left unset, allauth infers it
from Zitadel's discovery document — and that inference rule changed in allauth `65.16`
(shipped in paperless v3.0): the prior version returned `client_secret_basic` whenever
it was advertised; `65.16` instead returns
`"client_secret_post" not in methods and "client_secret_basic" in methods`. Zitadel
advertises both, so on `65.16` the discovery-based inference now resolves to
`client_secret_post` instead of `client_secret_basic`.

## Rules

- **Pin `token_auth_method` to whatever the Zitadel app is actually registered as, never
  to what a migration guide recommends** — allauth's generic upgrade advice is to pin
  `client_secret_basic`, but that would break this setup, where the app is registered
  `OIDC_AUTH_METHOD_TYPE_POST`. The `65.16` inference-rule change is exactly what makes
  this non-obvious: an unset value can silently flip which method gets used on an
  otherwise-unrelated paperless upgrade.
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
