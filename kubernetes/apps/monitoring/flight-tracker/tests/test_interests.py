import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import clef  # noqa: E402
import interests  # noqa: E402

RAW = {
    "version": 3,
    "tiers": {"1": 1.0, "2": 0.6, "3": 0.3},
    "deliver_min": 0.7,
    "yes_at": 0.5,
    "adjust": {"marketing": 0.6, "substantive": 0.2, "major": 0.5, "paywalled": 0.5, "build_noise": 0.5},
    "pure_feed_pattern": r"github\.com/[^/]+/[^/]+/(releases|tags)",
    "groups": {
        "homelab": {"title": "Homelab", "tier": 1, "categories": ["T1 Homelab"],
                    "keywords": ["Proxmox", "Cilium"], "scoped_keywords": ["Flux"]},
        "ai": {"title": "Local AI", "tier": 1, "categories": ["T1 AI"],
               "keywords": ["llama.cpp", "GGUF"], "scoped_keywords": ["Vulkan"]},
        "privacy": {"title": "Privacy", "tier": 2, "categories": ["T2 Privacy"],
                    "keywords": ["GrapheneOS"], "scoped_keywords": ["Signal", "Matrix"]},
        "gaming": {"title": "Gaming", "tier": 2, "categories": ["T2 Gaming"],
                   "keywords": ["Expedition 33"], "scoped_keywords": []},
        "other": {"title": "Situational", "tier": 3, "categories": ["T3 Other"],
                  "keywords": ["FOSDEM", "Steel Ball Run"], "scoped_keywords": ["strike"]},
    },
    "ruled_out": ["TrueNAS"],
    "spoilers": {"topics": ["Expedition 33", "Steel Ball Run"], "allow": ["patch notes", "release date"]},
    "paywalled_domains": ["ft.com"],
    "build_noise": {"title_regex": r"^b\d{3,}", "unless_keywords": ["Vulkan"]},
    "questions": {"marketing": "M?", "substantive": "S?", "major": "J?", "time_sensitive": "T?", "spoiler": "X?"},
}


def make(**changes):
    raw = copy.deepcopy(RAW)
    raw.update(changes)
    return interests.compile_profile(raw)


def article(title="t", summary="", url="https://x.example/a", categories=(), site=""):
    return {"id": "a", "title": title, "summary": summary, "url": url,
            "categories": list(categories), "site": site}


class FakeClef:
    """Answers each question id with a configured probability (default 0.1) and records the calls."""

    def __init__(self, **answers):
        self.answers, self.calls, self.error = answers, [], None

    def noul(self, state, questions):
        self.calls.append(sorted(questions))
        if self.error:
            raise self.error
        return {q: self.answers.get(q, 0.1) for q in questions}


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.p = make()

    def group(self, a):
        m = interests.match_group(self.p, a)
        return m and (m[0]["key"], m[1])

    def test_keyword_matches_whole_words_case_insensitively(self):
        self.assertEqual(self.group(article("New proxmox release")), ("homelab", False))
        self.assertIsNone(self.group(article("Proxmoxy things")))

    def test_regex_characters_in_keywords_are_literal(self):
        self.assertEqual(self.group(article("llama.cpp adds support")), ("ai", False))
        self.assertIsNone(self.group(article("llamaXcpp adds support")))

    def test_scoped_keyword_only_counts_inside_the_group_category(self):
        self.assertIsNone(self.group(article("Signal analyzer for ADCs", categories=["T1 AI"])))
        self.assertEqual(self.group(article("Signal adds backups", categories=["T2 Privacy"])), ("privacy", False))

    def test_pure_release_feed_matches_its_category_without_keywords(self):
        a = article("v1.2.3", site="https://github.com/org/repo/releases", categories=["T1 Homelab"])
        self.assertEqual(self.group(a), ("homelab", True))

    def test_release_feed_in_an_unrelated_category_does_not_match(self):
        a = article("v1.2.3", site="https://github.com/org/repo/releases", categories=["Core"])
        self.assertIsNone(self.group(a))

    def test_lowest_tier_wins_and_ties_go_to_the_first_group(self):
        a = article("Proxmox on GrapheneOS and llama.cpp and FOSDEM")
        self.assertEqual(self.group(a), ("homelab", False))

    def test_no_match(self):
        self.assertIsNone(self.group(article("Celebrity news")))


