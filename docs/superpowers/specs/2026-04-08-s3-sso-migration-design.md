# S3 Retirement & SSO Migration Design

**Date:** 2026-04-08
**Status:** Approved
**Topics:** OpenTofu S3 → PostgreSQL backend; Authentik → Zitadel

---

## Overview

Two independent migrations driven by the same theme: reduce unnecessary services and replace what isn't working well.

1. **S3 retirement** — TrueNAS's built-in MinIO app is being discontinued in newer SCALE versions. The two S3 consumers are reactive-resume (dispensable) and OpenTofu state (critical). OpenTofu has a native PostgreSQL backend; since Postgres is already deployed and backed up on TrueNAS, MinIO can be retired entirely with no new infrastructure.

2. **SSO migration** — Authentik's UI and documentation are poor. Zitadel is a Go-based identity provider with a cleaner console, better docs, and PostgreSQL as its only dependency. Authelia stays in place as the Traefik forward-auth middleware; it is reconfigured to use Zitadel as its OIDC backend instead of Authentik. The two-tier auth architecture (Zitadel as IdP, Authelia as proxy) is unchanged.

---

## Part 1 — S3 Retirement

### What changes

| Item | Current | After |
|---|---|---|
| OpenTofu state | S3 bucket on TrueNAS MinIO | PostgreSQL database `tofu_state` on TrueNAS |
| reactive-resume | Swarm service, uses S3 | Removed from stack |
| MinIO app | Built-in TrueNAS app | Retired |
| `tank/s3/` dataset | 100 GB quota, MinIO data | Retired (destroy after migration) |
| `terraform-state` S3 bucket | OpenTofu backend | No longer needed |

### OpenTofu backend change

`infra/terraform/backend.tf` switches from S3 to PostgreSQL:

```hcl
terraform {
  backend "pg" {
    conn_str = "postgres://tofu@172.16.20.2/tofu_state"
  }
}
```

State locking uses PostgreSQL advisory locks — no DynamoDB equivalent needed. The connection string password is injected via the `PG_CONN_STR` environment variable at runtime (SOPS-managed, same pattern as other DB credentials).

### Database provisioning

A new `tofu_state` database and `tofu` user are created on TrueNAS Postgres by Ansible. OpenTofu initialises the schema on first `tofu init`. No manual schema management required.

### TrueNAS snapshot schedule update

Remove the daily snapshot task for `tank/s3/` from TrueNAS when the dataset is retired. This is a TrueNAS UI change (or Ansible task) — leaving the snapshot task pointing at a destroyed dataset causes TrueNAS alerts.

### State migration

Before retiring the S3 backend:
1. `tofu state pull > state-backup.json` (local backup)
2. Update `backend.tf` to `pg`
3. `tofu init -migrate-state` — OpenTofu prompts to copy state from S3 to pg
4. Verify: `tofu state list`
5. Decommission MinIO app and `tank/s3/` dataset

### Backup script update

Two changes to the daily backup script:

**Add** `tofu_state` to the pg_dump step:
```bash
pg_dump -h 127.0.0.1 tofu_state > "${DUMP_DIR}/${DATE}-tofu_state.sql"
```
This is backed up offsite to Filen alongside the other database dumps with the same 30-day retention.

**Remove** step 4 (explicit `tofu state pull` via SSH). With the pg backend, state lives in Postgres and is fully captured by `pg_dump tofu_state` above. The SSH call to Services VM to run `tofu state pull` becomes redundant and should be deleted from the script.

### CLAUDE.md key decisions to update

- Remove: SeaweedFS decision (already gone) → update reactive_resume entry to reflect stack removal
- Remove: MinIO TLS hostname entry
- Update: Tofu state entry from MinIO S3 to PostgreSQL

---

## Part 2 — SSO Migration (Authentik → Zitadel)

### Architecture — unchanged

The two-tier model stays:

```
Apps with native OIDC  →  Zitadel directly
Apps without OIDC      →  Traefik → Authelia (forward auth) → Zitadel (OIDC)
```

Zitadel has no built-in proxy/outpost, so Authelia remains for apps that can't do OIDC natively. Authelia's config changes minimally — its OIDC provider URL and client credentials point to Zitadel instead of Authentik.

### Components

| Component | Current | After |
|---|---|---|
| Identity provider | Authentik server + worker | Zitadel (single container) |
| IdP cache | authentik-valkey (dedicated) | **None** — Zitadel needs only PostgreSQL |
| IdP database | `authentik` DB on TrueNAS Postgres | `zitadel` DB on TrueNAS Postgres |
| IdP local volume | Authentik media (branding assets) | **None** — Zitadel has no local volume |
| Forward auth proxy | Authelia | Authelia (unchanged) |
| Authelia session store | authelia-valkey | authelia-valkey (unchanged) |
| Authelia OIDC backend | Authentik | Zitadel |
| Domain | `authentik.blackcats.cc` | `zitadel.blackcats.cc` |

