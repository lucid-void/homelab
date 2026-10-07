import http.client
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import clef  # noqa: E402
import triage  # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload):
    seen = []

    def opener(req, timeout):
        seen.append((req, timeout))
        if isinstance(payload, Exception):
            raise payload
        return FakeResp(json.dumps(payload).encode())
    opener.seen = seen
    return opener


def answer(**probs):
    return {"answers": {k: {"type": "noul", "noul": v} for k, v in probs.items()}}


class ClefTests(unittest.TestCase):
    def test_request_shape_and_result(self):
        op = opener_returning(answer(a=0.9, b=0.1))
        c = clef.Clef("http://clef:8080/", timeout=7, opener=op)
        self.assertEqual(c.noul("state", {"a": "A?", "b": "B?"}), {"a": 0.9, "b": 0.1})
        req, timeout = op.seen[0]
        self.assertEqual(req.full_url, "http://clef:8080/v1/systemone")
        self.assertEqual(timeout, 7)
        body = json.loads(req.data)
        self.assertEqual(body["model"], "clef-flash")
        self.assertEqual(body["state"], "state")
        self.assertEqual(body["questions"]["a"], {"type": "noul", "instructions": "A?"})

    def test_every_failure_becomes_clef_error(self):
        bad = [OSError("down"), urllib.error.URLError("x"), TimeoutError(),
               http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x"),
               {}, {"answers": {}}, {"answers": {"a": {}}},
               answer(a="0.9"), answer(a=1.5), answer(a=-0.1), answer(a=True), "text"]
        for payload in bad:
            with self.subTest(payload=payload):
                c = clef.Clef("http://clef", opener=opener_returning(payload))
                with self.assertRaises(clef.ClefError):
                    c.noul("s", {"a": "A?"})


class FakeClef:
    def __init__(self, p=None, error=None):
        self.p, self.error, self.calls = p, error, []

    def noul(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {"needed": self.p}


def msg(priority=5, id=1, title="t", message="m"):
    return {"id": id, "priority": priority, "title": title, "message": message}


class TriageTests(unittest.TestCase):
    def test_high_priority_is_forwarded_without_asking_clef(self):
        f = FakeClef(p=0.0)
        self.assertTrue(triage.Triage(f).judge(msg(priority=8)))
        self.assertTrue(triage.Triage(f).judge(msg(priority=10)))
        self.assertEqual(f.calls, [])

    def test_missing_or_odd_priority_is_forwarded_without_asking_clef(self):
        f = FakeClef(p=0.0)
        for m in ({"id": 1}, msg(priority="high"), msg(priority=None), msg(priority=True)):
            self.assertTrue(triage.Triage(f).judge(m))
        self.assertEqual(f.calls, [])

    def test_threshold_decides_low_priority(self):
        self.assertFalse(triage.Triage(FakeClef(p=0.49)).judge(msg()))
        self.assertTrue(triage.Triage(FakeClef(p=0.5)).judge(msg()))
        self.assertTrue(triage.Triage(FakeClef(p=0.99)).judge(msg()))

    def test_clef_failure_forwards(self):
        self.assertTrue(triage.Triage(FakeClef(error=clef.ClefError("OSError"))).judge(msg()))

    def test_missing_title_and_message_are_sent_as_empty_strings(self):
        f = FakeClef(p=0.9)
        triage.Triage(f).judge({"id": 1, "priority": 5})
        self.assertEqual(f.calls[0][0], {"title": "", "message": "", "priority": 5})

    def test_long_title_and_message_are_cut_before_sending(self):
        f = FakeClef(p=0.9)
        triage.Triage(f).judge(msg(title="t" * 5000, message="m" * 500000))
        state = f.calls[0][0]
        self.assertEqual((len(state["title"]), len(state["message"])), (triage.MAX_TITLE, triage.MAX_MESSAGE))

    def test_clef_error_starts_a_cooldown_that_skips_clef_then_retries(self):
        now = [1000.0]
        f = FakeClef(error=clef.ClefError("OSError"))
        t = triage.Triage(f, cooldown=300, clock=lambda: now[0])
        self.assertTrue(t.judge(msg(id=1)))
        self.assertEqual(len(f.calls), 1)
        now[0] += 299
        self.assertTrue(t.judge(msg(id=2)))
        self.assertTrue(t.judge(msg(id=3)))
        self.assertEqual(len(f.calls), 1)
        now[0] += 2
        self.assertTrue(t.judge(msg(id=4)))
        self.assertEqual(len(f.calls), 2)

    def test_cooldown_start_is_logged_once_not_per_message(self):
        now = [0.0]
        t = triage.Triage(FakeClef(error=clef.ClefError("OSError")), clock=lambda: now[0])
        with mock.patch("triage.log") as lg:
            for i in range(5):
                t.judge(msg(id=i))
        self.assertEqual(lg.call_count, 1)

    def test_is_critical(self):
        self.assertTrue(triage.is_critical(msg(priority=8)))
        self.assertFalse(triage.is_critical(msg(priority=7)))


if __name__ == "__main__":
    unittest.main()
