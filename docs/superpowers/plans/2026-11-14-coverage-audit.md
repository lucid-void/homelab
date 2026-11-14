# coverage-audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily read-only CronJob that tells Gotify which routed services have no Gatus check or Homepage tile, and which PVCs have no backup job, unless git says the gap is deliberate.

**Architecture:** A stdlib-only Python script in the `monitoring` namespace reads five things from the Kubernetes API (HTTPRoutes, PVCs, CronJobs, the `gatus-config` ConfigMap, the `homepage` ConfigMap), compares them with pure functions, and subtracts an `exemptions.json` that lives beside the script in git. Any remaining gap, and any exemption that no longer excuses anything, is one line in a single Gotify message. It is stateless: a clean cluster sends nothing.

**Tech Stack:** Python 3 stdlib (`urllib`, `json`, `re`, `fnmatch`, `unittest`), the same `python:*-alpine` tag as `kubernetes/apps/monitoring/flight-tracker/app/deployment.yml:39`, a `configMapGenerator` for the code, a raw `CronJob` manifest.

**Spec:** none. The design was agreed in conversation on 2026-11-14 and is written out in the next section; this plan is the only record.

## Design

Three checks, each a set difference:

| Check | Every one of these | must appear in | or match an exemption under |
|---|---|---|---|
| gatus | hostname on an HTTPRoute attached to `gateway/shared` | a `url:` line in `gatus-config` → `config.yaml` | `gatus` |
| homepage | the same hostnames | an `href:` line in `homepage` → `services.yaml` | `homepage` |
| backup | every PVC, as `namespace/name` | the `claimName` of a volume on a CronJob whose name contains `backup` | `backup` |

A fourth rule keeps the exemptions file honest: an exemption that matches no uncovered item is reported as stale, and an exemption with an empty reason is reported as invalid.

Non-goals:

- SSO. `sso-audit` already checks login gates, and 23 of 34 routes are off SSO on purpose, so a route-to-OIDC-client check would be a 23-line exemption list with no signal.
- Whether a Gatus check or a backup is *good* (right health path, restorable snapshot). This only checks that one exists.
- ClusterIP-only services, TCPRoutes and GRPCRoutes. There is one TCPRoute (`gitea-ssh`) and it already has a Gatus check.
- A CI check on pull requests. The live cluster is the source of truth because several PVCs come from Helm charts and never appear as manifests in git.

What it finds today (measured against the live cluster, 2026-11-14):

- No Gatus check: `crosswatch`, `grafana`, `tdarr` (and `gatus` itself, which gets an exemption).
- No Homepage tile: `crosswatch`, `tdarr` (and `home`, `kromgo`, `obsidian`, which get exemptions).
- No backup job: 38 of 47 PVCs. About 16 are plainly disposable or covered another way; about 22 have never had a decision. Task 5 handles both groups.

## Global Constraints

- The Flux Kustomization must NOT set `spec.targetNamespace`: this app has a Role in `homepage` and everything else in `monitoring`. Every resource carries an explicit `metadata.namespace`.
- No Python at 0-indent inside a YAML `|` block. The code ships as files through `configMapGenerator`, never inline.
- The job never reads a Secret. RBAC grants `get` on exactly two named ConfigMaps and `get`/`list` on HTTPRoutes, PVCs and CronJobs.
- Stdlib only. No `pip`, no `apk add`.
- Image tag is pinned, never rolling. Copy the tag from `flight-tracker/app/deployment.yml:39` at implementation time.
- Run `.agents/scripts/validate-manifests.sh <file>` on each new or edited manifest, one file per call. A `SKIPPED:` line means not validated.
- Nothing is applied with `kubectl`. It reaches the cluster through a merged PR and Flux.
- Tests run with `python3 -m unittest discover -s kubernetes/apps/monitoring/coverage-audit/tests`, as flight-tracker's do. No CI workflow runs them.

## Review Focus

