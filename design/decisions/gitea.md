# Gitea

**Read before editing:** `kubernetes/apps/gitea/`

## Current state

Official `gitea-charts/gitea` chart from `https://dl.gitea.com/charts/`. External CNPG
Postgres — `postgresql.enabled: false`, `postgresql-ha.enabled: false`. DB password via
`extraEnvFrom: gitea-db-env` (written by the `gitea-db-bootstrap` Job, which remaps
`password` → `GITEA__database__PASSWD`). The chart creates a **Deployment**, not a
StatefulSet. HTTP service name is `{release-name}-http` (`gitea-http`) on port `3000` —
HTTPRoute `backendRef` must use `gitea-http`.

**SSH.** `git@gitea.blackcats.cc:...` works via a `TCPRoute` at
`kubernetes/apps/gitea/gitea/app/tcproute.yml`, attaching the shared Gateway's `ssh`
TCP:22 listener to the `gitea-ssh` Service (22 → pod `2222`). The chart's `gitea-ssh`
Service is headless ClusterIP by default. Relevant env: `START_SSH_SERVER`,
`SSH_DOMAIN`, `SSH_PORT`. This has to live on the Gateway rather than a pool-b
LoadBalancer: the hostname is an A record for the Gateway VIP, one name can't split
HTTPS to `.50` and SSH elsewhere, and sharing `.50` with a second Service is the
dual-ARP/RST trap. The Gateway Service is `externalTrafficPolicy: Cluster`, so Gitea
logs the cluster-internal source IP (`10.244.x.x`), not the real client IP. Gatus checks
`tcp://gitea.blackcats.cc:22` as a separate check from the HTTPS one.

**Cache/session/queue run on a bundled valkey cluster, not memory.** Chart v11 renamed
the dependency `redis-cluster` → `valkey-cluster`; a values file still setting
`redis-cluster.enabled: false` is a silent no-op (default `enabled: true`), and while
valkey is enabled the chart unconditionally overwrites `cache.ADAPTER`/
`session.PROVIDER`/`queue.TYPE` and their connection strings (`_helpers.tpl`, "valkey
queue" block) — any `memory`/`channel` values in the HelmRelease are silently discarded.
`cluster.nodes` is the **total** pod count including replicas
(`nodes = primaries + primaries * replicas`), so 3 primaries + 1 replica each is
`nodes: 6, replicas: 1`; the chart default `nodes: 3 / replicas: 0` is 3 primaries with
no failover.

**OIDC.** Callback URI: `https://gitea.blackcats.cc/user/oauth2/Keycloak/callback` — the
provider name segment is case-sensitive and must match `gitea.oauth[].name` exactly.
The sealed `gitea-oidc-secret` carries a `values.yaml` key containing the full
`gitea.oauth` list (`key`, `secret`, `autoDiscoverUrl`). The HelmRelease uses two
`valuesFrom` entries: the static sealed secret (admin password) and the OIDC secret.

**Gitea stores auth sources in its database and only ever adds or updates by name**, so
renaming the provider in `gitea.oauth[].name` creates a *second* source rather than
renaming the first — the login page then shows both buttons. Deleting the old source
unlinks every account that was linked through it, so delete it only after a successful
login through the new one. Flux's helm-controller also does **not** watch `valuesFrom`
Secrets: after changing one, `flux reconcile helmrelease gitea -n gitea --force`, or the
pod restarts with the old values. `DISABLE_REGISTRATION: false` +
`ALLOW_ONLY_EXTERNAL_REGISTRATION: true` allows OIDC self-register. Admin account uses
`passwordMode: initialOnlyNoReset` (`initialSetup` is invalid from chart v10 onward;
valid values are `keepUpdated`, `initialOnlyNoReset`, `initialOnlyRequireReset`).

## Rules

- **Never declare `cache.ADAPTER`, `session.PROVIDER` or `queue.TYPE` while valkey is
  enabled** — the chart overwrites all three unconditionally, so a `memory`/`channel`
  value in git is not what the pod runs.
- **Never roll the Gateway API bundle below `1.6.x`, and after bumping it run `kubectl -n kube-system rollout restart deploy/cilium-operator`**
  — this SSH TCPRoute needs the v1 TCPRoute CRD, which only ships at bundle 1.6; Cilium
  also discovers optional GW API CRDs once at startup with a version-exact check, so a
  running operator ignores a newly installed v1 TCPRoute. Symptom: an empty
  `TCPRoute.status` with every Gateway listener `Programmed` and the port refusing
  connections; `GatewayClass.status.supportedFeatures` lists TCPRoute the whole time
  regardless and is not evidence it works.
- **Check `kubectl get endpointslice -n gateway` for SSH routing, not the
  `CiliumEnvoyConfig`** — Cilium serves TCPRoute without Envoy: it generates an
  `EndpointSlice` on the Gateway's own Service (annotated
  `gateway.cilium.io/backend-service`/`-port`) pointing straight at the pod's `:2222`, so
  a working TCPRoute leaves no trace in the CEC. `enable-gateway-api-proxy-protocol`
  cannot recover the client IP here because this path never reaches Envoy.
- **Scaling the valkey cluster is not a plain `nodes` bump** — new pods never join on
  their own, so their `cluster_state:ok` readiness probe never passes, Helm times out on
  `--wait`, and Flux rolls back. Neither documented path works unattended:
  `cluster.update.addNodes: true` runs as a `post-upgrade` hook, but `--wait` blocks on
  the StatefulSet being Ready *before* post-upgrade hooks fire, so it deadlocks; cluster
  creation otherwise happens only in pod 0's entrypoint
  (`VALKEY_CLUSTER_CREATOR=yes`), which fires only when pod 0's data dir is empty. The
  rollout that works: `flux suspend` → scale the StatefulSet to 0 → wipe the data
  directories in place (commands below) → resume → scale to the target.
- **Deleting the valkey PVCs does not clear the data** — `nfs-client` share paths are
  derived from namespace+PVC name and the class is `Retain`, so a same-named PVC
  re-adopts the old directory and pod 0 skips cluster creation, re-forming the old
  topology with stale node IDs. Wipe the directory contents in place instead:

  ```bash
  kubectl scale sts <name> -n <ns> --replicas=0
  # run a throwaway root pod mounting each PVC, then:
  #   find /d -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  kubectl scale sts <name> -n <ns> --replicas=<n>
  ```
- **Scaling the StatefulSet to 0 makes Helm's `--wait` succeed trivially** (0/0 reads as
  Ready) — a stuck `pending-upgrade` release can flip to `deployed` while the live
  StatefulSet is empty. Check `spec.replicas` against the chart, not just HR `Ready`.
- **After any valkey recreate, pair replicas onto a different node than their primary
  with `CLUSTER REPLICATE`** — `valkey-cli --cluster create` pairs each primary with the
  replica on its own node by default, so a single node loss kills both and takes the
  slots down. This pairing lives in each node's `nodes.conf`, which is runtime state:
  it survives restarts but not a rebuild, so re-check `cluster nodes` against
  pod→node placement after every recreate.

## Verify

```bash
mise exec -- kubectl get deployments -n gitea
mise exec -- kubectl get endpointslice -n gateway
mise exec -- kubectl get sts -n gitea -o jsonpath='{.items[*].spec.replicas}'
```
