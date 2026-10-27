import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import interests  # noqa: E402

PATH = os.path.join(os.path.dirname(__file__), "..", "app", "news-profile.json")
# Category names created by the OPML import (docs: the owner's FreshRSS).
OPML_CATEGORIES = {
    "T1 HPC/AI infra", "T1 Linux desktop", "T1 Homelab", "T1 Local AI / agents", "T1 Keyboards/electronics",
    "T1 3D printing", "T2 Mobile privacy", "T2 Gaming", "T2 Pokemon TCG", "T2 EU compliance/business",
    "T2 Shopping/hardware", "T2 Investing/career", "T3 events/travel/other", "Core already in OPML"}


class Quiet:
    """A Clef stand-in that never says yes."""

    def noul(self, state, questions):
        return {q: 0.1 for q in questions}


def art(title, categories, summary="", site="", url="https://example.org/a"):
    return {"id": "a", "title": title, "summary": summary, "url": url, "categories": categories, "site": site}


class NewsProfileFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = interests.load_profile(PATH)

    def verdict(self, a):
        return interests.assess(self.p, a, Quiet())

    def test_the_file_loads_with_the_fourteen_groups(self):
        self.assertEqual(len(self.p["groups"]), 14)
        self.assertEqual([t for _, t in interests.group_lines(self.p)], [1] * 6 + [2] * 7 + [3])

    def test_every_group_category_exists_in_the_opml_import(self):
        self.assertEqual(interests.missing_categories(self.p, OPML_CATEGORIES), [])

    def test_homonyms_do_not_match_outside_their_category(self):
        for title in ("Signal analyzer, 10 Gbps USB logic analyzer", "Pixel art fish tank for an LED matrix",
                      "Thread loose in NVIDIA whitepaper", "What matter most to AI resilience"):
            with self.subTest(title=title):
                m = interests.match_group(self.p, art(title, ["T1 Keyboards/electronics"]))
                self.assertTrue(m is None or m[0]["key"] != "mobile_privacy")
                self.assertTrue(m is None or m[0]["key"] != "shopping")

    def test_a_homonym_does_match_inside_its_category(self):
        m = interests.match_group(self.p, art("Introducing Signal Secure Backups", ["T2 Mobile privacy"]))
        self.assertEqual(m[0]["key"], "mobile_privacy")

    def test_github_release_feeds_are_pure_matches(self):
        a = art("v2.10.0", ["T1 Homelab"], site="https://github.com/fluxcd/flux2/releases")
        v = self.verdict(a)
        self.assertEqual((v["deliver"], v["group"]), (True, "Homelab and self-hosting"))

    def test_llama_cpp_build_numbers_are_noise_unless_vulkan(self):
        site = "https://github.com/ggml-org/llama.cpp/releases"
        noisy = self.verdict(art("b11918: opencl: initial support for q3_k (#31172)", ["T1 Local AI / agents"], site=site))
        rescued = self.verdict(art("b11919: vulkan: faster matmul", ["T1 Local AI / agents"], site=site))
        self.assertEqual((noisy["deliver"], rescued["deliver"]), (False, True))

    def test_ruled_out_products_are_dropped(self):
        v = self.verdict(art("Bambu X2D review", ["T1 3D printing"]))
        self.assertEqual((v["deliver"], v["reason"]), (False, "ruled out"))

    def test_expedition_33_patch_notes_are_allowed_but_spoilers_are_not(self):
        class Spoils(Quiet):
            def noul(self, state, questions):
                return {q: 0.9 if q == "spoiler" else 0.1 for q in questions}
        patch = interests.assess(self.p, art("Expedition 33 patch notes 1.4", ["T2 Gaming"]), Spoils())
        plot = interests.assess(self.p, art("Expedition 33 ending explained", ["T2 Gaming"]), Spoils())
        self.assertNotEqual(patch["reason"], "spoiler")
        self.assertEqual(plot["reason"], "spoiler")

    def test_renovate_and_atmosphere_are_common_words_outside_their_categories(self):
        m1 = interests.match_group(self.p, art("Renovate your kitchen this winter", ["Core already in OPML"]))
        self.assertIsNone(m1)
        m2 = interests.match_group(self.p, art("The atmosphere of the new game is great", ["T1 Keyboards/electronics"]))
        self.assertIsNone(m2)

    def test_they_still_match_inside_their_own_category(self):
        m1 = interests.match_group(self.p, art("Renovate bot now supports X", ["T1 Homelab"]))
        self.assertEqual(m1[0]["key"], "homelab")
        m2 = interests.match_group(self.p, art("Atmosphère 1.9 released", ["T2 Gaming"]))
        self.assertEqual(m2[0]["key"], "gaming")
        v3 = self.verdict(art("2.1.0", ["T1 Homelab"], site="https://github.com/renovatebot/renovate/releases"))
        self.assertTrue(v3["deliver"])


if __name__ == "__main__":
    unittest.main()