1. An HTTPRoute with two hostnames, or none (`gateway/redirect-http-to-https` has none): each hostname is checked on its own, and a route without hostnames is ignored rather than crashing. Test in Task 1.
2. A Gatus `tcp://host:port` or quoted `url: "https://host/path"` line: the host is still extracted. Test in Task 1.
3. A hostname that is a prefix of another (`gatus.blackcats.cc` vs `gatus.blackcats.cc.evil`, `home` vs `homebox`): matching is on the whole hostname, never a substring. Test in Task 1.
4. The Kubernetes API or a ConfigMap read fails: the job sends "Coverage audit failed" at priority 8 and exits 1, and does NOT report every service as uncovered. Test in Task 3.
5. `exemptions.json` is malformed or has an unknown top-level key: the job fails loudly (same path as 4) instead of running with no exemptions. Test in Task 2.

---

## File Structure

```
kubernetes/apps/monitoring/coverage-audit/
  ks.yml                      Flux Kustomization (no targetNamespace)
  app/
    kustomization.yml         resources + configMapGenerator
    audit.py                  pure comparison logic, no I/O
    main.py                   Kubernetes API reads, Gotify, exit code
    exemptions.json           deliberate gaps, each with a reason
    rbac.yml                  ServiceAccount, ClusterRole+Binding, two Roles+Bindings
    cronjob.yml
  tests/
    test_audit.py
    test_main.py
```

Modified: `kubernetes/apps/monitoring/kustomization.yml`, `kubernetes/apps/monitoring/gotify-bootstrap/app/bootstrap.sh`, `kubernetes/apps/monitoring/gatus/app/configmap.yml`, `kubernetes/apps/homepage/homepage/app/helm-values.yml`, `design/docs/services.md`, `design/docs/gitops.md`, `design/decisions/monitoring.md`, `design/TODO.md`.

---

### Task 1: Extract the facts (about 30 min)

**Files:**
- Create: `kubernetes/apps/monitoring/coverage-audit/app/audit.py`
- Test: `kubernetes/apps/monitoring/coverage-audit/tests/test_audit.py`

**Interfaces:**
- Produces:
  - `route_hosts(routes: list[dict]) -> dict[str, str]` maps hostname to `"namespace/name"` of its HTTPRoute, only for routes with a parentRef `{name: "shared", namespace: "gateway"}`.
  - `config_hosts(text: str, key: str) -> set[str]` returns every hostname on a line `<key>: <scheme>://<host>...` in a YAML text. `key` is `"url"` or `"href"`.
  - `backed_up_claims(cronjobs: list[dict]) -> set[str]` returns `"namespace/claimName"` for each PVC volume on a CronJob whose `metadata.name` contains `backup`.
  - `pvc_ids(pvcs: list[dict]) -> set[str]` returns `"namespace/name"`.

- [ ] **Step 1: Write the failing tests**

Follow `flight-tracker/tests/test_core.py` for the `sys.path.insert` header. Kubernetes objects in tests are minimal dicts shaped like `kubectl get -o json` items.

