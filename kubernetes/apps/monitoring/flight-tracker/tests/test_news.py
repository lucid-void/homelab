import copy
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
import interests  # noqa: E402
import news  # noqa: E402
import test_interests  # noqa: E402  (shares its small profile fixture)

UTC = timezone.utc
T0 = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
CHAT = "42"


def art(n, title=None, url=None):
    return {"id": f"a{n}", "title": title or f"title {n}", "summary": f"sum {n}", "url": url or f"https://x/{n}"}


class FakeSource:
    def __init__(self):
        self.batches, self.since = [], []

    def unread(self, since=None):
        self.since.append(since)
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


class ProfileClef(FakeClef):
    """Profile questions get the configured probability (default 0.1); custom-topic questions work as in FakeClef."""

    def __init__(self, **answers):
        super().__init__()
        self.answers = answers

    def noul(self, state, questions):
        if any(q.startswith("t") and q[1:].isdigit() for q in questions):
            return super().noul(state, questions)
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {q: self.answers.get(q, 0.1) for q in questions}


class Harness:
    profile = None

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "news.json")
        self.now, self.sent = T0, []
        self.send_ok = True
        self.source, self.clef = FakeSource(), self.make_clef()
        self.news = self.make()

    def make_clef(self):
        return FakeClef()

    def make(self):
        ctx = core.Ctx("news", lambda text, buttons=None: (self.sent.append(text), self.send_ok)[1], self.path)
        return news.News(ctx, CHAT, self.source, self.clef, now=lambda: self.now, profile=self.profile)

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

    def test_first_poll_stores_since_and_passes_it_later_polls_reuse_it(self):
        h = Harness()
        h.seed([art(1)])
        stamp = int(T0.timestamp())
        self.assertEqual((h.state()["since"], h.source.since), (stamp, [stamp]))
        h.now = T0 + timedelta(hours=1)
        h.poll([art(2)])
        self.assertEqual((h.state()["since"], h.source.since), (stamp, [stamp, stamp]))

    def test_since_survives_a_failed_first_fetch(self):
        h = Harness()
        h.poll(freshrss.FreshRSSError("OSError"))
        stamp = int(T0.timestamp())
        self.assertEqual(h.state()["since"], stamp)
        h.now = T0 + timedelta(hours=1)
        h.seed([art(1)])
        self.assertEqual(h.source.since, [stamp, stamp])

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

    def test_topics_are_scored_in_chunks_of_three(self):
        h = Harness()
        for t in ("aa", "bb", "cc", "dd", "ee", "ff", "gg"):
            h.say("add", t)
        h.seed()
        h.poll([art(2, "aa ee gg")])
        self.assertEqual([sorted(q) for _, q in h.clef.calls],
                         [["t0", "t1", "t2"], ["t3", "t4", "t5"], ["t6"]])
        self.assertEqual(h.state()["pending"][0]["topics"], ["aa", "ee", "gg"])

    def test_clef_error_in_a_later_chunk_leaves_the_article_unseen(self):
        h = Harness()
        for t in ("aa", "bb", "cc", "dd"):
            h.say("add", t)
        h.seed()
        real = h.clef.noul

        def flaky(state, questions):
            if len(h.clef.calls) == 1:
                h.clef.calls.append((state, questions))
                raise clef.ClefError("OSError")
            return real(state, questions)
        h.clef.noul = flaky
        self.assertFalse(h.poll([art(2, "aa dd")]))
        self.assertEqual((h.state()["seen"], h.state()["pending"]), ([], []))

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
                        json.dumps({"topics": {"42": ["rust"]}, "pending": [], "since": "x"}),
                        json.dumps({"topics": {"42": ["rust"]}, "pending": [], "since": True}),
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

    def test_matches_for_unfollowed_topics_are_dropped_at_digest_time(self):
        h = Harness()
        h.say("add", "rust")
        h.say("add", "python")
        h.seed()
        h.poll([art(2, "rust only"), art(3, "Python and Rust"), art(4, "python only")])
        h.say("rm", "python")
        h.sent.clear()
        h.news.maybe_digest()
        h.now = T0.replace(hour=18)
        self.assertTrue(h.news.maybe_digest())
        self.assertIn("rust only", h.sent[0])
        self.assertIn("Python and Rust (rust)", h.sent[0])
        self.assertNotIn("python only", h.sent[0])
        self.assertEqual(h.state()["pending"], [])

    def test_digest_with_only_unfollowed_matches_sends_nothing_and_closes_the_slot(self):
        h = Harness()
        h.say("add", "rust")
        h.seed()
        h.poll([art(2, "rust")])
        h.say("rm", "rust")
        h.news.maybe_digest()
        h.sent.clear()
        h.now = T0.replace(hour=18)
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual((h.sent, h.state()["pending"]), ([], []))
        self.assertEqual(h.state()["last_slot"], "2026-10-06T18")

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