class AssessTests(unittest.TestCase):
    def setUp(self):
        self.p = make()

    def run_assess(self, a, clef_obj=None):
        c = clef_obj or FakeClef()
        return interests.assess(self.p, a, c), c

    def test_no_match_is_not_delivered_and_asks_nothing(self):
        v, c = self.run_assess(article("Celebrity news"))
        self.assertFalse(v["deliver"])
        self.assertEqual(c.calls, [])

    def test_tier1_keyword_article_is_delivered_after_one_marketing_check(self):
        v, c = self.run_assess(article("Cilium 1.20 adds things"))
        self.assertEqual((v["deliver"], v["score"], v["tier"], v["group"]), (True, 1.0, 1, "Homelab"))
        self.assertEqual(c.calls, [["marketing"]])

    def test_marketing_demotes_tier1_below_the_threshold(self):
        v, _ = self.run_assess(article("Cilium partner announcement"), FakeClef(marketing=0.9))
        self.assertEqual((v["deliver"], v["score"]), (False, 0.6))

    def test_pure_release_feed_skips_clef_entirely(self):
        a = article("v1.2.3", site="https://github.com/org/repo/releases", categories=["T1 Homelab"])
        v, c = self.run_assess(a)
        self.assertEqual((v["deliver"], v["score"]), (True, 1.0))
        self.assertEqual(c.calls, [])

    def test_tier2_release_feed_passes_without_clef(self):
        a = article("20261027", site="https://github.com/o/GrapheneOS/releases", categories=["T2 Privacy"])
        v, c = self.run_assess(a)
        self.assertEqual((v["deliver"], v["score"]), (True, 0.8))
        self.assertEqual(c.calls, [])

    def test_tier2_needs_a_substantive_yes(self):
        a = article("GrapheneOS news", categories=["T2 Privacy"])
        no, c = self.run_assess(a)
        self.assertEqual((no["deliver"], no["score"]), (False, 0.6))
        self.assertEqual(c.calls, [["marketing", "substantive"]])
        yes, _ = self.run_assess(a, FakeClef(substantive=0.9))
        self.assertEqual((yes["deliver"], yes["score"]), (True, 0.8))

    def test_tier3_needs_major_news_and_alerts_only_when_time_sensitive(self):
        a = article("FOSDEM tickets", categories=["T3 Other"])
        quiet, c = self.run_assess(a)
        self.assertEqual((quiet["deliver"], quiet["alert"], quiet["score"]), (False, False, 0.3))
        self.assertEqual(c.calls, [["major", "marketing", "time_sensitive"]])
        major, _ = self.run_assess(a, FakeClef(major=0.9))
        self.assertEqual((major["deliver"], major["alert"], major["score"]), (True, False, 0.8))
        urgent, _ = self.run_assess(a, FakeClef(major=0.9, time_sensitive=0.9))
        self.assertEqual((urgent["deliver"], urgent["alert"]), (True, True))

    def test_time_sensitive_alone_does_not_alert_below_the_threshold(self):
        v, _ = self.run_assess(article("FOSDEM tickets", categories=["T3 Other"]), FakeClef(time_sensitive=0.9))
        self.assertEqual((v["deliver"], v["alert"]), (False, False))

    def test_ruled_out_term_drops_without_asking_clef(self):
        v, c = self.run_assess(article("TrueNAS and Proxmox"))
        self.assertEqual((v["deliver"], v["reason"]), (False, "ruled out"))
        self.assertEqual(c.calls, [])

    def test_spoiler_yes_drops_and_asks_first(self):
        a = article("Expedition 33 ending explained", categories=["T2 Gaming"])
        v, c = self.run_assess(a, FakeClef(spoiler=0.9, substantive=0.9))
        self.assertEqual((v["deliver"], v["reason"]), (False, "spoiler"))
        self.assertEqual(c.calls, [["spoiler"]])

    def test_spoiler_check_passes_then_normal_scoring_follows(self):
        a = article("Expedition 33 secrets", categories=["T2 Gaming"])
        v, c = self.run_assess(a, FakeClef(substantive=0.9))
        self.assertTrue(v["deliver"])
        self.assertEqual(c.calls, [["spoiler"], ["marketing", "substantive"]])

    def test_spoiler_allow_term_skips_the_spoiler_question(self):
        a = article("Expedition 33 patch notes 1.2", categories=["T2 Gaming"])
        _, c = self.run_assess(a, FakeClef(substantive=0.9))
        self.assertEqual(c.calls, [["marketing", "substantive"]])

    def test_paywalled_host_and_its_subdomains_are_halved(self):
        for url in ("https://ft.com/x", "https://www.ft.com/x"):
            v, _ = self.run_assess(article("Cilium story", url=url))
            self.assertEqual((v["score"], v["deliver"]), (0.5, False), url)
        v, _ = self.run_assess(article("Cilium story", url="https://soft.com/x"))
        self.assertTrue(v["deliver"])

    def test_build_noise_is_halved_unless_a_keyword_rescues_it(self):
        site = "https://github.com/ggml-org/llama.cpp/releases"
        noisy = article("b11918: opencl: q3_k (#31172)", site=site, categories=["T1 AI"])
        v, _ = self.run_assess(noisy)
        self.assertEqual((v["deliver"], v["score"]), (False, 0.5))
        rescued = article("b11919: Vulkan: faster matmul", site=site, categories=["T1 AI"])
        v, _ = self.run_assess(rescued)
        self.assertEqual((v["deliver"], v["score"]), (True, 1.0))

    def test_threshold_boundary_is_inclusive(self):
        p = make(deliver_min=0.6)
        v = interests.assess(p, article("Cilium partner announcement"), FakeClef(marketing=0.9))
        self.assertTrue(v["deliver"])

    def test_clef_errors_propagate_so_the_caller_can_retry(self):
        c = FakeClef()
        c.error = clef.ClefError("OSError")
        with self.assertRaises(clef.ClefError):
            interests.assess(self.p, article("Cilium news"), c)

    def test_a_malformed_article_url_does_not_break_scoring(self):
        a = article("Cilium 1.20 adds things", url="http://[oops/x")
        v, c = self.run_assess(a)
        self.assertEqual((v["deliver"], v["score"], v["tier"], v["group"]), (True, 1.0, 1, "Homelab"))
        self.assertEqual(c.calls, [["marketing"]])


