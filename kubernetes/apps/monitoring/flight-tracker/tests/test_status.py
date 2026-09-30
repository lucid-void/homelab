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
