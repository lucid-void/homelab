import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import clef  # noqa: E402
import core  # noqa: E402
import freshrss  # noqa: E402
import news  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
CHAT = "42"


def art(n, title=None, url=None):
    return {"id": f"a{n}", "title": title or f"title {n}", "summary": f"sum {n}", "url": url or f"https://x/{n}"}


class FakeSource:
    def __init__(self):
        self.batches = []

    def unread(self):
        r = self.batches.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClef:
    """Scores every topic by whether the topic word appears in the article title."""

    def __init__(self):
        self.error, self.calls = None, []

    def noul(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {qid: 0.9 if text.split(": ", 1)[1].split()[0].rstrip("?").lower() in state["title"].lower() else 0.1
                for qid, text in questions.items()}


class Harness:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "news.json")
        self.now, self.sent = T0, []
        self.send_ok = True
        self.source, self.clef = FakeSource(), FakeClef()
        self.news = self.make()

    def make(self):
        ctx = core.Ctx("news", lambda text, buttons=None: (self.sent.append(text), self.send_ok)[1], self.path)
        return news.News(ctx, CHAT, self.source, self.clef, now=lambda: self.now)

    def say(self, *args):
        self.news.commands["topics"](list(args))

    def poll(self, batch):
        self.source.batches.append(batch)
        return self.news.poll_once()

    def seed(self, batch=()):
        self.poll(list(batch))

    def state(self):
        with open(self.path) as f:
            return json.load(f)


class TopicCommandTests(unittest.TestCase):
    def test_empty_list_explains_how_to_add(self):
        h = Harness()
        h.say()
        self.assertIn("/topics add", h.sent[0])

    def test_add_list_and_remove_by_name_or_number(self):
        h = Harness()
        h.say("add", "kubernetes", "security")
        h.say("add", "football")
        self.assertEqual(h.state()["topics"][CHAT], ["kubernetes security", "football"])
        h.say("rm", "FOOTBALL")
        h.say("rm", "1")
        self.assertEqual(h.state()["topics"][CHAT], [])

    def test_duplicates_overlong_and_too_many_are_refused(self):
        h = Harness()
        h.say("add", "Rust")
        h.say("add", "rust")
        self.assertIn("Already following", h.sent[-1])
        h.say("add", "x" * (news.MAX_TOPIC_LEN + 1))
        self.assertIn("too long", h.sent[-1])
        for i in range(news.MAX_TOPICS):
            h.say("add", f"t{i}")
        self.assertIn("At most", h.sent[-1])

    def test_unknown_removal_and_bad_usage(self):
        h = Harness()
        h.say("rm", "nothing")
        self.assertIn("Not following", h.sent[-1])
        h.say("rm", "9")
        self.assertIn("Not following", h.sent[-1])
        h.say("add")
        self.assertEqual(h.sent[-1], news.USAGE)
        h.say("bogus")
        self.assertEqual(h.sent[-1], news.USAGE)

    def test_topics_survive_restart(self):
        h = Harness()
        h.say("add", "rust")
        h.news = h.make()
        self.assertEqual(h.news._topics(), ["rust"])


