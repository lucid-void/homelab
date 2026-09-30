# Telegram status module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/status` command that replies with a one-message cluster health summary (firing alerts, pods, backups) read from VictoriaMetrics.

**Architecture:** `status.py` is a core module with no state file. Five PromQL queries run concurrently (5 s timeout each) through an injected `query` callable; three checks turn the results into green/red lines. Any failed or empty-when-it-should-not-be check renders red `unavailable`, never green.

**Tech Stack:** Python 3 stdlib only (`unittest`, `concurrent.futures`), Kustomize, Flux.

**Spec:** `docs/superpowers/specs/2026-09-30-telegram-status-module-design.md`. Read it first.

## Global Constraints

- Stdlib-only Python; read-only rootfs; no state file; no new secret, RBAC or Deployment change (env `VM_URL` has a default).
- `VM_URL` default: `http://vmsingle-vm-stack-victoria-metrics-k8s-stack.monitoring.svc.cluster.local:8428`.
- Queries run concurrently, each with a 5 s timeout; `/status` runs on the Telegram receiver thread.
- Log exception **type only**.
- Backup match is by CronJob name: `.*-backup|etcd-snapshot`; limit 26 h (`>` is stale, `==` is fine).
- An unavailable check is red. No Flux line (out of scope).
- Never `git add -A` (unrelated uncommitted files: `design/decisions/jellyfin.md`, `design/docs/storage.md`). Never record deployed versions in docs. Do not push; the user pushes on request.
- Test command (repo root, must end `OK`): `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
- Baseline: 146 tests pass.

## Review Focus

Each has a test in Task 1.

1. kube-state-metrics down: the pod-phase query returns no series. Pods and Backups must read `unavailable`, not green. [Task 1]
2. A backup CronJob that has never succeeded has no `last_successful_time` series and is invisible to the age query. It must show as `never`. [Task 1]
3. No matching backup CronJobs at all must be red `none found`. [Task 1]
4. One query raising an error whose text contains a secret-looking string: that line is `unavailable`, other lines still render, only the type is logged. [Task 1]
5. Malformed series (missing labels, `NaN`, non-numeric value, non-dict entries) are skipped, never crash. [Task 1]

---

### Task 1: `status.py` with tests

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/status.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_status.py`

**Interfaces:**
- Consumes: `core.log`, `core.Ctx`.
- Produces (Task 2): `status.DEFAULT_VM_URL`, `status.vm_query(base, promql, timeout=5) -> list`, `status.Status(ctx, query)` implementing the module protocol (`name="status"`, `help`, `commands={"status": fn}`, `callbacks={}`).

- [ ] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_status.py`:

```python
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import core  # noqa: E402
import status  # noqa: E402

GREEN, RED = "\U0001f7e2", "\U0001f534"


