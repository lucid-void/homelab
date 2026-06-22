terraform {
  required_providers {
    zitadel = {
      source  = "zitadel/zitadel"
      version = "~> 2.0"
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
