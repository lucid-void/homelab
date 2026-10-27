import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import main  # noqa: E402


class DigestHoursTests(unittest.TestCase):
    def test_valid_lists_are_sorted_and_deduplicated(self):
        for text, want in (("8,18", (8, 18)), (" 6 , 18 ", (6, 18)), ("18,8", (8, 18)),
                           ("8,8,18", (8, 18)), ("0,23", (0, 23)), ("7", (7,))):
            with self.subTest(text=text):
                self.assertEqual(main.parse_digest_hours(text), want)

    def test_any_problem_logs_and_falls_back(self):
        for text in ("8,,18", "x", "25", "-1", "", "8,x", "24", "1.5"):
            with self.subTest(text=text), mock.patch("main.core.log") as lg:
                self.assertEqual(main.parse_digest_hours(text), (8, 18))
                lg.assert_called_once()


PROFILE = os.path.join(os.path.dirname(__file__), "..", "app", "news-profile.json")


class NewsProfileTests(unittest.TestCase):
    def test_the_shipped_profile_loads(self):
        self.assertEqual(main.load_news_profile(PROFILE)["version"], 1)

    def test_a_missing_or_broken_profile_turns_news_off_with_a_log_line(self):
        with tempfile.TemporaryDirectory() as d:
            broken = os.path.join(d, "broken.json")
            with open(broken, "w") as f:
                f.write("{nope")
            for path in (os.path.join(d, "missing.json"), broken):
                with self.subTest(path=path), mock.patch("main.core.log") as lg:
                    self.assertIsNone(main.load_news_profile(path))
                    lg.assert_called_once()

    def test_unused_profile_categories_are_logged_once(self):
        profile = main.load_news_profile(PROFILE)
        source = mock.Mock()
        source.labels.return_value = {"T1 Homelab"}
        with mock.patch("main.core.log") as lg:
            main.warn_unused_categories(profile, source)
        lg.assert_called_once()
        self.assertIn("T1 HPC/AI infra", lg.call_args[0][0])

    def test_nothing_is_logged_when_every_category_has_a_feed(self):
        profile = main.load_news_profile(PROFILE)
        source = mock.Mock()
        source.labels.return_value = {c for g in profile["groups"] for c in g["categories"]}
        with mock.patch("main.core.log") as lg:
            main.warn_unused_categories(profile, source)
        lg.assert_not_called()

    def test_a_freshrss_failure_while_checking_categories_never_raises(self):
        source = mock.Mock()
        source.labels.side_effect = main.freshrss.FreshRSSError("OSError")
        with mock.patch("main.core.log") as lg:
            main.warn_unused_categories(main.load_news_profile(PROFILE), source)
        lg.assert_called_once()


if __name__ == "__main__":
    unittest.main()
