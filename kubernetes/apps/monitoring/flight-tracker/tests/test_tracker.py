import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import tracker  # noqa: E402
from samples import leg, payload  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 6, 30, tzinfo=UTC)  # 1h45m before the sample departure


def snap(**kw):
    return tracker.parse_leg(payload(**kw))


class ParseTests(unittest.TestCase):
    def test_command_with_bot_suffix(self):
        self.assertEqual(tracker.parse_command("/track@mybot LH123 2026-10-05"),
                         ("track", ["LH123", "2026-10-05"]))

    def test_plain_text_is_not_a_command(self):
        self.assertIsNone(tracker.parse_command("hello"))
        self.assertIsNone(tracker.parse_command(None))

    def test_leading_zeros_are_dropped(self):
        self.assertEqual(tracker.normalize_flight_number("sk0486"), "SK486")
        self.assertEqual(tracker.normalize_flight_number("SK 486"), "SK486")
        self.assertEqual(tracker.parse_track_args(["sk0486", "2026-09-30"]), ("SK486", "2026-09-30"))

    def test_track_args_normalise_case_and_spaces(self):
        self.assertEqual(tracker.parse_track_args(["lh", "123", "2026-10-05"]),
                         ("LH123", "2026-10-05"))

    def test_track_args_reject_bad_input(self):
        for bad in (["LH123"], ["LH123", "2026-13-01"], ["LH1234567", "2026-10-05"],
                    ["!!", "2026-10-05"], []):
            self.assertIsNone(tracker.parse_track_args(bad), bad)

    def test_ref_with_and_without_date(self):
        self.assertEqual(tracker.parse_ref(["LH123"]), ("LH123", None))
        self.assertEqual(tracker.parse_ref(["lh", "123"]), ("LH123", None))
        self.assertEqual(tracker.parse_ref(["LH123", "2026-10-05"]), ("LH123", "2026-10-05"))
        self.assertIsNone(tracker.parse_ref([]))
        self.assertIsNone(tracker.parse_ref(["!!"]))


class LegTests(unittest.TestCase):
    def test_parses_fields(self):
        s = snap(dep_gate="B24")
        self.assertEqual(s["status"], "Expected")
        self.assertEqual(s["dep"]["gate"], "B24")
        self.assertEqual(s["dep"]["airport"], "FRA")
        self.assertEqual(s["arr"]["airport"], "JFK")
        self.assertIsNone(s["dep"]["est"])

    def test_empty_or_wrong_shape_is_none(self):
        self.assertIsNone(tracker.parse_leg([]))
        self.assertIsNone(tracker.parse_leg(None))
        self.assertIsNone(tracker.parse_leg({"error": "x"}))

    def test_multiple_legs_pick_earliest_departure(self):
        later = leg(dep_sched_utc="2026-10-05 12:00Z", dep_gate="Z9")
        earlier = leg(dep_gate="A12")
        self.assertEqual(tracker.parse_leg([later, earlier])["dep"]["gate"], "A12")

    def test_real_response_parses(self):
        path = os.path.join(os.path.dirname(__file__), "fixtures", "real_lookup.json")
        with open(path) as f:
            s = tracker.parse_leg(json.load(f))
        self.assertIsNotNone(s)
        self.assertTrue(s["dep"]["airport"])


class DiffTests(unittest.TestCase):
    def test_gate_change(self):
        self.assertEqual(tracker.diff_snapshots(snap(), snap(dep_gate="B24")),
                         [("Dep gate", "A12", "B24")])

    def test_no_change(self):
        self.assertEqual(tracker.diff_snapshots(snap(), snap()), [])

    def test_gate_appearing(self):
        self.assertEqual(tracker.diff_snapshots(snap(dep_gate=None), snap(dep_gate="A12")),
                         [("Dep gate", None, "A12")])

    def test_status_change(self):
        self.assertEqual(tracker.diff_snapshots(snap(), snap(status="Boarding")),
                         [("Status", "Expected", "Boarding")])

    def test_real_delay_is_reported(self):
        new = tracker.suppress_jitter(snap(), snap(dep_est_utc="2026-10-05 08:35Z",
                                                  dep_est="2026-10-05 10:35+02:00"))
        self.assertEqual(tracker.diff_snapshots(snap(), new),
                         [("Dep expected", None, "2026-10-05 10:35+02:00")])

    def test_small_wobble_is_ignored(self):
        new = tracker.suppress_jitter(snap(), snap(dep_est_utc="2026-10-05 08:17Z",
                                                   dep_est="2026-10-05 10:17+02:00"))
        self.assertEqual(tracker.diff_snapshots(snap(), new), [])

    def test_wobble_does_not_accumulate(self):
        base = snap()
        step1 = tracker.suppress_jitter(base, snap(dep_est_utc="2026-10-05 08:18Z",
                                                   dep_est="2026-10-05 10:18+02:00"))
        step2 = tracker.suppress_jitter(step1, snap(dep_est_utc="2026-10-05 08:21Z",
                                                    dep_est="2026-10-05 10:21+02:00"))
        self.assertEqual(len(tracker.diff_snapshots(base, step2)), 1)  # 6 min from baseline