```python
SHARED = [{"name": "shared", "namespace": "gateway"}]

def route(ns, name, hosts, parents=SHARED):
    spec = {"parentRefs": parents}
    if hosts is not None:
        spec["hostnames"] = hosts
    return {"metadata": {"namespace": ns, "name": name}, "spec": spec}

class RouteHosts(unittest.TestCase):
    def test_maps_each_hostname_to_its_route(self):
        got = audit.route_hosts([route("media", "plex", ["plex.blackcats.cc"]),
                                 route("media", "mc", ["a.blackcats.cc", "b.blackcats.cc"])])
        self.assertEqual(got, {"plex.blackcats.cc": "media/plex",
                               "a.blackcats.cc": "media/mc", "b.blackcats.cc": "media/mc"})

    def test_ignores_route_without_hostnames(self):
        self.assertEqual(audit.route_hosts([route("gateway", "redirect", None)]), {})

    def test_ignores_route_on_another_gateway(self):
        r = route("x", "y", ["y.blackcats.cc"], [{"name": "other", "namespace": "gateway"}])
        self.assertEqual(audit.route_hosts([r]), {})

class ConfigHosts(unittest.TestCase):
    GATUS = '''
      - name: A
        url: https://a.blackcats.cc/health
      - name: B
        url: "https://b.blackcats.cc"
      - name: SSH
        url: tcp://gitea.blackcats.cc:22
      # url: https://commented.blackcats.cc
    '''
    def test_plain_quoted_and_tcp(self):
        self.assertEqual(audit.config_hosts(self.GATUS, "url"),
                         {"a.blackcats.cc", "b.blackcats.cc", "gitea.blackcats.cc"})

    def test_key_must_match(self):
        self.assertEqual(audit.config_hosts("href: https://a.blackcats.cc", "url"), set())

    def test_whole_hostname_only(self):
        got = audit.config_hosts("url: https://homebox.blackcats.cc", "url")
        self.assertNotIn("home.blackcats.cc", got)

class Claims(unittest.TestCase):
    def cj(self, ns, name, claims):
        vols = [{"name": c, "persistentVolumeClaim": {"claimName": c}} for c in claims]
        vols.append({"name": "tmp", "emptyDir": {}})
        return {"metadata": {"namespace": ns, "name": name},
                "spec": {"jobTemplate": {"spec": {"template": {"spec": {"volumes": vols}}}}}}

    def test_only_backup_cronjobs_count(self):
        got = audit.backed_up_claims([self.cj("gitea", "gitea-backup", ["gitea-data"]),
                                      self.cj("media", "youtube-queue", ["media-nfs"])])
        self.assertEqual(got, {"gitea/gitea-data"})

    def test_cronjob_without_volumes(self):
        cj = self.cj("postgres", "postgres-backup", [])
        del cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
        self.assertEqual(audit.backed_up_claims([cj]), set())

    def test_pvc_ids(self):
        self.assertEqual(audit.pvc_ids([{"metadata": {"namespace": "a", "name": "b"}}]), {"a/b"})
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/coverage-audit/tests`
Expected: `ModuleNotFoundError: No module named 'audit'`

- [ ] **Step 3: Implement the four functions in `audit.py`**

`config_hosts` uses one regex applied per line, anchored so a commented line does not match: `^\s*(?:-\s*)?<key>:\s*["']?[a-z]+://([^/:"'\s]+)`. No YAML parser: the image has none and the two files are flat enough.

- [ ] **Step 4: Run and confirm pass**

Run: same command. Expected: `OK`, 9 tests.

- [ ] **Step 5: Commit**

`git add kubernetes/apps/monitoring/coverage-audit && git commit -m "feat(coverage-audit): extract routes, config hosts and backup claims"`

---

### Task 2: Compare and apply exemptions (about 45 min)

**Files:**
- Modify: `kubernetes/apps/monitoring/coverage-audit/app/audit.py`
- Test: `kubernetes/apps/monitoring/coverage-audit/tests/test_audit.py`

**Interfaces:**
- Consumes: the four functions from Task 1.
- Produces:
  - `CHECKS = ("gatus", "homepage", "backup")`
  - `load_exemptions(text: str) -> dict[str, dict[str, str]]` parses JSON shaped `{"gatus": {pattern: reason}, "homepage": {...}, "backup": {...}}`. Missing check keys default to `{}`. Raises `ValueError` on invalid JSON, a non-object, an unknown top-level key, or a non-string reason.
  - `audit(hosts: dict[str, str], gatus: set[str], homepage: set[str], pvcs: set[str], backed_up: set[str], exemptions: dict[str, dict[str, str]]) -> list[str]` returns sorted finding lines.

Finding line wording, exact:

- `no Gatus check: <host> (<ns>/<route>)`
- `no Homepage tile: <host> (<ns>/<route>)`
- `no backup job: <ns>/<pvc>`
- `stale exemption: <check> "<pattern>" excuses nothing`
- `exemption without a reason: <check> "<pattern>"`

