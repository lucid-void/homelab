import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main  # noqa: E402

GATUS_CM = "/api/v1/namespaces/monitoring/configmaps/gatus-config"
HOMEPAGE_CM = "/api/v1/namespaces/homepage/configmaps/homepage"


class Main(unittest.TestCase):
    """Fixture: one route a.blackcats.cc with a Gatus check and a tile, one PVC m/one
    mounted by a backup CronJob. Clean until a test breaks something."""

    def setUp(self):
        self.sent = []
        self.responses = {
            "/apis/gateway.networking.k8s.io/v1/httproutes": {"items": [{
                "metadata": {"namespace": "x", "name": "a"},
                "spec": {"parentRefs": [{"name": "shared", "namespace": "gateway"}],
                         "hostnames": ["a.blackcats.cc"]}}]},
            "/api/v1/persistentvolumeclaims": {"items": [
                {"metadata": {"namespace": "m", "name": "one"}}]},
            "/apis/batch/v1/cronjobs": {"items": [{
                "metadata": {"namespace": "m", "name": "one-backup"},
                "spec": {"jobTemplate": {"spec": {"template": {"spec": {"volumes": [
                    {"name": "d", "persistentVolumeClaim": {"claimName": "one"}}]}}}}}}]},
            GATUS_CM: {"data": {"config.yaml": "  - name: A\n    url: https://a.blackcats.cc/h\n"}},
            HOMEPAGE_CM: {"data": {"services.yaml": "- G:\n  - A:\n      href: https://a.blackcats.cc\n"}},
        }
        fd, self.ex = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write('{"gatus": {}, "homepage": {}, "backup": {}}')
        self.addCleanup(os.unlink, self.ex)

    def get(self, path):
        return self.responses[path]

    delivered = True

    def send(self, title, message, priority):
        self.sent.append((title, message, priority))
        return self.delivered

    def run_main(self, get=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main.main(get=get or self.get, send=self.send, exemptions_path=self.ex)
        return code

    def test_clean_cluster_sends_nothing(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sent, [])

    def test_gap_is_sent_at_priority_5(self):
        self.responses[GATUS_CM] = {"data": {"config.yaml": "  url: https://other.blackcats.cc\n"}}
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sent, [("Coverage audit: 1 gap(s)",
                                      "• no Gatus check: a.blackcats.cc (x/a)", 5)])

    def test_undelivered_gaps_fail_the_job(self):
        self.responses[GATUS_CM] = {"data": {"config.yaml": "  url: https://other.blackcats.cc\n"}}
        self.delivered = False
        self.assertEqual(self.run_main(), 1)

    def test_config_with_no_recognisable_hosts_fails_instead_of_flagging_everything(self):
        for path, key in ((GATUS_CM, "config.yaml"), (HOMEPAGE_CM, "services.yaml")):
            with self.subTest(path=path):
                saved = self.responses[path]
                self.responses[path] = {"data": {key: "format: changed\n"}}
                self.sent.clear()
                self.assertEqual(self.run_main(), 1)
                self.assertEqual([s[0] for s in self.sent], ["Coverage audit failed"])
                self.responses[path] = saved

    def test_api_failure_is_not_reported_as_gaps(self):
        def boom(path):
            raise OSError("connection refused")
        self.assertEqual(self.run_main(get=boom), 1)
        self.assertEqual(len(self.sent), 1)
        title, message, priority = self.sent[0]
        self.assertEqual((title, priority), ("Coverage audit failed", 8))
        self.assertIn("connection refused", message)

    def test_missing_configmap_key_fails(self):
        self.responses[HOMEPAGE_CM] = {"data": {}}
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sent[0][0], "Coverage audit failed")

    def test_bad_exemptions_file_fails(self):
        with open(self.ex, "w") as f:
            f.write('{"sso": {}}')
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sent[0][0], "Coverage audit failed")


if __name__ == "__main__":
    unittest.main()
