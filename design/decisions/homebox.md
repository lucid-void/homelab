# Homebox

**Read before editing:** `kubernetes/apps/homebox/`

## Current state

`replicas: 1` is pinned explicitly in the HelmRelease — without it, an out-of-band
scale-down sticks, since Flux does not reconcile replicas unless the chart or values
change.

`0.25.0` is the minimum working version: `v0.11.1` crashes on startup with
`NOT NULL constraint failed: new_users.group_users` (a known migration bug).

`v0.26.x` adds a mandatory API-key pepper — the app panics on startup
(`auth.api_key_pepper must be set to at least 32 bytes`) unless
`HBOX_AUTH_API_KEY_PEPPER` (≥32 bytes) is set. It is provided via the `homebox-secret`
SealedSecret and wired into the app env with `secretKeyRef`. Without it, every rollout
times out (pod never Ready) and Flux rolls back, leaving the HelmRelease stalled.

## Rules

- **Never rotate `HBOX_AUTH_API_KEY_PEPPER` casually** — it must stay stable, because
  rotating it invalidates every API key issued so far.
- **Never downgrade below `0.25.0`** — earlier versions hit the `new_users.group_users`
  migration crash on startup.

## Verify

```bash
mise exec -- kubectl get deploy homebox -n homebox -o jsonpath='{.spec.replicas}'
mise exec -- kubectl get deploy homebox -n homebox -o jsonpath='{..secretKeyRef}'
```