Zitadel is simpler: one container, one database, no sidecar cache. The `auth` overlay simplifies from 5 services to 3 (Zitadel, Authelia, authelia-valkey).

### Zitadel configuration

Zitadel requires `ZITADEL_EXTERNALDOMAIN` and `ZITADEL_EXTERNALPORT` to match the public URL:

```yaml
environment:
  ZITADEL_EXTERNALDOMAIN: zitadel.blackcats.cc
  ZITADEL_EXTERNALPORT: "443"
  ZITADEL_EXTERNALDOMAINSTRICTMODE: "true"
  ZITADEL_DATABASE_POSTGRES_HOST: "172.16.20.2"
  ZITADEL_DATABASE_POSTGRES_PORT: "5432"
  ZITADEL_DATABASE_POSTGRES_DATABASE: "zitadel"
  ZITADEL_DATABASE_POSTGRES_USER_USERNAME: "zitadel"
  ZITADEL_DATABASE_POSTGRES_USER_PASSWORD: "<sops-managed>"
  ZITADEL_DATABASE_POSTGRES_ADMIN_USERNAME: "postgres"
  ZITADEL_DATABASE_POSTGRES_ADMIN_PASSWORD: "<sops-managed>"
  ZITADEL_MASTERKEY: "<32-char-sops-managed>"
```

Zitadel runs its own schema migrations on startup — no manual SQL needed.

### OIDC clients to reconfigure

All existing Authentik OIDC integrations must be recreated in Zitadel:

| App | Auth method | Action |
|---|---|---|
| Grafana | Native OIDC | Create Zitadel application, update Grafana env vars |
| Immich | Native OIDC | Create Zitadel application, update Immich env vars |
| Proxmox | Native OIDC | Create Zitadel application, update PVE realm config |
| TrueNAS | Native OIDC (if configured) | Create Zitadel application, update TrueNAS OIDC config |
| Authelia | OIDC client | Create Zitadel application, update Authelia `identity_providers.oidc` config |

### Authelia config change

Only the OIDC provider block changes:

```yaml
identity_providers:
  oidc:
    clients:
      - client_id: authelia
        client_secret: <sops-managed>
        issuer_url: https://zitadel.blackcats.cc  # was: authentik.blackcats.cc
```

Everything else in Authelia (access control rules, session config, regulation) is unchanged.

### Zitadel data model note

Zitadel's console uses "instances → organizations → projects" terminology. For a single-user homelab, use the default instance and a single organization. All OIDC applications live under one project (e.g., "Homelab"). This is more structured than Authentik but logical once understood.

### Backup script update

Replace `authentik` with `zitadel` in the daily pg_dump step:

```bash
# Remove:
pg_dump -h 127.0.0.1 authentik   > "${DUMP_DIR}/${DATE}-authentik.sql"
# Add:
pg_dump -h 127.0.0.1 zitadel     > "${DUMP_DIR}/${DATE}-zitadel.sql"
```

### Migration sequence

1. Deploy Zitadel, configure domain, verify console is reachable
2. Create all OIDC applications in Zitadel (Grafana, Immich, Proxmox, Authelia)
3. Create the single admin user (migrate manually — no user import from Authentik)
4. Update Authelia config → point at Zitadel → redeploy Authelia
5. Update each native OIDC app (Grafana, Immich, Proxmox) one at a time — verify login works
6. Verify Authelia forward auth works end-to-end
7. Decommission Authentik stack (server, worker, authentik-valkey)
8. Drop `authentik` database (after confirming everything works — keep dump as archive)
9. Update DNS: retire `authentik.blackcats.cc`

### CLAUDE.md key decisions to update

- SSO provider: `Zitadel` (was Authentik)
- SSO forward auth: Authelia as Traefik middleware; authenticates against **Zitadel** via OIDC
- SSO placement: **Zitadel** + Authelia both on Services VM (.13); dedicated Valkey per Authelia only
- Authentik data: remove entry (no longer relevant)
- Remove: Authentik SECRET_KEY from credential rotation runbook; add Zitadel MASTERKEY
- Remove: Valkey passwords ×2 entry → update to ×1 (authelia-valkey only)

---

## What is NOT changing

- TrueNAS remains a separate physical host (DXP4800 at 172.16.20.2) — not virtualizing
- Authelia stays in the stack as Traefik forward-auth middleware
- All other Swarm services unchanged
- Backup strategy, ZFS datasets, NFS exports unchanged (minus `tank/s3/`)
- SOPS/age key management unchanged
