import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import alerts  # noqa: E402
import clef  # noqa: E402
import core  # noqa: E402
import triage  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TOKEN = "SECRET-CLIENT-TOKEN"


def m(id, title="t", message="b", priority=5):
    return {"id": id, "title": title, "message": message, "priority": priority}


class Harness:
    """Alerts wired to a fake clock, a scripted Gotify and a message log."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "alerts.json")
        self.now = T0
        self.sent = []
        self.batches = []  # queue of message lists (newest first) or exceptions
        self.alerts = self.make()

    def make(self):
        ctx = core.Ctx("alerts", lambda text, buttons=None: self.sent.append(text), self.path)
        return alerts.Alerts(ctx, self._fetch, lambda: self.now)

    def _fetch(self):
        r = self.batches.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def poll(self, batch):
        self.batches.append(batch)
        return self.alerts.poll_once()

    def say(self, *args):
        self.alerts.commands["alerts"](list(args))

    def seed(self, top=1):
        self.poll([m(top)] if top else [])
        self.sent.clear()

    def state(self):
        with open(self.path) as f:
            return json.load(f)

    def write(self, content):
        with open(self.path, "w") as f:
            f.write(content)


class DurationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(alerts.parse_duration("1h"), timedelta(hours=1))
        self.assertEqual(alerts.parse_duration("24h"), timedelta(hours=24))
        self.assertEqual(alerts.parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(alerts.parse_duration("168h"), timedelta(days=7))

    def test_invalid(self):
        for bad in ("", "0h", "0m", "x", "8d", "169h", "-1h", "1.5h", "1h30m", "h", "1", None):
            self.assertIsNone(alerts.parse_duration(bad), bad)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_first_start_forwards_nothing_and_remembers_the_newest_id(self):
        self.h.poll([m(5), m(3)])
        self.assertEqual(self.h.sent, [])
        self.assertEqual(self.h.state()["last_id"], 5)
        self.h.poll([m(6, "new"), m(5), m(3)])
        self.assertEqual(self.h.sent, ["\U0001f7e1 new\nb"])

    def test_empty_server_seeds_zero_then_forwards_the_first_message(self):
        self.h.poll([])
        self.assertEqual(self.h.state()["last_id"], 0)
        self.h.poll([m(1, "first")])
        self.assertEqual(self.h.sent, ["\U0001f7e1 first\nb"])

    def test_forwards_oldest_first(self):
        self.h.seed(1)
        self.h.poll([m(4, "d"), m(3, "c"), m(2, "b"), m(1, "a")])
        self.assertEqual([s.split("\n")[0][2:] for s in self.h.sent], ["b", "c", "d"])

    def test_priority_picks_the_emoji(self):
        self.h.seed(0)
        self.h.poll([m(3, priority=9), m(2, priority=5), m(1, priority=1)])
        self.assertEqual([s[0] for s in self.h.sent], ["\U0001f7e2", "\U0001f7e1", "\U0001f534"])

    def test_message_without_title_message_or_priority(self):
        self.h.seed(0)
        self.h.poll([{"id": 1}])
        self.assertEqual(self.h.sent, ["\U0001f7e1"])

    def test_entries_without_a_usable_id_are_skipped(self):
        self.h.seed(0)
        self.h.poll([m(2, "ok"), "junk", None, {"title": "no id"}, {"id": "7"}, {"id": True}])
        self.assertEqual(self.h.sent, ["\U0001f7e1 ok\nb"])
        self.assertEqual(self.h.state()["last_id"], 2)

    def test_nothing_new_sends_nothing(self):
        self.h.seed(4)
        self.h.poll([m(4)])
        self.assertEqual(self.h.sent, [])

    def test_gotify_db_reset_forwards_what_the_server_now_holds(self):
        self.h.seed(500)
        with mock.patch("alerts.log") as lg:
            self.h.poll([m(2, "DRIFT", priority=8), m(1, "hello")])
        self.assertEqual(len(self.h.sent), 2)
        self.assertIn("DRIFT", self.h.sent[1])
        self.assertEqual(self.h.state()["last_id"], 2)
        lg.assert_called_once()

    def test_last_id_survives_restart_without_replaying(self):
        self.h.seed(4)
        self.h.poll([m(5), m(4)])
        self.h.sent.clear()
        self.h.alerts = self.h.make()
        self.h.poll([m(5), m(4)])
        self.assertEqual(self.h.sent, [])


class PollFailureTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.seed(1)

    def test_failure_keeps_last_id_and_returns_false(self):
        with mock.patch("alerts.log"):
            self.assertFalse(self.h.poll(OSError("down")))
        self.assertEqual(self.h.state()["last_id"], 1)

    def test_only_the_exception_type_is_logged_once_per_streak(self):
        with mock.patch("alerts.log") as lg:
            for _ in range(3):
                self.h.poll(OSError(f"connect to http://x?token={TOKEN} refused"))
        lg.assert_called_once_with("gotify poll failed: OSError")
        self.assertNotIn(TOKEN, str(lg.call_args))

    def test_a_success_ends_the_streak(self):
        with mock.patch("alerts.log") as lg:
            self.h.poll(OSError("a"))
            self.h.poll([m(1)])
            self.h.poll(OSError("b"))
        self.assertEqual(lg.call_count, 2)

    def test_bad_shapes_are_failures_not_crashes(self):
        with mock.patch("alerts.log"):
            for bad in (ValueError("json"), KeyError("messages"), TypeError("x"), {"messages": []}, None):
                self.assertFalse(self.h.poll(bad), bad)
        self.assertEqual(self.h.state()["last_id"], 1)


class MuteTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.seed(1)

    def test_status_when_on(self):
        self.h.say()
        self.assertEqual(self.h.sent, ["Alerts: ON"])

    def test_off_without_duration_is_indefinite(self):
        self.h.say("off")
        self.assertEqual(self.h.sent[-1], "Alerts: OFF (until you turn them on)")
        self.h.now = T0 + timedelta(days=30)
        self.h.say()
        self.assertEqual(self.h.sent[-1], "Alerts: OFF (until you turn them on)")

    def test_off_with_duration_shows_the_expiry(self):
        self.h.say("off", "1h")
        self.assertEqual(self.h.sent[-1], "Alerts: OFF until 13:00 UTC")

    def test_expiry_on_another_day_includes_the_date(self):
        self.h.say("off", "24h")
        self.assertEqual(self.h.sent[-1], "Alerts: OFF until 06 Oct 12:00 UTC")

    def test_on_unmutes(self):
        self.h.say("off")
        self.h.say("on")
        self.assertEqual(self.h.sent[-1], "Alerts: ON")

    def test_muted_messages_are_dropped_and_never_replayed(self):
        self.h.say("off")
        self.h.sent.clear()
        self.h.poll([m(2, "quiet"), m(1)])
        self.assertEqual(self.h.sent, [])
        self.h.say("on")
        self.h.sent.clear()
        self.h.poll([m(3, "loud"), m(2, "quiet"), m(1)])
        self.assertEqual(self.h.sent, ["\U0001f7e1 loud\nb"])

    def test_timed_mute_expires_by_itself(self):
        self.h.say("off", "1h")
        self.h.sent.clear()
        self.h.now = T0 + timedelta(minutes=30)
        self.h.poll([m(2, "during"), m(1)])
        self.assertEqual(self.h.sent, [])
        self.h.now = T0 + timedelta(minutes=61)
        self.h.poll([m(3, "after"), m(2), m(1)])
        self.assertEqual(self.h.sent, ["\U0001f7e1 after\nb"])
        self.h.say()
        self.assertEqual(self.h.sent[-1], "Alerts: ON")

    def test_mute_survives_a_restart(self):
        self.h.say("off", "4h")
        self.h.alerts = self.h.make()
        self.h.sent.clear()
        self.h.poll([m(2), m(1)])
        self.assertEqual(self.h.sent, [])

    def test_bad_arguments_get_the_usage_line(self):
        for args in (("off", "0h"), ("off", "1h", "x"), ("maybe",), ("on", "1h"), ("off", "8d")):
            self.h.sent.clear()
            self.h.say(*args)
            self.assertEqual(self.h.sent, [alerts.USAGE], args)
        self.h.say()
        self.assertEqual(self.h.sent[-1], "Alerts: ON")


class FailOpenTests(unittest.TestCase):
    def test_missing_state_means_on(self):
        h = Harness()
        h.say()
        self.assertEqual(h.sent, ["Alerts: ON"])

    def test_corrupt_or_wrong_shape_state_means_on_and_reseeds(self):
        for bad in ("{not json", "[]", "null", '{"muted": "yes"}', '{"muted": true, "until": "junk"}',
                    '{"muted": false, "last_id": "x"}', '{"muted": false, "last_id": true}'):
            h = Harness()
            h.write(bad)
            h.alerts = h.make()
            with mock.patch("alerts.log"):
                h.say()
                h.poll([m(9)])
                h.poll([m(10, "after"), m(9)])
            self.assertEqual(h.sent[0], "Alerts: ON", bad)
            self.assertEqual(h.sent[1:], ["\U0001f7e1 after\nb"], bad)


class FetchTests(unittest.TestCase):
    def call(self, body=b'{"messages": [{"id": 1}]}', side_effect=None):
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        with mock.patch("alerts.urllib.request.urlopen", side_effect=side_effect,
                        return_value=resp) as urlopen:
            return alerts.fetch_messages("http://gotify", TOKEN), urlopen

    def test_request_shape_and_result(self):
        result, urlopen = self.call()
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://gotify/message?limit=100")
        self.assertEqual(req.get_header("X-gotify-key"), TOKEN)
        self.assertEqual(result, [{"id": 1}])

    def test_http_error_propagates_as_oserror(self):
        err = urllib.error.HTTPError("http://gotify", 401, "no", {}, None)
        with self.assertRaises(OSError):
            self.call(side_effect=err)

    def test_body_without_messages_key_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.call(body=b"{}")


class ModuleTests(unittest.TestCase):
    def test_module_protocol(self):
        a = Harness().alerts
        self.assertEqual(a.name, "alerts")
        self.assertEqual(list(a.commands), ["alerts"])
        self.assertEqual(a.callbacks, {})
        self.assertTrue(a.help and all(line.startswith("/alerts") for line in a.help))



class FakeJudge:
    """Stands in for Triage: noise titles are dropped; `on_judge` runs after each judgement."""

    def __init__(self, noise=(), on_judge=None):
        self.noise, self.on_judge, self.judged = set(noise), on_judge, []

    def judge(self, msg):
        self.judged.append(msg["id"])
        if self.on_judge:
            self.on_judge(msg)
        return msg["title"] not in self.noise


class TriageHarness(Harness):
    def __init__(self, judge):
        self.judge = judge
        super().__init__()

    def make(self):
        ctx = core.Ctx("alerts", lambda text, buttons=None: self.sent.append(text), self.path)
        return alerts.Alerts(ctx, self._fetch, lambda: self.now, triage=self.judge)


class TriageTests(unittest.TestCase):
    def test_noise_is_dropped_and_needed_is_forwarded(self):
        h = TriageHarness(FakeJudge(noise={"Backup: x"}))
        h.seed(1)
        h.poll([m(3, title="Needed"), m(2, title="Backup: x")])
        self.assertEqual(h.sent, ["\U0001f7e1 Needed\nb"])
        self.assertEqual(h.state()["last_id"], 3)

    def test_critical_messages_are_sent_before_judged_ones(self):
        h = TriageHarness(FakeJudge())
        h.seed(1)
        h.poll([m(4, title="low2"), m(3, title="CRIT", priority=9), m(2, title="low1")])
        self.assertEqual([x.split("\n")[0] for x in h.sent],
                         ["\U0001f534 CRIT", "\U0001f7e1 low1", "\U0001f7e1 low2"])

    def test_real_triage_forwards_when_clef_is_down(self):
        class Down:
            def noul(self, state, questions):
                raise clef.ClefError("OSError")
        h = TriageHarness(triage.Triage(Down()))
        h.seed(1)
        h.poll([m(2, title="anything")])
        self.assertEqual(len(h.sent), 1)
        self.assertEqual(h.state()["dropped"], [])

    def test_a_muted_poll_never_calls_triage(self):
        j = FakeJudge()
        h = TriageHarness(j)
        h.seed(1)
        h.say("off")
        h.poll([m(2)])
        self.assertEqual((h.sent[-1:], j.judged), (["Alerts: OFF (until you turn them on)"], []))

    def test_muting_during_judging_stops_the_rest(self):
        h = TriageHarness(FakeJudge(on_judge=lambda msg: h.say("off") if msg["id"] == 2 else None))
        h.seed(1)
        h.sent.clear()
        h.poll([m(2), m(3)])
        self.assertEqual(h.sent, ["Alerts: OFF (until you turn them on)"])
        self.assertEqual(h.state()["last_id"], 3)

    def test_dropped_command_lists_newest_first_and_survives_restart(self):
        h = TriageHarness(FakeJudge(noise={"n1", "n2"}))
        h.seed(1)
        h.poll([m(2, title="n1")])
        h.now = T0 + timedelta(minutes=5)
        h.poll([m(3, title="n2")])
        h.sent.clear()
        h.alerts = h.make()
        h.say("dropped")
        text = h.sent[0]
        self.assertIn("Last 2 messages", text)
        self.assertLess(text.index("n2"), text.index("n1"))
        self.assertIn("10-05 12:05", text)

    def test_only_the_last_twenty_drops_are_kept(self):
        h = TriageHarness(FakeJudge(noise={"n"}))
        h.seed(1)
        h.poll([m(i, title="n") for i in range(2, 30)])
        self.assertEqual(len(h.state()["dropped"]), alerts.MAX_DROPS)
        self.assertEqual(h.state()["dropped"][0]["id"], 29)

    def test_dropped_command_without_triage_or_drops(self):
        h = Harness()
        h.say("dropped")
        self.assertEqual(h.sent, ["Triage is off: every message is forwarded."])
        t = TriageHarness(FakeJudge())
        t.say("dropped")
        self.assertEqual(t.sent, ["Triage has dropped nothing yet."])

    def test_corrupt_dropped_state_is_reset_not_fatal(self):
        h = TriageHarness(FakeJudge())
        h.write(json.dumps({"last_id": 1, "muted": False, "until": None, "dropped": "oops"}))
        h.alerts = h.make()
        self.assertEqual(h.alerts.state["dropped"], [])

    def test_old_state_file_without_dropped_still_loads(self):
        h = TriageHarness(FakeJudge())
        h.write(json.dumps({"last_id": 5, "muted": False, "until": None}))
        h.alerts = h.make()
        self.assertEqual((h.alerts.state["last_id"], h.alerts.state["dropped"]), (5, []))

    def test_usage_mentions_dropped(self):
        h = Harness()
        h.say("bogus")
        self.assertIn("dropped", h.sent[0])

if __name__ == "__main__":
    unittest.main()