class FormatTests(unittest.TestCase):
    def test_snapshot(self):
        self.assertEqual(
            tracker.format_snapshot("LH123", "2026-10-05", snap()),
            "✈ LH123 · 2026-10-05 · Expected\n"
            "FRA → JFK\n"
            "Dep 10-05 10:15 (sched 10-05 10:15) · Terminal 1 · Gate A12\n"
            "Arr 10-05 13:05 (sched 10-05 13:05) · Terminal 4 · Gate — · Belt —")

    def test_changes(self):
        self.assertEqual(
            tracker.format_changes("LH123", "2026-10-05",
                                   [("Dep gate", "A12", "B24"),
                                    ("Dep expected", None, "2026-10-05 10:35+02:00")]),
            "✈ LH123 · 2026-10-05\nDep gate: A12 → B24\nDep expected: — → 10-05 10:35")


class ScheduleTests(unittest.TestCase):
    def interval(self, now, **kw):
        return tracker.poll_interval(now, snap(**kw))

    def test_far_out_polls_every_two_days(self):
        self.assertEqual(self.interval(datetime(2026, 10, 1, 8, 15, tzinfo=UTC)), timedelta(hours=48))

    def test_two_days_out(self):
        self.assertEqual(self.interval(datetime(2026, 10, 3, 8, 15, tzinfo=UTC)), timedelta(hours=12))

    def test_twelve_hours_out(self):
        self.assertEqual(self.interval(datetime(2026, 10, 4, 20, 15, tzinfo=UTC)), timedelta(hours=6))

    def test_five_hours_out(self):
        self.assertEqual(self.interval(datetime(2026, 10, 5, 3, 15, tzinfo=UTC)), timedelta(minutes=60))

    def test_last_two_hours(self):
        self.assertEqual(self.interval(datetime(2026, 10, 5, 7, 0, tzinfo=UTC)), timedelta(minutes=15))

    def test_late_departure_keeps_polling_fast(self):
        self.assertEqual(self.interval(datetime(2026, 10, 5, 8, 30, tzinfo=UTC)), timedelta(minutes=15))

    def test_in_flight(self):
        self.assertEqual(self.interval(datetime(2026, 10, 5, 12, 0, tzinfo=UTC), status="Departed"),
                         timedelta(minutes=120))

    def test_after_expected_arrival(self):
        self.assertEqual(self.interval(datetime(2026, 10, 5, 17, 10, tzinfo=UTC), status="Departed"),
                         timedelta(minutes=15))

    def test_arrived(self):
        self.assertEqual(self.interval(T0, status="Arrived"), timedelta(minutes=15))


class FinishTests(unittest.TestCase):
    def flight(self, seen=None, **kw):
        return {"snapshot": snap(**kw), "arrived_seen": seen.isoformat() if seen else None}

    def test_cancelled(self):
        self.assertEqual(tracker.finish_reason(T0, self.flight(status="Canceled")), "cancelled")

    def test_landed_with_belt(self):
        self.assertEqual(tracker.finish_reason(T0, self.flight(seen=T0, status="Arrived", arr_belt="5")),
                         "landed")

    def test_landed_waits_for_belt_up_to_an_hour(self):
        f = self.flight(seen=T0, status="Arrived")
        self.assertIsNone(tracker.finish_reason(T0 + timedelta(minutes=30), f))
        self.assertEqual(tracker.finish_reason(T0 + timedelta(minutes=61), f), "landed")

    def test_never_reported_arrived_is_dropped_after_window(self):
        f = self.flight(status="Departed")
        self.assertIsNone(tracker.finish_reason(datetime(2026, 10, 5, 22, 0, tzinfo=UTC), f))
        self.assertEqual(tracker.finish_reason(datetime(2026, 10, 5, 23, 30, tzinfo=UTC), f),
                         "no data after the arrival window")

    def test_active_flight_continues(self):
        self.assertIsNone(tracker.finish_reason(T0, self.flight()))


if __name__ == "__main__":
    unittest.main()
