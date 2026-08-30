terraform {
  required_providers {
    zitadel = {
      source  = "zitadel/zitadel"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
  }
  backend "kubernetes" {
    secret_suffix     = "zitadel-bootstrap"
    namespace         = "auth"
    in_cluster_config = true
  }
}

provider "zitadel" {
  domain       = "zitadel.blackcats.cc"
  port         = "8080"
  insecure     = true
  access_token = var.zitadel_pat
}

provider "kubernetes" {}

variable "zitadel_pat" {
  sensitive = true
}

data "zitadel_orgs" "default" {
  name = "Homelab"
}

locals {
  org_id = tolist(data.zitadel_orgs.default.ids)[0]
}

resource "zitadel_project" "homelab" {
  name   = "Homelab"
  org_id = local.org_id

  project_role_assertion = false
  project_role_check     = false
  has_project_check      = false
}

resource "zitadel_application_oidc" "immich" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Immich"

  redirect_uris = [
    "https://immich.blackcats.cc/auth/login",
    "app.immich:///oauth-callback",
  ]
  post_logout_redirect_uris = [
    "https://immich.blackcats.cc",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_POST"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

resource "zitadel_application_oidc" "freshrss" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "FreshRSS"

  redirect_uris = [
    "https://rss.blackcats.cc/i/oidc/",
  ]
  post_logout_redirect_uris = [
    "https://rss.blackcats.cc",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

resource "kubernetes_secret_v1" "freshrss_oidc_secret" {
  metadata {
    name      = "freshrss-oidc-secret"
    namespace = "freshrss"
  }
  data = {
    OIDC_CLIENT_ID     = zitadel_application_oidc.freshrss.client_id
    OIDC_CLIENT_SECRET = zitadel_application_oidc.freshrss.client_secret
  }
}


resource "zitadel_application_oidc" "paperless" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Paperless"

  redirect_uris = [
    "https://paperless.blackcats.cc/accounts/oidc/zitadel/login/callback/",
  ]
  post_logout_redirect_uris = [
    "https://paperless.blackcats.cc",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_POST"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

resource "kubernetes_secret_v1" "paperless_oidc_secret" {
  metadata {
    name      = "paperless-oidc-secret"
    namespace = "paperless"
  }
  data = {
    PAPERLESS_SOCIALACCOUNT_PROVIDERS = jsonencode({
      openid_connect = {
        APPS = [{
          provider_id = "zitadel"
          name        = "Zitadel"
          client_id   = zitadel_application_oidc.paperless.client_id
          secret      = zitadel_application_oidc.paperless.client_secret
          settings = {
            server_url = "https://zitadel.blackcats.cc"
            # Must match auth_method_type above (OIDC_AUTH_METHOD_TYPE_POST).
            # Left unset, allauth infers it from Zitadel's discovery document,
            # and the inference rule changed in allauth 65.16 (paperless v3):
            # 65.12 picked basic whenever advertised, 65.16 prefers post. Pinning
            # removes the dependency on both the discovery doc and allauth internals.
            token_auth_method = "client_secret_post"
          }
        }]
      }
    })
  }
}

resource "zitadel_application_oidc" "gitea" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Gitea"

  redirect_uris = [
    "https://gitea.blackcats.cc/user/oauth2/Zitadel/callback",
  ]
  post_logout_redirect_uris = [
    "https://gitea.blackcats.cc",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_POST"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

resource "kubernetes_secret_v1" "gitea_oidc_secret" {
  metadata {
    name      = "gitea-oidc-secret"
    namespace = "gitea"
  }
  data = {
    "values.yaml" = yamlencode({
      gitea = {
        oauth = [{
          name            = "Zitadel"
          provider        = "openidConnect"
          key             = zitadel_application_oidc.gitea.client_id
          secret          = zitadel_application_oidc.gitea.client_secret
          autoDiscoverUrl = "https://zitadel.blackcats.cc/.well-known/openid-configuration"
          scopes          = "openid email profile"
        }]
      }
    })
  }
}

resource "zitadel_application_oidc" "grafana" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Grafana"

  redirect_uris = [
    "https://grafana.blackcats.cc/login/generic_oauth",
  ]
  post_logout_redirect_uris = [
    "https://grafana.blackcats.cc",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

resource "kubernetes_secret_v1" "grafana_oidc_secret" {
  metadata {
    name      = "grafana-oidc-secret"
    namespace = "monitoring"
  }
  data = {
    GF_AUTH_GENERIC_OAUTH_CLIENT_ID     = zitadel_application_oidc.grafana.client_id
    GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET = zitadel_application_oidc.grafana.client_secret
  }
}

resource "zitadel_application_oidc" "kavita" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Kavita"

  redirect_uris = [
    "https://kavita.blackcats.cc/signin-oidc",
  ]
  post_logout_redirect_uris = [
    "https://kavita.blackcats.cc/login",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_POST"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

# Kavita reads OIDC creds only from /config/appsettings.json (key OpenIdConnectSettings),
# which it manages itself. The Kavita HelmRelease runs an initContainer that merges these
# flat values into that file. Authority is set statically in the HelmRelease.
resource "kubernetes_secret_v1" "kavita_oidc_secret" {
  metadata {
    name      = "kavita-oidc-secret"
    namespace = "media"
  }
  data = {
    OIDC_CLIENT_ID     = zitadel_application_oidc.kavita.client_id
    OIDC_CLIENT_SECRET = zitadel_application_oidc.kavita.client_secret
  }
}

resource "zitadel_application_oidc" "romm" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "RomM"

  redirect_uris = [
    "https://romm.blackcats.cc/api/oauth/openid",
  ]
  post_logout_redirect_uris = [
    "https://romm.blackcats.cc/",
  ]

  response_types = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types    = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type       = "OIDC_APP_TYPE_WEB"
  # RomM's OIDC guide specifies HTTP Basic (client_secret_basic).
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"

  access_token_type = "OIDC_TOKEN_TYPE_BEARER"
  # "User Info inside ID Token" — RomM resolves the email claim from the ID token.
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

# RomM reads every OIDC_* var from the environment. The HelmRelease consumes this
# Secret via an optional envFrom, so its mere presence enables OIDC (and its
# absence on a fresh cluster leaves RomM on local-account auth). Reloader restarts
# RomM when this Secret is created/rotated.
resource "kubernetes_secret_v1" "romm_oidc_secret" {
  metadata {
    name      = "romm-oidc-secret"
    namespace = "media"
  }
  data = {
    OIDC_ENABLED                = "true"
    OIDC_PROVIDER               = "Zitadel"
    OIDC_CLIENT_ID              = zitadel_application_oidc.romm.client_id
    OIDC_CLIENT_SECRET          = zitadel_application_oidc.romm.client_secret
    OIDC_REDIRECT_URI           = "https://romm.blackcats.cc/api/oauth/openid"
    OIDC_SERVER_APPLICATION_URL = "https://zitadel.blackcats.cc"
  }
}

resource "zitadel_application_oidc" "proxmox" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Proxmox VE"

  # Proxmox uses the web UI base URL (no path) as the OIDC redirect target.
  # Register both :8006 (default) and :443 so login works whether or not a
  # host-level 443->8006 redirect is in place. Proxmox lives outside the
  # cluster (172.16.20.3) — do NOT front it behind the k8s Gateway (circular
  # dependency: the Gateway runs on the VMs this host hypervises).
  redirect_uris = [
    "https://pve.blackcats.cc:8006",
    "https://pve.blackcats.cc",
  ]
  post_logout_redirect_uris = [
    "https://pve.blackcats.cc:8006",
    "https://pve.blackcats.cc",
  ]

  response_types = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types    = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type       = "OIDC_APP_TYPE_WEB"
  # proxmox-openid (Rust openidconnect crate) authenticates at the token
  # endpoint with HTTP Basic (client_secret_basic) by default.
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_BASIC"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

# Proxmox is bare metal, not a k8s workload — nothing in-cluster consumes this.
# Written to the auth namespace purely as a retrieval mechanism; copy the values
# into the Proxmox OpenID Connect realm (see design/RUNBOOK.md):
#   kubectl get secret proxmox-oidc-secret -n auth -o jsonpath='{.data.OIDC_CLIENT_SECRET}' | base64 -d
resource "kubernetes_secret_v1" "proxmox_oidc_secret" {
  metadata {
    name      = "proxmox-oidc-secret"
    namespace = "auth"
  }
  data = {
    ISSUER_URL         = "https://zitadel.blackcats.cc"
    OIDC_CLIENT_ID     = zitadel_application_oidc.proxmox.client_id
    OIDC_CLIENT_SECRET = zitadel_application_oidc.proxmox.client_secret
  }
}

# Joplin Server speaks SAML, not OIDC (upstream issue #14252 — OIDC is still an
# open feature request). The SP metadata below must stay byte-identical to
# kubernetes/apps/joplin/joplin/app/saml-sp-configmap.yml: Joplin serves that
# same document to samlify, and a mismatch in entityID or ACS Location makes
# Zitadel's assertion fail the audience check.
#
# No client secret exists for a SAML SP, so unlike every OIDC app here there is
# nothing to write back into a Kubernetes Secret — Joplin only needs the public
# IdP metadata, which it fetches from Zitadel at pod start.
resource "zitadel_application_saml" "joplin" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Joplin"

  metadata_xml = <<-EOT
    <?xml version="1.0"?>
    <md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://joplin.blackcats.cc">
      <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="false" protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
        <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
        <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
                                     Location="https://joplin.blackcats.cc/api/saml"
                                     index="1" />
      </md:SPSSODescriptor>
    </md:EntityDescriptor>
  EOT
}

# Zitadel emits SAML attributes named Email / FullName / FirstName / SurName /
# UserName / UserID. Joplin looks up exactly `email` and `displayName`
# (packages/server/src/routes/api/login.ts) and throws ErrorBadRequest when
# either is missing — so without this action every SSO login fails with
# "email must be a string". setCustomAttribute only adds keys that aren't
# already present, so the stock attributes are left untouched.
#
# allowed_to_fail = false: a silent failure here would degrade to that same
# opaque 400, so fail the login loudly instead.
resource "zitadel_action" "joplin_saml_attributes" {
  org_id          = local.org_id
  name            = "joplinSamlAttributes"
  timeout         = "10s"
  allowed_to_fail = false

  script = <<-EOT
    function joplinSamlAttributes(ctx, api) {
      const user = ctx.v1.getUser();
      if (!user || !user.human) {
        return;
      }

      // Do NOT type-check these with `typeof x === 'string'`. Zitadel's action
      // user object (internal/actions/object/user.go) types DisplayName as a
      // plain Go `string` but Email as `domain.EmailAddress` — a named string
      // type, which goja does not surface as a JS string primitive. A typeof
      // check therefore passes for displayName and silently drops email from
      // the assertion, and Joplin 3.7.1 reports that as the opaque
      // 'Could not login using email "undefined"'. Coerce instead.
      function text(value) {
        if (value === null || value === undefined) return '';
        const s = String(value);
        return (s === 'undefined' || s === 'null' || s === '[object Object]') ? '' : s;
      }

      const email = text(user.human.email);
      const displayName = text(user.human.displayName)
        || [text(user.human.firstName), text(user.human.lastName)].filter(Boolean).join(' ');

      if (email) {
        api.v1.attributes.setCustomAttribute('email', '', email);
      }
      if (displayName) {
        api.v1.attributes.setCustomAttribute('displayName', '', displayName);
      }
    }
  EOT
}

resource "zitadel_trigger_actions" "joplin_saml_attributes" {
  org_id       = local.org_id
  flow_type    = "FLOW_TYPE_SAML_RESPONSE"
  trigger_type = "TRIGGER_TYPE_PRE_SAML_RESPONSE_CREATION"
  action_ids   = [zitadel_action.joplin_saml_attributes.id]
}

resource "kubernetes_secret_v1" "immich_oidc_config" {
  metadata {
    name      = "immich-oidc-config"
    namespace = "immich"
  }
  data = {
    "immich.json" = jsonencode({
      oauth = {
        enabled               = true
        issuerUrl             = "https://zitadel.blackcats.cc"
        clientId              = zitadel_application_oidc.immich.client_id
        clientSecret          = zitadel_application_oidc.immich.client_secret
        buttonText            = "Login with SSO"
        autoRegister          = true
        mobileOverrideEnabled = true
        mobileRedirectUri     = "https://immich.blackcats.cc/api/oauth/mobile-redirect"
        scope                 = "openid email profile"
        signingAlgorithm      = "RS256"
      }
      passwordLogin = {
        enabled = false
      }
    })
  }
}

# Open WebUI reads every OAUTH_*/OPENID_* value from the environment, so this is
# the flat env-var style (same as Kavita/RomM), consumed via `envFrom: secretRef`
# in the HelmRelease.
#
# POST rather than BASIC deliberately: the HelmRelease pins
# OAUTH_TOKEN_ENDPOINT_AUTH_METHOD=client_secret_post to match. Zitadel advertises
# both methods in its discovery document, so leaving the client to infer one puts
# the choice at the mercy of an upstream default — which is precisely how the
# paperless 2.20.15 -> 3.0.4 bump silently flipped basic -> post.
resource "zitadel_application_oidc" "openwebui" {
  project_id = zitadel_project.homelab.id
  org_id     = local.org_id
  name       = "Open WebUI"

  redirect_uris = [
    "https://chat.blackcats.cc/oauth/oidc/callback",
  ]
  post_logout_redirect_uris = [
    "https://chat.blackcats.cc/",
  ]

  response_types   = ["OIDC_RESPONSE_TYPE_CODE"]
  grant_types      = ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"]
  app_type         = "OIDC_APP_TYPE_WEB"
  auth_method_type = "OIDC_AUTH_METHOD_TYPE_POST"

  access_token_type           = "OIDC_TOKEN_TYPE_BEARER"
  id_token_userinfo_assertion = true

  version  = "OIDC_VERSION_1_0"
  dev_mode = false
}

resource "kubernetes_secret_v1" "openwebui_oidc_secret" {
  metadata {
    name      = "openwebui-oidc-secret"
    namespace = "ai"
  }
  data = {
    OAUTH_CLIENT_ID     = zitadel_application_oidc.openwebui.client_id
    OAUTH_CLIENT_SECRET = zitadel_application_oidc.openwebui.client_secret
  }
}