Patterns are `fnmatch.fnmatchcase` globs against the hostname (gatus, homepage) or `namespace/name` (backup). An exemption is stale when it matches no item that is uncovered before exemptions are applied.

- [ ] **Step 1: Write the failing tests**

```python
class Audit(unittest.TestCase):
    HOSTS = {"a.blackcats.cc": "x/a", "b.blackcats.cc": "x/b"}
    NONE = {"gatus": {}, "homepage": {}, "backup": {}}

    def run_audit(self, **kw):
        args = dict(hosts=self.HOSTS, gatus=set(self.HOSTS), homepage=set(self.HOSTS),
                    pvcs=set(), backed_up=set(), exemptions=self.NONE)
        args.update(kw)
        return audit.audit(**args)

    def test_clean(self):
        self.assertEqual(self.run_audit(), [])

    def test_missing_gatus_and_homepage(self):
        got = self.run_audit(gatus={"a.blackcats.cc"}, homepage={"b.blackcats.cc"})
        self.assertEqual(got, ["no Gatus check: b.blackcats.cc (x/b)",
                               "no Homepage tile: a.blackcats.cc (x/a)"])

    def test_missing_backup(self):
        got = self.run_audit(pvcs={"m/one", "m/two"}, backed_up={"m/one"})
        self.assertEqual(got, ["no backup job: m/two"])

    def test_exemption_silences_and_glob_matches(self):
        ex = {"gatus": {"b.blackcats.cc": "why"}, "homepage": {},
              "backup": {"gitea/valkey-data-*": "disposable"}}
        got = self.run_audit(gatus={"a.blackcats.cc"},
                             pvcs={"gitea/valkey-data-0", "gitea/valkey-data-1"}, exemptions=ex)
        self.assertEqual(got, [])

    def test_stale_exemption_when_item_is_now_covered(self):
        ex = {"gatus": {"a.blackcats.cc": "why"}, "homepage": {}, "backup": {}}
        self.assertEqual(self.run_audit(exemptions=ex),
                         ['stale exemption: gatus "a.blackcats.cc" excuses nothing'])

    def test_stale_exemption_when_item_is_gone(self):
        ex = {"gatus": {}, "homepage": {}, "backup": {"old/pvc": "why"}}
        self.assertEqual(self.run_audit(exemptions=ex),
                         ['stale exemption: backup "old/pvc" excuses nothing'])

    def test_empty_reason_is_reported_and_does_not_exempt(self):
        ex = {"gatus": {"b.blackcats.cc": "  "}, "homepage": {}, "backup": {}}
        got = self.run_audit(gatus={"a.blackcats.cc"}, exemptions=ex)
        self.assertEqual(got, ['exemption without a reason: gatus "b.blackcats.cc"',
                               "no Gatus check: b.blackcats.cc (x/b)"])

class LoadExemptions(unittest.TestCase):
    def test_missing_keys_default_empty(self):
        self.assertEqual(audit.load_exemptions('{"gatus": {"a": "r"}}'),
                         {"gatus": {"a": "r"}, "homepage": {}, "backup": {}})

    def test_rejects_bad_input(self):
        for bad in ("not json", "[]", '{"sso": {}}', '{"gatus": {"a": 1}}', '{"gatus": []}'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                audit.load_exemptions(bad)
```

- [ ] **Step 2: Run and confirm failure**

Expected: `AttributeError: module 'audit' has no attribute 'audit'`

- [ ] **Step 3: Implement `CHECKS`, `load_exemptions` and `audit`**

- [ ] **Step 4: Run and confirm pass**

Expected: `OK`, 18 tests.

- [ ] **Step 5: Commit**

`git commit -am "feat(coverage-audit): compare coverage and apply exemptions"`

---

### Task 3: Read the cluster and notify (about 45 min)

