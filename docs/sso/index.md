---
tags:
  - sso
  - authentik
  - authelia
  - auth
---

# SSO

Authentik is the single identity provider — all users, credentials, 2FA, and group membership are managed there. Authelia sits in front of Traefik as a forward auth middleware for services that have no native OIDC support, authenticating back to Authentik via OIDC. There is no separate user database in Authelia.

## Auth Flows

### Native OIDC

For apps that support it (Grafana, Immich, and others determined per service):

```mermaid
sequenceDiagram
    participant U as User
    participant App as Application
    participant AK as Authentik<br/>authentik.blackcats.cc

    U->>App: Access app
    App->>AK: Redirect to login
    U->>AK: Authenticate (credentials + 2FA)
    AK-->>App: OIDC token
    App->>App: Validate token
    App-->>U: Authenticated session
```

### Traefik Forward Auth

For apps without native OIDC support:

```mermaid
sequenceDiagram
    participant U as User
    participant TF as Traefik
    participant AL as Authelia
    participant AK as Authentik

    U->>TF: Request app.blackcats.cc
    TF->>AL: Forward auth check
    AL-->>TF: 401 — not authenticated
    TF-->>U: Redirect to Authelia login
    U->>AL: Login page
    AL->>AK: OIDC redirect
    U->>AK: Authenticate (credentials + 2FA)
    AK-->>AL: OIDC callback + token
    AL-->>U: Set session cookie
    U->>TF: Retry with cookie
    TF->>AL: Forward auth check
    AL-->>TF: 200 — authenticated
    TF-->>U: Serve application
```

Single user store throughout. Whether an app uses native OIDC or forward auth is determined at service deployment time based on what the app supports.

## Components

All components run as Swarm services on Services VM (.13).

| Component | Detail |
|---|---|
| Authentik server | `authentik.blackcats.cc` via Traefik · TLS auto |
| Authentik worker | Same image, separate service · background tasks (email, flows, events) |
| authentik-valkey | Dedicated Valkey · cache + sessions · ephemeral local volume |
| Authentik DB | Shared Postgres on TrueNAS (.2) · dedicated `authentik` database |
| Authentik data | `tank/media/authentik` NFS -> `/mnt/media/authentik` · media uploads, custom assets |
| Authelia | Traefik forward auth middleware · OIDC client of Authentik · no own user DB |
| authelia-valkey | Dedicated Valkey · session storage · ephemeral local volume |
| Authelia config | YAML managed by Ansible · no persistent data volume · OIDC client secret in SOPS |

### Component Relationships

```mermaid
graph TB
    U[User] --> TF[Traefik]

    TF -->|native OIDC apps| AK[Authentik<br/>Identity Provider]
    TF -->|forward auth| AL[Authelia<br/>Middleware]
    AL -->|OIDC client| AK

    AK --> AK_V[authentik-valkey<br/>Cache + sessions]
    AK --> AK_DB[(Postgres<br/>authentik DB)]
    AK --> AK_DATA[tank/media/authentik<br/>NFS]
    AL --> AL_V[authelia-valkey<br/>Sessions]

    style AK fill:#c6a0f6,stroke:#c6a0f6,color:#1e2030
    style AL fill:#b7bdf8,stroke:#b7bdf8,color:#1e2030
    style TF fill:#eed49f,stroke:#eed49f,color:#1e2030
```

!!! note "Authelia has no data volume"
    Config is YAML in git (deployed by Ansible), sessions live in Valkey, and all credentials are stored in Authentik. The OIDC client secret (Authelia registered as a client in Authentik) is encrypted in SOPS.

!!! note "Placement rationale"
    A dedicated auth VM was considered but rejected: Traefik is pinned to Services VM, so forward auth always traverses Services VM regardless. Separating auth adds VM overhead without meaningful resilience gain.
