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