def profile_for_tests(version=3):
    raw = copy.deepcopy(test_interests.RAW)
    raw["version"] = version
    return interests.compile_profile(raw)


def part(n, title, categories=(), site="", url=None, summary=""):
    return {**art(n, title, url), "summary": summary, "categories": list(categories), "site": site, "feed": "f"}


class ProfileHarness(Harness):
    def __init__(self, version=3, **answers):
        self.profile = profile_for_tests(version)
        self._answers = answers
        super().__init__()

    def make_clef(self):
        return ProfileClef(**self._answers)

    def started(self):
        """Run the first poll so later polls score instead of seeding."""
        self.seed()
        self.sent.clear()
        return self


RELEASE = "https://github.com/org/repo/releases"


class ProfileTests(unittest.TestCase):
    def test_first_poll_seeds_the_backlog_and_records_the_profile_version(self):
        h = ProfileHarness()
        h.poll([part(1, "Cilium old news", ["T1 Homelab"])])
        self.assertEqual(h.clef.calls, [])
        st = h.state()
        self.assertEqual((st["seen"], st["profile_version"], st["pending"]), (["a1"], 3, []))

    def test_changed_profile_version_reseeds_instead_of_scoring_the_backlog(self):
        h = ProfileHarness(version=3).started()
        h.poll([part(2, "Cilium new", ["T1 Homelab"])])
        self.assertEqual(len(h.state()["pending"]), 1)
        h.profile = profile_for_tests(version=4)
        h.news = h.make()
        h.now = T0 + timedelta(hours=1)
        calls = len(h.clef.calls)
        h.poll([part(3, "Cilium backlog", ["T1 Homelab"])])
        st = h.state()
        self.assertEqual(len(h.clef.calls), calls)
        self.assertEqual((st["profile_version"], st["since"]), (4, int(h.now.timestamp())))
        self.assertIn("a3", st["seen"])
        self.assertEqual(len(st["pending"]), 1)  # the earlier match is kept, the backlog is not scored

    def test_pure_release_article_is_queued_with_tier_score_and_group_and_no_clef_call(self):
        h = ProfileHarness().started()
        h.poll([part(2, "v1.2.3", ["T1 Homelab"], site=RELEASE)])
        [item] = h.state()["pending"]
        self.assertEqual((item["tier"], item["score"], item["group"], item["topics"]), (1, 1.0, "Homelab", ["Homelab"]))
        self.assertEqual(h.clef.calls, [])

    def test_marketing_demotes_a_tier1_article_out_of_the_digest(self):
        h = ProfileHarness(marketing=0.9).started()
        h.poll([part(2, "Cilium partner announcement", ["T1 Homelab"])])
        st = h.state()
        self.assertEqual((st["pending"], st["seen"][-1]), ([], "a2"))

    def test_ruled_out_products_never_reach_clef(self):
        h = ProfileHarness().started()
        h.poll([part(2, "TrueNAS and Proxmox", ["T1 Homelab"])])
        self.assertEqual((h.state()["pending"], h.clef.calls), ([], []))

    def test_unmatched_articles_are_marked_seen_without_any_call(self):
        h = ProfileHarness().started()
        h.poll([part(2, "Celebrity news")])
        self.assertEqual((h.state()["seen"][-1], h.clef.calls), ("a2", []))

    def test_time_sensitive_tier3_becomes_an_alert_that_is_sent_once_and_cleared(self):
        h = ProfileHarness(major=0.9, time_sensitive=0.9).started()
        h.poll([part(2, "FOSDEM tickets on sale", ["T3 Other"], url="https://fosdem.example/t")])
        st = h.state()
        self.assertEqual((len(st["alerts"]), st["pending"]), (1, []))
        self.assertTrue(h.news.send_alerts())
        self.assertEqual(len(h.sent), 1)
        self.assertIn("FOSDEM tickets on sale", h.sent[0])
        self.assertIn("https://fosdem.example/t", h.sent[0])
        self.assertEqual(h.state()["alerts"], [])
        self.assertTrue(h.news.send_alerts())
        self.assertEqual(len(h.sent), 1)

    def test_a_failed_alert_send_is_kept_and_retried(self):
        h = ProfileHarness(major=0.9, time_sensitive=0.9).started()
        h.poll([part(2, "FOSDEM tickets on sale", ["T3 Other"])])
        h.send_ok = False
        self.assertFalse(h.news.send_alerts())
        self.assertEqual(len(h.state()["alerts"]), 1)
        h.send_ok = True
        self.assertTrue(h.news.send_alerts())
        self.assertEqual(h.state()["alerts"], [])

    def test_queued_alerts_survive_a_restart(self):
        h = ProfileHarness(major=0.9, time_sensitive=0.9).started()
        h.poll([part(2, "FOSDEM tickets on sale", ["T3 Other"])])
        h.news = h.make()
        self.assertEqual(len(h.news.state["alerts"]), 1)

    def test_major_but_not_urgent_tier3_goes_to_the_digest(self):
        h = ProfileHarness(major=0.9).started()
        h.poll([part(2, "FOSDEM schedule published", ["T3 Other"])])
        st = h.state()
        self.assertEqual((st["alerts"], len(st["pending"]), st["pending"][0]["tier"]), ([], 1, 3))

    def test_clef_failure_leaves_the_article_unseen_and_loses_nothing(self):
        h = ProfileHarness().started()
        h.clef.error = clef.ClefError("OSError")
        self.assertFalse(h.poll([part(2, "Cilium news", ["T1 Homelab"])]))
        st = h.state()
        self.assertNotIn("a2", st["seen"])
        h.clef.error = None
        self.assertTrue(h.poll([part(2, "Cilium news", ["T1 Homelab"])]))
        self.assertEqual(len(h.state()["pending"]), 1)

    def test_custom_topics_still_work_alongside_the_profile(self):
        h = ProfileHarness().started()
        h.say("add", "rust")
        h.poll([part(2, "Rust 2.0 released")])
        [item] = h.state()["pending"]
        self.assertEqual((item["tier"], item["score"], item["topics"], item["group"]), (2, 0.8, ["rust"], None))

    def test_an_article_matching_the_profile_and_a_custom_topic_is_one_item(self):
        h = ProfileHarness().started()
        h.say("add", "cilium")
        h.poll([part(2, "Cilium 1.20", ["T1 Homelab"])])
        [item] = h.state()["pending"]
        self.assertEqual(item["topics"], ["Homelab", "cilium"])

    def test_topics_command_lists_profile_groups_and_custom_topics(self):
        h = ProfileHarness()
        h.say("add", "rust")
        h.sent.clear()
        h.say()
        text = h.sent[0]
        self.assertIn("Profile groups", text)
        self.assertIn("Homelab (tier 1)", text)
        self.assertIn("1. rust", text)

    def test_topics_command_without_custom_topics_still_explains_adding(self):
        h = ProfileHarness()
        h.say()
        self.assertIn("/topics add", h.sent[0])

    def test_bad_profile_version_in_state_starts_empty(self):
        h = ProfileHarness()
        with open(h.path, "w") as f:
            json.dump({"topics": {}, "seen": [], "pending": [], "profile_version": "x"}, f)
        h.news = h.make()
        self.assertIsNone(h.news.state["profile_version"])
        self.assertIsNone(h.news.state["seen"])


