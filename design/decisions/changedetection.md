# Changedetection.io

**Read before editing:** `kubernetes/apps/changedetection/`, `kubernetes/apps/monitoring/gotify-bootstrap/app/bootstrap.sh`

## Current state

Website change monitor in its own `changedetection` namespace, at
`changedetection.blackcats.cc` via HTTPRoute on the shared Gateway. One
`bjw-s/app-template` HelmRelease, one controller named `app`, so the Deployment and
Service are both `changedetection` with no suffix. Service port is `5000`.

The controller runs **two containers in one pod**:

| Container | Image | Port | Role |
|---|---|---|---|
| `app` | `ghcr.io/dgtlmoon/changedetection.io` | 5000 | Flask app + scheduler |
| `browser` | `dgtlmoon/sockpuppetbrowser` | 3000 | Chrome over CDP, for JavaScript pages |

They share a pod, so the app reaches Chrome at `PLAYWRIGHT_DRIVER_URL=ws://localhost:3000`
and the browser needs no Service of its own. Port 3000 speaks **only** WebSocket — it
serves no HTTP routes at all — so the browser liveness probe is a `tcpSocket`.

Storage is the `changedetection-data` PVC on `nfs-client`, RWO, 5Gi, mounted at
`/datastore` into the `app` container only (via `advancedMounts`). The image declares no
`USER`, so it runs as root and ignores `PUID`/`PGID`.

App env: `BASE_URL`, `PORT`, `PLAYWRIGHT_DRIVER_URL`, `TZ`, `DISABLE_VERSION_CHECK`,
`LLM_FEATURES_DISABLED`. Browser env: `SCREEN_WIDTH`, `SCREEN_HEIGHT`, `SCREEN_DEPTH`,
`MAX_CONCURRENT_CHROME_PROCESSES`.

Authentication is changedetection's own password, set once in the UI and stored in the
datastore. There is no OIDC and no SSO client.

Backups are the `changedetection-backup` CronJob at 01:30 — scale to 0, restic to
`rclone:filen:backups/restic/changedetection`, scale back up via an `EXIT` trap. Its
Gotify token comes from `changedetection/gotify-secret`, provisioned by the
`changedetection-backup` entry in `gotify-bootstrap`. Gatus checks the hostname with
`[STATUS] < 400`; Homepage has a tile under Apps.

## Rules

- **The `browser` container needs no `SYS_ADMIN` capability and no `/dev/shm` emptyDir** —
  upstream's `docker-compose.yml` suggests `cap_add: SYS_ADMIN`, but the image passes
  `--no-sandbox` and `--disable-dev-shm-usage` unconditionally, so it runs under the
  cluster-default PSA `baseline` with no namespace exception and no shared-memory volume.
  Do not add a `pod-security.kubernetes.io/enforce: privileged` label to this namespace.
- **The `browser` liveness probe must be `tcpSocket`, never `httpGet`** — port 3000 is a
  bare Python `websockets` server with no HTTP routing. Every path, `/stats` included,
  answers `426 Upgrade Required`, so an `httpGet` probe can never pass and kills the
  container every `periodSeconds × failureThreshold`. The per-connection stats this image
  logs go to stdout, not to an endpoint. Symptom when this is wrong: the browser container
  CrashLoopBackOffs while its own logs look completely healthy.
- **A crashlooping `browser` container takes the whole web UI down** — the Pod is Ready
  only when every container is, so the EndpointSlice goes `ready=false, serving=false` and
  the shared Gateway answers `no healthy upstream` even though the `app` container is fine
  and passing its own probes. Whenever changedetection 503s, check *both* container
  statuses, not just `app`.
- **Give the `browser` container a liveness probe but never a readiness probe** — any
  container's readiness gates the whole Pod's readiness, so a Chrome stall would pull the
  Service endpoint and take the web UI offline for a fault that only affects JavaScript
  fetching.
- **Keep `strategy: Recreate` on the controller** — the datastore is plain JSON files with
  no locking, so two pods overlapping during a rolling update both hold `/datastore` and
  the second writer's `url-watches.json` wins, silently losing every watch the first
  added.
- **Keep the storage class as `nfs-client`** — there is no SQLite here, unlike Plex and
  Obsidian LiveSync. The datastore is JSON plus brotli snapshot files, written to a temp
  file in the same directory and then `os.replace()`d, which upstream does explicitly for
  NFS atomicity. Moving it to `openebs-hostpath` by analogy with Plex would pin the app to
  one node for no gain.
- **Probes and health checks must accept 3xx, not assert `== 200`** — `/` answers 200 with
  no password set and 302 to the login page once one is set, so a 200-only condition turns
  red the moment the built-in password is enabled. The kubelet treats 200-399 as healthy;
  the Gatus condition is `[STATUS] < 400` for the same reason.
- **Raising `MAX_CONCURRENT_CHROME_PROCESSES` requires raising the `browser` memory limit
  in the same change** — each Chrome process costs roughly 300-400Mi, so raising the count
  without the limit gets the container OOMKilled mid-fetch. Excess fetches queue rather
  than fail, because `DROP_EXCESS_CONNECTIONS` defaults to `False`.
- **Do not set `USE_X_SETTINGS`** — it exists for reverse proxies that rewrite `Host` or
  add `X-Forwarded-Prefix`, and the shared Gateway passes the real `Host` and serves this
  at the domain root, so enabling it produces wrong generated URLs.
- **Keep `BASE_URL` pointing at the public hostname** — it prefixes links inside
  notification bodies, so without it alerts arrive with unclickable relative paths.
- **This service is VPN-gated, not behind SSO** — upstream has no OIDC support, so the
  only authentication is the built-in password set in the UI, the same position as
  Obsidian LiveSync. Do not add a `KeycloakOIDCClient` for it expecting a login
  flow to appear.
- **The backup scales the Deployment to 0 first** — not for SQLite reasons, but because the
  datastore is a multi-file set (a JSON index plus per-watch brotli snapshots and
  screenshots) and restic walking a live tree can capture the index and the files it
  references at different moments. A missed poll window costs nothing for this app.
- **The backup Job must keep its `podAffinity` onto the app pod's node** — the claim is
  RWO on `nfs-client`, so a Job scheduled elsewhere blocks on the mount.
- **Treat the container resource values as estimates until re-derived from
  VictoriaMetrics** — they were set before the service had any history, which the house
  rule otherwise forbids. Re-derive with a 30d p90 and pin `metrics_path="/metrics/cadvisor"`
  in the selector, because the kubelet exports `container_*` from two endpoints and an
  unpinned query doubles every series.

## Verify

```bash
mise exec -- kubectl get deploy changedetection -n changedetection -o jsonpath='{.spec.strategy.type}'
mise exec -- kubectl get pod -n changedetection -l app.kubernetes.io/name=changedetection -o jsonpath='{.items[0].spec.containers[*].name}'
mise exec -- kubectl get ns changedetection -o jsonpath='{.metadata.labels}'
mise exec -- kubectl get pvc changedetection-data -n changedetection -o jsonpath='{.spec.storageClassName}'
mise exec -- kubectl get secret gotify-secret -n changedetection -o jsonpath='{.metadata.name}'
mise exec -- kubectl get cronjob changedetection-backup -n changedetection -o jsonpath='{.status}'
# browser liveness must be tcpSocket -- httpGet on any path 426s forever
mise exec -- kubectl get deploy changedetection -n changedetection \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="browser")].livenessProbe}'
```
