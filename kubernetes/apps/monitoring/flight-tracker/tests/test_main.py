import os
import sys
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


if __name__ == "__main__":
    unittest.main()