class ProfileDigestTests(unittest.TestCase):
    def due(self, h):
        h.news.maybe_digest()
        h.now = T0 + timedelta(hours=1)

    def queue(self, h, items):
        h.news.state["pending"] = items

    def item(self, n, tier, score=1.0, group="G", title=None):
        return {"id": f"p{n}", "title": title or f"title {n}", "url": f"https://x/{n}",
                "topics": [group], "tier": tier, "score": score, "group": group, "custom": []}

    def test_digest_is_grouped_by_tier_then_score_then_newest_first(self):
        h = ProfileHarness().started()
        self.due(h)
        self.queue(h, [self.item(1, 2, 0.8), self.item(2, 1, 1.0), self.item(3, 3, 0.8), self.item(4, 1, 1.0)])
        self.assertTrue(h.news.maybe_digest())
        text = h.sent[-1]
        self.assertIn("News digest: 4 items", text)
        order = [text.index(f"title {n}") for n in (4, 2, 1, 3)]
        self.assertEqual(order, sorted(order))
        self.assertLess(text.index("Tier 1"), text.index("Tier 2"))
        self.assertLess(text.index("Tier 2"), text.index("Tier 3"))
        self.assertEqual(h.state()["pending"], [])

    def test_digest_is_capped_and_the_rest_waits_for_the_next_one(self):
        h = ProfileHarness().started()
        self.due(h)
        self.queue(h, [self.item(n, 1) for n in range(news.DIGEST_MAX_ITEMS + 5)])
        h.news.maybe_digest()
        self.assertEqual(h.sent[-1].count("https://x/"), news.DIGEST_MAX_ITEMS)
        self.assertIn("+5 more", h.sent[-1])
        self.assertEqual(len(h.state()["pending"]), 5)

    def test_char_limit_also_splits_a_digest(self):
        h = ProfileHarness().started()
        self.due(h)
        self.queue(h, [self.item(n, 1, title="t" * 300) for n in range(20)])
        h.news.maybe_digest()
        self.assertLessEqual(len(h.sent[-1]), 4096)
        self.assertGreater(len(h.state()["pending"]), 0)

    def test_failed_send_restores_everything(self):
        h = ProfileHarness().started()
        self.due(h)
        self.queue(h, [self.item(1, 1), self.item(2, 2, 0.8)])
        h.send_ok = False
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(len(h.state()["pending"]), 2)

    def test_legacy_pending_items_without_a_tier_appear_under_tier_2(self):
        h = ProfileHarness().started()
        self.due(h)
        h.say("add", "rust")
        self.queue(h, [{"id": "old", "title": "Old Rust news", "url": "https://x/old", "topics": ["rust"]}])
        self.assertTrue(h.news.maybe_digest())
        self.assertIn("Tier 2", h.sent[-1])
        self.assertIn("Old Rust news", h.sent[-1])

    def test_custom_only_items_are_dropped_once_the_topic_is_unfollowed(self):
        h = ProfileHarness().started()
        self.due(h)
        item = {**self.item(1, 2, 0.8, group=None), "group": None, "topics": ["rust"], "custom": ["rust"]}
        self.queue(h, [item])
        self.assertFalse(h.news.maybe_digest())
        self.assertEqual(h.state()["pending"], [])
        self.assertEqual(len(h.sent), 0)


if __name__ == "__main__":
    unittest.main()
