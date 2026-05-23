terraform {
  required_providers {
    zitadel = {
      source  = "zitadel/zitadel"
      version = "~> 2.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
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
    "https://paperless.blackcats.cc/accounts/zitadel/login/callback/",
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