def s(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


def phases(running=125, failed=0, pending=0, unknown=0):
    return [s(running, phase="Running"), s(failed, phase="Failed"), s(pending, phase="Pending"),
            s(9, phase="Succeeded"), s(unknown, phase="Unknown")]


HEALTHY = {
    status.Q_ALERTS: [],
    status.Q_PHASES: phases(),
    status.Q_DOWN: [],
    status.Q_AGES: [s(52117, cronjob="etcd-snapshot"), s(37757, cronjob="gitea-backup")],
    status.Q_NEVER: [],
}


class Harness:
    """Status wired to a scripted VictoriaMetrics and a message log."""

    def __init__(self, overrides=None):
        self.results = {**HEALTHY, **(overrides or {})}
        self.sent = []
        ctx = core.Ctx("status", lambda text, buttons=None: self.sent.append(text), "/unused")
        self.status = status.Status(ctx, self.query)

    def query(self, promql):
        r = self.results[promql]
        if isinstance(r, Exception):
            raise r
        return r

    def run(self, *args):
        self.status.commands["status"](list(args))
        return self.sent[-1].split("\n")


class HealthyTests(unittest.TestCase):
    def test_all_green(self):
        self.assertEqual(Harness().run(), [
            f"Cluster: {GREEN} all good",
            f"{GREEN} Alerts: none firing",
            f"{GREEN} Pods: 125 running · 0 failed · 0 pending · 0 deployments down",
            f"{GREEN} Backups: oldest 14.5h (etcd-snapshot), limit 26h",
        ])

    def test_extra_arguments_are_ignored(self):
        self.assertEqual(Harness().run("foo", "bar"), Harness().run())

    def test_module_protocol(self):
        mod = Harness().status
        self.assertEqual(mod.name, "status")
        self.assertEqual(list(mod.commands), ["status"])
        self.assertEqual(mod.callbacks, {})
        self.assertEqual(mod.help, ["/status - cluster health summary"])


class AlertTests(unittest.TestCase):
    def test_firing_alerts_are_listed_with_counts_and_turn_the_header_red(self):
        lines = Harness({status.Q_ALERTS: [s(2, alertname="KubeJobFailed"), s(1, alertname="Foo")]}).run()
        self.assertEqual(lines[0], f"Cluster: {RED} needs attention")
        self.assertEqual(lines[1], f"{RED} Alerts: Foo, KubeJobFailed ×2")

    def test_more_than_five_alerts_are_truncated(self):
        many = [s(1, alertname=f"a{i}") for i in range(1, 8)]
        self.assertEqual(Harness({status.Q_ALERTS: many}).run()[1],
                         f"{RED} Alerts: a1, a2, a3, a4, a5, +2 more")

    def test_the_always_on_alerts_are_excluded_by_the_query(self):
        self.assertIn("Watchdog|InfoInhibitor", status.Q_ALERTS)

    def test_malformed_series_are_skipped(self):
        junk = [{"metric": {}}, {"value": "x"}, "junk", None, s("nan", alertname="A"),
                s("abc", alertname="B"), s(0, alertname="C"), {"metric": {"alertname": "D"}}]
        self.assertEqual(Harness({status.Q_ALERTS: junk}).run()[1], f"{GREEN} Alerts: none firing")


class PodTests(unittest.TestCase):
    def line(self, **over):
        return Harness(over).run()[2]

    def test_failed_pods_are_red(self):
        self.assertEqual(self.line(**{status.Q_PHASES: phases(failed=1)}),
                         f"{RED} Pods: 125 running · 1 failed · 0 pending · 0 deployments down")

    def test_pending_pods_are_red(self):
        self.assertTrue(self.line(**{status.Q_PHASES: phases(pending=2)}).startswith(RED))

    def test_unknown_pods_are_red_and_shown(self):
        self.assertEqual(self.line(**{status.Q_PHASES: phases(unknown=1)}),
                         f"{RED} Pods: 125 running · 0 failed · 0 pending · 1 unknown · 0 deployments down")

    def test_deployments_with_unavailable_replicas_are_red(self):
        self.assertEqual(self.line(**{status.Q_DOWN: [s(2)]}),
                         f"{RED} Pods: 125 running · 0 failed · 0 pending · 2 deployments down")

    def test_no_pod_series_means_unavailable_for_pods_and_backups_only(self):
        lines = Harness({status.Q_PHASES: []}).run()
        self.assertEqual(lines[0], f"Cluster: {RED} needs attention")
        self.assertEqual(lines[1], f"{GREEN} Alerts: none firing")
        self.assertEqual(lines[2], f"{RED} Pods: unavailable")
        self.assertEqual(lines[3], f"{RED} Backups: unavailable")


class BackupTests(unittest.TestCase):
    def line(self, **over):
        return Harness(over).run()[3]

    def test_stale_backup_is_named_with_its_age(self):
        self.assertEqual(self.line(**{status.Q_AGES: [s(31 * 3600, cronjob="gitea-backup")]}),
                         f"{RED} Backups: STALE gitea-backup 31h")

    def test_exactly_at_the_limit_is_fine(self):
        self.assertTrue(self.line(**{status.Q_AGES: [s(26 * 3600, cronjob="x-backup")]}).startswith(GREEN))

    def test_a_cronjob_that_never_succeeded_is_red(self):
        lines = self.line(**{status.Q_AGES: [s(31 * 3600, cronjob="gitea-backup")],
                             status.Q_NEVER: [s(1, cronjob="obsidian-backup")]})
        self.assertEqual(lines, f"{RED} Backups: STALE gitea-backup 31h, obsidian-backup never")

    def test_never_succeeded_alone_is_red_even_if_others_are_fresh(self):
        self.assertEqual(self.line(**{status.Q_NEVER: [s(1, cronjob="obsidian-backup")]}),
                         f"{RED} Backups: STALE obsidian-backup never")

    def test_no_matching_cronjobs_at_all_is_red(self):
        self.assertEqual(self.line(**{status.Q_AGES: [], status.Q_NEVER: []}),
                         f"{RED} Backups: none found")

    def test_offenders_are_truncated_at_five(self):
        many = [s(40 * 3600, cronjob=f"j{i}-backup") for i in range(1, 8)]
        self.assertTrue(self.line(**{status.Q_AGES: many}).endswith("+2 more"))

    def test_series_without_a_cronjob_label_are_skipped(self):
        self.assertTrue(self.line(**{status.Q_AGES: [s(99999), s(100, cronjob="a-backup")]}).startswith(GREEN))


class FailureTests(unittest.TestCase):
    def test_one_failing_query_only_affects_its_own_line(self):
        h = Harness({status.Q_ALERTS: OSError("http://x?token=SECRET refused")})
        with mock.patch("status.log") as lg:
            lines = h.run()
        self.assertEqual(lines[1], f"{RED} Alerts: unavailable")
        self.assertTrue(lines[2].startswith(GREEN) and lines[3].startswith(GREEN))
        self.assertEqual(lines[0], f"Cluster: {RED} needs attention")
        lg.assert_called_once_with("status Alerts failed: OSError")
        self.assertNotIn("SECRET", str(lg.call_args))

    def test_all_queries_failing_marks_everything_unavailable(self):
        h = Harness({q: OSError("down") for q in HEALTHY})
        with mock.patch("status.log"):
            lines = h.run()
        self.assertEqual(lines, [f"Cluster: {RED} needs attention", f"{RED} Alerts: unavailable",
                                 f"{RED} Pods: unavailable", f"{RED} Backups: unavailable"])

    def test_a_result_that_is_not_a_list_is_unavailable(self):
        with mock.patch("status.log"):
            self.assertEqual(Harness({status.Q_ALERTS: {"a": 1}}).run()[1], f"{RED} Alerts: unavailable")

    def test_queries_run_concurrently(self):
        barrier = threading.Barrier(5, timeout=3)
        h = Harness()

        def query(promql):
            barrier.wait()
            return h.results[promql]

        h.status.query = query
        self.assertEqual(h.run()[0], f"Cluster: {GREEN} all good")


def fake_response(payload):
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    return resp


class VmQueryTests(unittest.TestCase):
    def call(self, payload):
        with mock.patch("status.urllib.request.urlopen", return_value=fake_response(payload)) as urlopen:
            return status.vm_query("http://vm:8428", "up == 0"), urlopen

    def test_request_shape_and_result(self):
        result, urlopen = self.call({"status": "success", "data": {"result": [s(1)]}})
        url = urlopen.call_args[0][0]
        self.assertEqual(url, "http://vm:8428/api/v1/query?query=up+%3D%3D+0")
        self.assertEqual(urlopen.call_args[1]["timeout"], 5)
        self.assertEqual(result, [s(1)])

    def test_missing_or_null_data_raises(self):
        with self.assertRaises(KeyError):
            self.call({"status": "error"})
        with self.assertRaises(TypeError):
            self.call({"data": None})

    def test_default_url_points_at_vmsingle(self):
        self.assertIn("vmsingle-vm-stack-victoria-metrics-k8s-stack.monitoring.svc", status.DEFAULT_VM_URL)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -p test_status.py`
Expected: `ModuleNotFoundError: No module named 'status'`

- [ ] **Step 3: Write `status.py`**

Create `kubernetes/apps/monitoring/flight-tracker/app/status.py`:

```python
#!/usr/bin/env python3
"""/status: one-message cluster health summary read from VictoriaMetrics."""
import json
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core import log

DEFAULT_VM_URL = "http://vmsingle-vm-stack-victoria-metrics-k8s-stack.monitoring.svc.cluster.local:8428"
GREEN, RED = "\U0001f7e2", "\U0001f534"
BACKUP_MATCH = ".*-backup|etcd-snapshot"
BACKUP_MAX_AGE = 26 * 3600
LIST_MAX = 5

Q_ALERTS = 'sum by (alertname) (ALERTS{alertstate="firing",alertname!~"Watchdog|InfoInhibitor"})'
Q_PHASES = "sum by (phase) (kube_pod_status_phase)"
Q_DOWN = "count(kube_deployment_status_replicas_unavailable > 0)"
Q_AGES = f'time() - kube_cronjob_status_last_successful_time{{cronjob=~"{BACKUP_MATCH}"}}'
Q_NEVER = (f'kube_cronjob_info{{cronjob=~"{BACKUP_MATCH}"}} '
           "unless on(namespace,cronjob) kube_cronjob_status_last_successful_time")


class Unavailable(Exception):
    """The data needed for a check is missing, so the check cannot say the cluster is fine."""


def vm_query(base, promql, timeout=5):
    """Instant query. Raises OSError, ValueError, KeyError or TypeError on any failure."""
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)["data"]["result"]