**Files:**
- Create: `kubernetes/apps/monitoring/coverage-audit/app/main.py`
- Test: `kubernetes/apps/monitoring/coverage-audit/tests/test_main.py`

**Interfaces:**
- Consumes: everything `audit.py` produces.
- Produces:
  - `kube_get(path: str) -> dict` does a GET on `https://kubernetes.default.svc<path>` with the ServiceAccount token and CA from `/var/run/secrets/kubernetes.io/serviceaccount/`, 30 s timeout. Raises on a non-200.
  - `notify(title: str, message: str, priority: int) -> None` POSTs JSON to `http://gotify.monitoring.svc.cluster.local/message` with header `X-Gotify-Key: $GOTIFY_TOKEN`. With no token it prints a warning and returns. A failed POST prints a warning and does not raise. Same behaviour as `notify()` in `kubernetes/apps/postgres/sso-audit/app/audit.sh`.
  - `collect(get=kube_get, exemptions_path: str = EXEMPTIONS_PATH) -> list[str]` performs the five reads and returns `audit.audit(...)`.
  - `main(get=kube_get, send=notify, exemptions_path: str = EXEMPTIONS_PATH) -> int`
  - `EXEMPTIONS_PATH = "/app/exemptions.json"`

API paths, exact:

- `/apis/gateway.networking.k8s.io/v1/httproutes`
- `/api/v1/persistentvolumeclaims`
- `/apis/batch/v1/cronjobs`
- `/api/v1/namespaces/monitoring/configmaps/gatus-config` → `data["config.yaml"]`
- `/api/v1/namespaces/homepage/configmaps/homepage` → `data["services.yaml"]`

`main` behaviour:

- No findings: print `No gaps.`, send nothing, return 0.
- Findings: print them, send title `Coverage audit: <n> gap(s)`, message of `• <line>` joined by newlines, priority 5, return 0. Returning 0 keeps `KubeJobFailed` for real failures only, as `sso-audit` does.
- Any exception from `collect` (API error, missing ConfigMap key, bad exemptions file): print the traceback, send title `Coverage audit failed`, message `<ExceptionType>: <text>. Check the coverage-audit Job logs.`, priority 8, return 1.

- [ ] **Step 1: Write the failing tests**

Use a fake `get` that returns canned dicts keyed by path and a `send` that records calls. Write the exemptions file with `tempfile`.

```python
class Main(unittest.TestCase):
    # fixture: one route a.blackcats.cc, covered by gatus and homepage, one PVC m/one backed up
    def test_clean_cluster_sends_nothing(self):
        self.assertEqual(main.main(get=self.get, send=self.send, exemptions_path=self.ex), 0)
        self.assertEqual(self.sent, [])

    def test_gap_is_sent_at_priority_5(self):
        self.responses["/api/v1/namespaces/monitoring/configmaps/gatus-config"] = \
            {"data": {"config.yaml": "endpoints: []"}}
        self.assertEqual(main.main(get=self.get, send=self.send, exemptions_path=self.ex), 0)
        self.assertEqual(self.sent, [("Coverage audit: 1 gap(s)",
                                      "• no Gatus check: a.blackcats.cc (x/a)", 5)])

    def test_api_failure_is_not_reported_as_gaps(self):
        def boom(path):
            raise OSError("connection refused")
        self.assertEqual(main.main(get=boom, send=self.send, exemptions_path=self.ex), 1)
        self.assertEqual(len(self.sent), 1)
        title, message, priority = self.sent[0]
        self.assertEqual((title, priority), ("Coverage audit failed", 8))
        self.assertIn("connection refused", message)

    def test_missing_configmap_key_fails(self):
        self.responses["/api/v1/namespaces/homepage/configmaps/homepage"] = {"data": {}}
        self.assertEqual(main.main(get=self.get, send=self.send, exemptions_path=self.ex), 1)
        self.assertEqual(self.sent[0][0], "Coverage audit failed")

    def test_bad_exemptions_file_fails(self):
        with open(self.ex, "w") as f:
            f.write('{"sso": {}}')
        self.assertEqual(main.main(get=self.get, send=self.send, exemptions_path=self.ex), 1)
        self.assertEqual(self.sent[0][0], "Coverage audit failed")
```