class HelperTests(unittest.TestCase):
    def test_group_lines_keep_profile_order(self):
        self.assertEqual(interests.group_lines(make())[:2], [("Homelab", 1), ("Local AI", 1)])

    def test_missing_categories_lists_unused_profile_categories(self):
        self.assertEqual(interests.missing_categories(make(), ["T1 Homelab", "Other"]),
                         ["T1 AI", "T2 Gaming", "T2 Privacy", "T3 Other"])


class ValidationTests(unittest.TestCase):
    def bad(self, mutate):
        raw = copy.deepcopy(RAW)
        mutate(raw)
        with self.assertRaises(interests.ProfileError):
            interests.compile_profile(raw)

    def test_each_kind_of_damage_is_a_profile_error(self):
        cases = [
            lambda r: r.pop("version"), lambda r: r.update(version="1"), lambda r: r.update(version=True),
            lambda r: r["tiers"].pop("2"), lambda r: r["tiers"].update({"1": "high"}),
            lambda r: r["tiers"].update({"1": 10**400}),
            lambda r: r.pop("deliver_min"), lambda r: r.pop("yes_at"),
            lambda r: r["adjust"].pop("marketing"),
            lambda r: r["questions"].pop("spoiler"), lambda r: r["questions"].update(major=""),
            lambda r: r.update(groups={}), lambda r: r.update(groups=[]),
            lambda r: r["groups"]["ai"].update(tier=4), lambda r: r["groups"]["ai"].update(tier=True),
            lambda r: r["groups"]["ai"].update(title=""), lambda r: r["groups"]["ai"].update(categories=[]),
            lambda r: r["groups"]["ai"].update(keywords="x"), lambda r: r["groups"]["ai"].update(keywords=[1]),
            lambda r: r.update(pure_feed_pattern="("), lambda r: r.pop("pure_feed_pattern"),
            lambda r: r.update(pure_feed_pattern="a{99999999999}"),
            lambda r: r["build_noise"].update(title_regex="["),
            lambda r: r["build_noise"].update(title_regex="(" * 1000 + ")" * 1000),
            lambda r: r.update(spoilers=[]),
            lambda r: r.update(ruled_out="TrueNAS"), lambda r: r.update(paywalled_domains=[3]),
        ]
        for i, mutate in enumerate(cases):
            with self.subTest(case=i):
                self.bad(mutate)

    def test_not_an_object(self):
        for raw in ([], "x", 3, None):
            with self.assertRaises(interests.ProfileError):
                interests.compile_profile(raw)

    def test_load_profile_reports_unreadable_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(interests.ProfileError):
                interests.load_profile(os.path.join(d, "missing.json"))
            path = os.path.join(d, "bad.json")
            with open(path, "w") as f:
                f.write("{nope")
            with self.assertRaises(interests.ProfileError):
                interests.load_profile(path)
            with open(path, "w") as f:
                json.dump(RAW, f)
            self.assertEqual(interests.load_profile(path)["version"], 3)

    def test_optional_lists_may_be_omitted(self):
        raw = copy.deepcopy(RAW)
        for key in ("ruled_out", "paywalled_domains"):
            raw.pop(key)
        raw["spoilers"] = {}
        raw["build_noise"].pop("unless_keywords")
        raw["groups"]["ai"].pop("scoped_keywords")
        interests.compile_profile(raw)

    def test_deliver_min_nan_and_inf_rejected(self):
        for bad_val in (float("nan"), float("inf"), float("-inf")):
            raw = copy.deepcopy(RAW)
            raw["deliver_min"] = bad_val
            with self.assertRaises(interests.ProfileError):
                interests.compile_profile(raw)

    def test_load_profile_rejects_deeply_nested_json(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested.json")
            with open(path, "w") as f:
                f.write("[" * 200000 + "]" * 200000)
            with self.assertRaises(interests.ProfileError):
                interests.load_profile(path)


if __name__ == "__main__":
    unittest.main()