def _label(series, key):
    metric = series.get("metric") if isinstance(series, dict) else None
    value = metric.get(key) if isinstance(metric, dict) else None
    return value if isinstance(value, str) and value else None


def _value(series):
    try:
        value = float(series["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _rows(results, label):
    """(label, value) for every well-formed series; anything malformed is skipped."""
    if not isinstance(results, list):
        raise TypeError("result")
    rows = []
    for series in results:
        name, value = _label(series, label), _value(series) if isinstance(series, dict) else None
        if name and value is not None:
            rows.append((name, value))
    return rows


def _capped(items):
    shown = ", ".join(items[:LIST_MAX])
    return shown + (f", +{len(items) - LIST_MAX} more" if len(items) > LIST_MAX else "")


def check_alerts(get):
    firing = sorted((name, int(v)) for name, v in _rows(get("alerts"), "alertname") if v > 0)
    if not firing:
        return True, "none firing"
    return False, _capped([f"{n} ×{c}" if c > 1 else n for n, c in firing])


def _pod_phases(get):
    counts = {name: int(v) for name, v in _rows(get("phases"), "phase")}
    if sum(counts.values()) <= 0:
        raise Unavailable("no pod series")
    return counts


def check_pods(get):
    counts = _pod_phases(get)
    down_rows = get("down")
    if not isinstance(down_rows, list):
        raise TypeError("result")
    down = 0
    for series in down_rows:
        value = _value(series) if isinstance(series, dict) else None
        if value is not None:
            down = int(value)
            break
    failed, pending, unknown = (counts.get(p, 0) for p in ("Failed", "Pending", "Unknown"))
    parts = [f"{counts.get('Running', 0)} running", f"{failed} failed", f"{pending} pending"]
    if unknown:
        parts.append(f"{unknown} unknown")
    parts.append(f"{down} deployments down")
    return not (failed or pending or unknown or down), " · ".join(parts)


def check_backups(get):
    _pod_phases(get)  # kube-state-metrics down would make every series below vanish
    ages = _rows(get("ages"), "cronjob")
    never = sorted({name for name, _ in _rows(get("never"), "cronjob")})
    if not ages and not never:
        return False, "none found"
    stale = sorted(((n, a) for n, a in ages if a > BACKUP_MAX_AGE), key=lambda x: -x[1])
    bad = [f"{n} {a / 3600:.0f}h" for n, a in stale] + [f"{n} never" for n in never]
    if bad:
        return False, "STALE " + _capped(bad)
    name, age = max(ages, key=lambda x: x[1])
    return True, f"oldest {age / 3600:.1f}h ({name}), limit {BACKUP_MAX_AGE // 3600}h"


class Status:
    name = "status"
    help = ["/status - cluster health summary"]

    def __init__(self, ctx, query):
        self.ctx, self.query = ctx, query
        self.commands = {"status": self._command}
        self.callbacks = {}

    def _fetch(self):
        queries = {"alerts": Q_ALERTS, "phases": Q_PHASES, "down": Q_DOWN,
                   "ages": Q_AGES, "never": Q_NEVER}
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            futures = {key: pool.submit(self.query, q) for key, q in queries.items()}
        return lambda key: futures[key].result()

    def _check(self, label, fn, get):
        try:
            ok, text = fn(get)
        except Exception as e:  # one broken check must not hide the others
            log(f"status {label} failed: {type(e).__name__}")
            ok, text = False, "unavailable"
        return ok, f"{GREEN if ok else RED} {label}: {text}"

    def _command(self, args):
        get = self._fetch()
        results = [self._check("Alerts", check_alerts, get),
                   self._check("Pods", check_pods, get),
                   self._check("Backups", check_backups, get)]
        header = f"Cluster: {GREEN} all good" if all(ok for ok, _ in results) \
            else f"Cluster: {RED} needs attention"
        self.ctx.send("\n".join([header] + [line for _, line in results]))
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`. Fix code, not tests, unless a test contradicts the spec (ledger a ruling).

- [ ] **Step 5: Commit** (tick this task's boxes first)

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/status.py \
        kubernetes/apps/monitoring/flight-tracker/tests/test_status.py \
        docs/superpowers/plans/2026-09-30-telegram-status-module.md
git commit -m "feat(telegram-bot): add status module reading cluster health from VictoriaMetrics

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Wire it up and document

**Files:**
- Modify: `app/main.py`, `app/kustomization.yml` (add `status.py` to `files:`), `tests/test_core.py` (`MainWiringTests`)
- Modify: `design/decisions/flight-tracker.md`, `design/decisions/monitoring.md` (all under `kubernetes/apps/monitoring/flight-tracker/` or `design/decisions/`)

- [ ] **Step 1: Update `MainWiringTests` first** (in `tests/test_core.py`): add `import status` next to the other imports inside the test, register `status.Status(bot.ctx("status"), lambda q: [])` after alerts, and change the assertion to `["alerts", "fetch", "list", "status", "track", "untrack"]`. Run the suite; expected `OK` (the test builds its own bot, it pins the registry). Confirm `grep -c status kubernetes/apps/monitoring/flight-tracker/app/main.py` is `0`.

- [ ] **Step 2: Edit `main.py`**: add `import status` after `import flights`; in `main()` add `vm_url = os.environ.get("VM_URL", status.DEFAULT_VM_URL)` after `gotify_host`, and after the alerts registration add:

```python
    bot.register(status.Status(bot.ctx("status"), lambda promql: status.vm_query(vm_url, promql)))
```

`app/kustomization.yml`: add `- status.py` after `- main.py` in `files:`.

- [ ] **Step 3: Verify**

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | grep -E "^  (alerts|core|flights|main|status)\.py:"
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app; echo validate=$?
```

Expected: `OK`; five files listed; `validate=0`.

- [ ] **Step 4: Local smoke test** (fake tokens, unreachable services; must print `flight-tracker started` and fail only on the network)

```bash
cd kubernetes/apps/monitoring/flight-tracker/app
D=$(mktemp -d)
echo '{"flights": [], "offset": 42}' > $D/state.json
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=123 AERODATABOX_KEY=x CLIENT_TOKEN=x GOTIFY_HOST=http://127.0.0.1:9 \
  VM_URL=http://127.0.0.1:9 STATE_PATH=$D/state.json timeout 5 python3 -u main.py 2>&1 | head -6
rm -rf $D
```

- [ ] **Step 5: Docs**
  - `design/decisions/flight-tracker.md`: add `status.py` to the file list and layout, `/status` to the command summary, and rules: **`/status` relies on `ALERTS`, `kube_pod_status_phase`, `kube_deployment_status_replicas_unavailable` and `kube_cronjob_*`; an unavailable check is red, never green**; **the backup line matches CronJobs by name (`*-backup`, `etcd-snapshot`), so a new backup job with another name is not covered**; **there is no Flux line, because the Flux controllers do not emit per-object Ready metrics** (needs kube-state-metrics custom resource state).
  - `design/decisions/monitoring.md`: read it first, add one sentence in the fitting place: the bot's `/status` reads those series from vmsingle, so renaming or dropping them breaks it.

- [ ] **Step 6: Commit** (tick boxes first)

```bash
git add kubernetes/apps/monitoring/flight-tracker design/decisions/flight-tracker.md design/decisions/monitoring.md \
        docs/superpowers/plans/2026-09-30-telegram-status-module.md
git commit -m "feat(telegram-bot): register status module and document it

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git status --short
```

`git status --short` must show nothing under `design/` other than the two files above, and the user's two unrelated files stay untouched in the main checkout.

---

### Task 3: Final verification (ship needs the user)

- [ ] **Step 1:** Suite green; `git diff --stat main...HEAD` shows only `status.py`, `test_status.py`, `main.py`, `test_core.py`, `kustomization.yml`, the two design files and the plan.
- [ ] **Step 2:** Tell the user it is ready and wait for an explicit push instruction. Ship steps when told: `git push origin main` (after a fast-forward merge), `mise exec -- flux reconcile kustomization flight-tracker --with-source`, `kubectl -n monitoring logs deploy/flight-tracker --tail=15`, then `/status` in the chat (expect red while `KubeJobFailed` is firing). Rollback: revert the merge range, push, reconcile.