- [ ] **Step 2: Run and confirm failure**

Expected: `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 3: Implement `main.py`**, ending with `if __name__ == "__main__": sys.exit(main())`.

- [ ] **Step 4: Run and confirm pass**

Expected: `OK`, 23 tests.

- [ ] **Step 5: Commit**

`git commit -am "feat(coverage-audit): read the cluster and report to Gotify"`

---

### Task 4: Manifests and Gotify token (about 45 min)

**Files:**
- Create: `kubernetes/apps/monitoring/coverage-audit/ks.yml`, `app/kustomization.yml`, `app/rbac.yml`, `app/cronjob.yml`, `app/exemptions.json` (as `{"gatus": {}, "homepage": {}, "backup": {}}`; Task 5 fills it)
- Modify: `kubernetes/apps/monitoring/kustomization.yml` (add `- ./coverage-audit/ks.yml`)
- Modify: `kubernetes/apps/monitoring/gotify-bootstrap/app/bootstrap.sh` (after line 233)

**Interfaces:**
- Consumes: `main.py` reads `/app/exemptions.json` and env `GOTIFY_TOKEN`.
- Produces: Secret `monitoring/coverage-audit-gotify-secret`, key `GOTIFY_TOKEN`, written by gotify-bootstrap.

- [ ] **Step 1: `ks.yml`**

Copy `kubernetes/apps/postgres/sso-audit/ks.yml`. Change the name to `coverage-audit`, the path, and `dependsOn` to `gotify-bootstrap`. **Delete the `targetNamespace` line.** Keep `wait: false`.

- [ ] **Step 2: `app/kustomization.yml`**

Resources `./rbac.yml`, `./cronjob.yml`. One `configMapGenerator` named `coverage-audit-script`, `namespace: monitoring`, files `audit.py`, `main.py`, `exemptions.json`.

- [ ] **Step 3: `app/rbac.yml`**

- ServiceAccount `coverage-audit` in `monitoring`.
- ClusterRole + ClusterRoleBinding `coverage-audit`: `get`, `list` on `httproutes` (`gateway.networking.k8s.io`), `persistentvolumeclaims` (core), `cronjobs` (`batch`).
- Role + RoleBinding `coverage-audit` in `monitoring`: `get` on `configmaps`, `resourceNames: [gatus-config]`.
- Role + RoleBinding `coverage-audit` in `homepage`: `get` on `configmaps`, `resourceNames: [homepage]`; subject is the ServiceAccount in `monitoring`.

`kubernetes/apps/security/trivy-operator/report/rbac.yml` is the layout to follow.

- [ ] **Step 4: `app/cronjob.yml`**

Copy `kubernetes/apps/postgres/sso-audit/app/cronjob.yml`, including the kube-linter annotation and the whole `securityContext`, with these differences:

- `metadata.namespace: monitoring`, name `coverage-audit`, schedule `"30 7 * * *"`.
- `serviceAccountName: coverage-audit`, `automountServiceAccountToken: true`.
- Image: the pinned python tag from `flight-tracker/app/deployment.yml:39`.
- `command: ["python3", "/app/main.py"]`, env `PYTHONDONTWRITEBYTECODE=1` and `PYTHONUNBUFFERED=1`.
- Only env secret is `GOTIFY_TOKEN` from `coverage-audit-gotify-secret`, `optional: true`. No Postgres variables.
- ConfigMap `coverage-audit-script` mounted read-only at `/app`. No `/tmp` volume.
- `activeDeadlineSeconds: 120`. Requests `memory: 32Mi`, `cpu: 25m`; limit `memory: 128Mi`.

- [ ] **Step 5: Gotify token**

Add to `bootstrap.sh` directly under the `sso-audit` line:

```bash
ensure_app_token "coverage-audit"   "Services missing a health check, tile or backup" monitoring coverage-audit-gotify-secret GOTIFY_TOKEN
```

- [ ] **Step 6: Validate**

```bash
for f in kubernetes/apps/monitoring/coverage-audit/ks.yml \
         kubernetes/apps/monitoring/coverage-audit/app/kustomization.yml \
         kubernetes/apps/monitoring/coverage-audit/app/rbac.yml \
         kubernetes/apps/monitoring/coverage-audit/app/cronjob.yml \
         kubernetes/apps/monitoring/kustomization.yml; do
  .agents/scripts/validate-manifests.sh "$f"
