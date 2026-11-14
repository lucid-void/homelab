import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import audit  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