class PollTests(unittest.TestCase):
    def test_first_run_marks_the_backlog_seen_without_scoring(self):
        h = Harness()
        h.say("add", "rust")
        h.seed([art(1, "rust news"), art(2)])
        self.assertEqual(h.clef.calls, [])
        self.assertEqual(h.state()["seen"], ["a1", "a2"])

    def test_new_matching_article_becomes_pending_once(self):
        h = Harness()
        h.say("add", "rust")
        h.seed([art(1)])
        h.poll([art(3, "Rust 2.0 released"), art(1)])
        self.assertEqual(h.state()["pending"], [{"id": "a3", "title": "Rust 2.0 released",
                                                 "url": "https://x/3", "topics": ["rust"]}])
        h.poll([art(3, "Rust 2.0 released"), art(1)])
        self.assertEqual(len(h.state()["pending"]), 1)
        self.assertEqual(len(h.clef.calls), 1)

    def test_one_call_scores_all_topics(self):
        h = Harness()
        h.say("add", "rust")
        h.say("add", "python")
        h.seed()
        h.poll([art(2, "Python and Rust")])
        self.assertEqual(len(h.clef.calls), 1)
        self.assertEqual(h.state()["pending"][0]["topics"], ["rust", "python"])

    def test_non_matching_article_is_seen_but_not_pending(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        h.poll([art(2, "gardening")])
        self.assertEqual((h.state()["seen"], h.state()["pending"]), (["a2"], []))

    def test_clef_down_leaves_articles_unseen_and_logs_once(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        h.clef.error = clef.ClefError("OSError")
        self.assertFalse(h.poll([art(2, "rust")]))
        self.assertFalse(h.poll([art(2, "rust")]))
        self.assertEqual(h.state()["seen"], [])
        h.clef.error = None
        self.assertTrue(h.poll([art(2, "rust")]))
        self.assertEqual(len(h.state()["pending"]), 1)

    def test_freshrss_down_is_a_failed_poll_not_a_crash(self):
        h = Harness()
        h.seed()
        self.assertFalse(h.poll(freshrss.FreshRSSError("OSError")))

    def test_no_topics_means_nothing_is_scored_and_nothing_is_held_back(self):
        h = Harness()
        h.seed()
        h.poll([art(2, "rust")])
        self.assertEqual((h.clef.calls, h.state()["seen"]), ([], ["a2"]))

    def test_a_poll_scores_at_most_max_per_poll_and_keeps_the_rest_for_next_time(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        batch = [art(i, "rust") for i in range(2, news.MAX_PER_POLL + 12)]
        h.poll(batch)
        self.assertEqual(len(h.clef.calls), news.MAX_PER_POLL)
        h.poll(batch)
        self.assertEqual(len(h.clef.calls), len(batch))

    def test_seen_list_is_bounded(self):
        h = Harness()
        h.seed()
        h.poll([art(i) for i in range(news.MAX_PER_POLL)])
        h.news.state["seen"] = [f"old{i}" for i in range(news.MAX_SEEN)]
        h.poll([art(1000)])
        self.assertEqual(len(h.state()["seen"]), news.MAX_SEEN)
        self.assertEqual(h.state()["seen"][-1], "a1000")

    def test_corrupt_or_wrong_shape_state_starts_empty(self):
        for content in ("{nope", json.dumps({"topics": "x"}), json.dumps([1]),
                        json.dumps({"topics": {"1": [1]}, "pending": []})):
            with self.subTest(content=content):
                h = Harness()
                with open(h.path, "w") as f:
                    f.write(content)
                h.news = h.make()
                self.assertEqual(h.news.state["topics"], {})
                self.assertIsNone(h.news.state["seen"])


class DigestTests(unittest.TestCase):
    def pending(self, h, n=1):
        h.say("add", "rust")
        h.seed()
        h.poll([art(i, "rust") for i in range(2, 2 + n)])

    def test_first_check_sets_the_slot_and_sends_nothing(self):
        h = Harness()
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(h.state()["last_slot"], "2026-10-05T18")

    def test_digest_goes_out_when_the_slot_opens_and_clears_pending(self):
        h = Harness()
        self.pending(h, 2)
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)  # 08:00 UTC
        self.assertTrue(h.news.maybe_digest())
        self.assertIn("News digest: 2 matches", h.sent[-1])
        self.assertIn("https://x/2", h.sent[-1])
        self.assertEqual(h.state()["pending"], [])
        self.assertFalse(h.news.maybe_digest())  # same slot: once only

    def test_empty_digest_sends_nothing_but_closes_the_slot(self):
        h = Harness()
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(h.sent, [])
        self.assertEqual(h.state()["last_slot"], "2026-10-06T08")

    def test_second_slot_of_the_day(self):
        h = Harness()
        self.pending(h)
        h.now = T0 + timedelta(hours=1)
        h.news.maybe_digest()
        h.poll([art(9, "rust again")])
        h.now = T0 + timedelta(hours=11)  # 18:00
        self.assertTrue(h.news.maybe_digest())
        self.assertEqual(h.state()["last_slot"], "2026-10-06T18")

    def test_a_late_start_still_sends_the_missed_slot(self):
        h = Harness()
        self.pending(h)
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=4)  # 11:00, slot 08 opened earlier
        self.assertTrue(h.news.maybe_digest())

    def test_long_digest_is_split_and_the_rest_stays_pending(self):
        h = Harness()
        self.pending(h, 1)
        h.news.state["pending"] = [{"id": f"p{i}", "title": "t" * 200, "url": "https://x/" + "u" * 100,
                                    "topics": ["rust"]} for i in range(30)]
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)
        h.news.maybe_digest()
        self.assertLessEqual(len(h.sent[-1]), 4096)
        self.assertIn("more in the next digest", h.sent[-1])
        self.assertGreater(len(h.state()["pending"]), 0)

    def test_pending_is_bounded(self):
        h = Harness()
        h.seed()
        h.news._finish([], [{"id": str(i), "title": "t", "url": "", "topics": ["x"]}
                            for i in range(news.MAX_PENDING + 20)])
        self.assertEqual(len(h.state()["pending"]), news.MAX_PENDING)

    def test_digest_failure_restores_pending_and_slot(self):
        h = Harness()
        self.pending(h, 2)
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)  # 08:00 UTC
        pending_before = h.state()["pending"]
        last_slot_before = h.state()["last_slot"]
        h.send_ok = False
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(h.state()["pending"], pending_before)
        self.assertEqual(h.state()["last_slot"], last_slot_before)
        h.send_ok = True
        self.assertTrue(h.news.maybe_digest())
        self.assertEqual(h.state()["pending"], [])

    def test_module_protocol(self):
        h = Harness()
        self.assertEqual(set(h.news.commands), {"topics"})
        self.assertEqual(h.news.callbacks, {})
        self.assertTrue(all(isinstance(x, str) for x in h.news.help))


if __name__ == "__main__":
    unittest.main()