done
mise exec -- kubectl kustomize kubernetes/apps/monitoring/coverage-audit/app | grep -c "^kind:"
```

Expected: no error and no `SKIPPED:` line from the loop; the last command prints `9` (1 ConfigMap, 1 ServiceAccount, 1 ClusterRole, 1 ClusterRoleBinding, 2 Roles, 2 RoleBindings, 1 CronJob). Also confirm the rendered CronJob's `configMap.name` carries the hash suffix.

- [ ] **Step 7: Commit**

`git commit -am "feat(coverage-audit): CronJob, RBAC and Gotify token"`

---

### Task 5: Close the real gaps and seed the exemptions (about 1 h, includes owner decisions)

**Files:**
- Modify: `kubernetes/apps/monitoring/coverage-audit/app/exemptions.json`
- Modify: `kubernetes/apps/monitoring/gatus/app/configmap.yml`
- Modify: `kubernetes/apps/homepage/homepage/app/helm-values.yml`
- Modify: `design/TODO.md`

- [ ] **Step 1: Add the three missing Gatus checks**

`crosswatch`, `grafana`, `tdarr`. Follow `design/docs/gitops.md` step 9: curl each candidate health path first and write the condition from what it returns. Grafana has `/api/health`; find the other two.

- [ ] **Step 2: Add the two missing Homepage tiles**

`CrossWatch` and `Tdarr`, both under `Media`, per `design/docs/gitops.md` step 10.

- [ ] **Step 3: Seed route exemptions**

```json
"gatus": {
  "gatus.blackcats.cc": "Gatus cannot report its own outage"
},
"homepage": {
  "home.blackcats.cc": "this is Homepage",
  "kromgo.blackcats.cc": "badge JSON endpoint, no UI",
  "obsidian.blackcats.cc": "CouchDB sync endpoint, no UI"
}
```

- [ ] **Step 4: Seed backup exemptions that the docs already justify**

Confirm each reason against the file named before writing it.

| Pattern | Reason | Source |
|---|---|---|
| `ai/clef-data` | re-downloadable venv and weights | `design/docs/services.md` |
| `ai/llama-models` | re-downloadable weights | `design/docs/services.md` |
| `gitea/valkey-data-*` | disposable cache, recreate rather than repair | `design/docs/services.md` |
| `immich/immich-model-cache` | re-downloadable ML models | `design/decisions/immich.md` |
| `media/media-nfs` | the Synology media share itself | `design/docs/storage.md` |
| `media/matcha-data`, `media/vanilla-data` | backed up into `mc-backups` by the server's own backup | verify in `design/decisions/minecraft.md`; if false, it is a finding, not an exemption |
| `monitoring/vmsingle-*` | metrics, not backed up by design | `design/decisions/monitoring.md` |
| `network/blocky-logs-1` | 14-day query log | `design/decisions/dns.md` |
| `postgres/postgres-*` | covered by `postgres-backup` (`pg_dump`, no PVC mount) | `design/decisions/backups.md` |
| `paperless/protonmail-bridge-config` | recovery is a re-login | `design/docs/services.md` |
| `paperless/paperless-consume` | transient inbox | verify in `design/decisions/oidc-apps.md` |

- [ ] **Step 5: Owner decision on the PVCs nobody has decided**

These have no backup and no recorded reason. Present the list to the owner and get one answer per line: back it up, or exempt it with a reason.

- `ai/open-webui-data`, `degoog/degoog-data`, `freshrss/freshrss-data`, `gitea/gitea-shared-storage`
- `media/crosswatch-config`, `kavita-config`, `prowlarr-config`, `radarr-config`, `romm-config`, `sabnzbd-config`, `seerr-config`, `sonarr-config`, `suwayomi-config`, `tdarr-config`
- `media/plex-config-local`, `media/jellyfin-config-local` (already an open item in `design/TODO.md`)
- `monitoring/flight-tracker-data`, `gatus-data`, `gotify-data`, `vm-stack-grafana`

For each one the owner wants backed up but that will not be done in this PR: add an exemption with the reason `UNDECIDED: tracked in design/TODO.md`, and add one TODO bullet listing them. That keeps the first run quiet without hiding the gap, and `grep UNDECIDED exemptions.json` is the backlog.

- [ ] **Step 6: Dry run against the live cluster**

Run the comparison from the workstation with `kubectl` read-only output piped into the pure functions (a throwaway script in the scratchpad, not committed): load the five JSON documents with `mise exec -- kubectl get ... -o json`, call `audit.audit`, and print the result.

Expected: `[]`. Any line left is either a missed exemption or a real gap; resolve each before merging.

- [ ] **Step 7: Validate and commit**

Run `.agents/scripts/validate-manifests.sh` on `gatus/app/configmap.yml` and `homepage/homepage/app/helm-values.yml`, then `python3 -m json.tool` on `exemptions.json`, then the unit tests.

`git commit -am "feat(coverage-audit): close Gatus and Homepage gaps, seed exemptions"`

---

### Task 6: Docs, PR, and live verification (about 30 min plus one Flux reconcile)

**Files:**
- Modify: `design/docs/services.md` (Monitoring & Alerting table)
- Modify: `design/docs/gitops.md` (steps 9 and 10, and a note on PVCs)
- Modify: `design/decisions/monitoring.md`

- [ ] **Step 1: `services.md`**

Add a `coverage-audit` row under `sso-audit`'s style: namespace `monitoring`, CronJob, daily 07:30, what it compares, that deliberate gaps live in `exemptions.json`.

- [ ] **Step 2: `gitops.md`**

One sentence each on steps 9 and 10: skipping the step is reported by `coverage-audit` the next morning, and a deliberate skip goes in `kubernetes/apps/monitoring/coverage-audit/app/exemptions.json` with a reason. Add the same for a new PVC with no backup job.

- [ ] **Step 3: `monitoring.md`**

A `coverage-audit` section with the gotchas only: it matches Gatus and Homepage by regex on `url:`/`href:` lines, not a YAML parse; "backed up" means mounted by a CronJob with `backup` in its name, so a PVC backed up some other way needs an exemption; a stale exemption is itself a finding; its Kustomization has no `targetNamespace` because of the Role in `homepage`.

- [ ] **Step 4: Open the PR**

Push the branch and open a PR with `gh`. Merging needs the owner's say-so.

- [ ] **Step 5: Verify live after merge**

```bash
mise exec -- flux get kustomization coverage-audit
mise exec -- kubectl -n monitoring create job coverage-audit-verify --from=cronjob/coverage-audit
mise exec -- kubectl -n monitoring logs job/coverage-audit-verify
mise exec -- kubectl -n monitoring delete job coverage-audit-verify
```

Expected: Kustomization `Ready True`; log line `No gaps.`; no Gotify message. The manual Job is a diagnostic run of an existing CronJob, not a config change.

- [ ] **Step 6: Prove it can fail**

In a follow-up commit on a scratch branch, do not merge: delete one line from `exemptions.json` and rerun the Task 5 dry run. Expected: exactly one `no ...` line for the item that exemption covered.
