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

# The nine apps below were cut over to Keycloak (see keycloak-clients). Terraform
# state still held these eighteen resources, so simply deleting the blocks would
# DESTROY them on the next apply — that would delete the live Zitadel clients
# (the rollback path this cutover depends on) and the *-oidc-secret Secrets that
# SealedSecrets now owns. `removed` blocks drop them from state without touching
# the real objects. Leave these in place until Zitadel is retired entirely.

removed {
  from = zitadel_application_oidc.immich
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.freshrss
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.freshrss_oidc_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.paperless
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.paperless_oidc_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.gitea
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.gitea_oidc_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.grafana
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.grafana_oidc_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.kavita
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.kavita_oidc_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.romm
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.romm_oidc_secret
  lifecycle {
    destroy = false
  }
}

removed {
  from = zitadel_application_oidc.proxmox
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.proxmox_oidc_secret
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

removed {
  from = zitadel_application_oidc.openwebui
  lifecycle {
    destroy = false
  }
}

removed {
  from = kubernetes_secret_v1.openwebui_oidc_secret
  lifecycle {
    destroy = false
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
#
# Joplin stays on Zitadel — it is being retired soon and is deliberately out of
# scope for the Keycloak cutover.
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
