import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

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


class Harness:
    """A Tracker wired to a fake clock, a scripted API and a message log."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "state.json")
        self.now = T0
        self.sent = []       # (text, buttons)
        self.calls = []      # (number, day)
        self.responses = []  # queue of payload lists or exceptions
        self.tracker = self.make()

    def make(self):
        return tracker.Tracker(self.path, self._fetch,
                               lambda text, buttons=None: self.sent.append((text, buttons)),
                               lambda: self.now)

    def _fetch(self, number, day):
        self.calls.append((number, day))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def texts(self):
        return [t for t, _ in self.sent]

    def track(self, number="LH123", **kw):
        self.responses.append(payload(**kw))
        self.tracker.handle_message(f"/track {number} 2026-10-05")


class TrackTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_track_stores_and_replies_with_snapshot(self):
        self.h.responses.append(payload())
        self.h.tracker.handle_message("/track lh 123 2026-10-05")
        self.assertEqual(self.h.calls, [("LH123", "2026-10-05")])
        self.assertIn("✈ LH123 · 2026-10-05 · Expected", self.h.texts()[-1])
        self.assertEqual(len(self.h.tracker.state["flights"]), 1)

    def test_fetch_error_is_reported_and_nothing_stored(self):
        self.h.responses.append(tracker.FetchError("HTTP 500"))
        self.h.tracker.handle_message("/track LH123 2026-10-05")
        self.assertEqual(self.h.texts(), ["Failed to fetch API"])
        self.assertEqual(self.h.tracker.state["flights"], [])

    def test_empty_response_is_a_failure(self):
        self.h.responses.append([])
        self.h.tracker.handle_message("/track LH123 2026-10-05")
        self.assertEqual(self.h.texts(), ["Failed to fetch API"])
        self.assertEqual(self.h.tracker.state["flights"], [])

    def test_duplicate_track_makes_no_call(self):
        self.h.track()
        self.h.calls.clear()
        self.h.tracker.handle_message("/track LH123 2026-10-05")
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.texts()[-1], "Already tracking LH123 2026-10-05")

    def test_past_date_makes_no_call(self):
        self.h.tracker.handle_message("/track LH123 2026-10-01")
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.texts(), ["That date is in the past"])

    def test_bad_usage(self):
        self.h.tracker.handle_message("/track LH123")
        self.assertTrue(self.h.texts()[0].startswith("Usage:"))
        self.assertEqual(self.h.calls, [])


class PollTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.track()
        self.h.sent.clear()
        self.h.calls.clear()

    def poll_at(self, minutes, response):
        self.h.now = T0 + timedelta(minutes=minutes)
        self.h.responses.append(response)
        self.h.tracker.poll_due()

    def test_not_due_makes_no_call(self):
        self.h.now = T0 + timedelta(minutes=14)  # interval at T0 is 15 min
        self.h.tracker.poll_due()
        self.assertEqual(self.h.calls, [])

    def test_gate_change_sends_one_message(self):
        self.poll_at(16, payload(dep_gate="B24"))
        self.assertEqual(len(self.h.sent), 1)
        self.assertIn("Dep gate: A12 → B24", self.h.texts()[0])

    def test_unchanged_poll_is_silent(self):
        self.poll_at(16, payload())
        self.assertEqual(self.h.sent, [])

    def test_small_wobble_is_silent(self):
        self.poll_at(16, payload(dep_est_utc="2026-10-05 08:17Z", dep_est="2026-10-05 10:17+02:00"))
        self.assertEqual(self.h.sent, [])

    def test_expected_time_first_appearing_equal_to_schedule_is_silent(self):
        self.poll_at(16, payload(dep_est_utc="2026-10-05 08:15Z", dep_est="2026-10-05 10:15+02:00"))
        self.assertEqual(self.h.sent, [])

    def test_failure_streak_notifies_once_and_rearms(self):
        for minutes, resp in [(16, tracker.FetchError("x")), (35, tracker.FetchError("x")),
                              (55, payload()), (75, tracker.FetchError("x"))]:
            self.poll_at(minutes, resp)
        self.assertEqual(self.h.texts(), ["Failed to fetch API", "Failed to fetch API"])

    def test_landed_flight_is_alerted_then_dropped(self):
        self.poll_at(16, payload(status="Arrived", arr_belt="5"))
        self.assertIn("Belt: — → 5", self.h.texts()[0])
        self.h.tracker.poll_due()
        self.assertEqual(self.h.texts()[-1], "Stopped tracking LH123 2026-10-05: landed")
        self.assertEqual(self.h.tracker.state["flights"], [])

    def test_cancelled_flight_is_dropped(self):
        self.poll_at(16, payload(status="Canceled"))
        self.h.tracker.poll_due()
        self.assertEqual(self.h.texts()[-1], "Stopped tracking LH123 2026-10-05: cancelled")


class BudgetTests(unittest.TestCase):
    def test_warns_at_80_percent(self):
        h = Harness()
        start = int(tracker.WARN_AT * tracker.MONTHLY_UNITS) - tracker.UNITS_PER_CALL
        h.tracker.state["usage"] = {"month": "2026-10", "units": start,
                                    "warned": False, "paused_notified": False}
        h.track()
        self.assertTrue(any("80%" in t for t in h.texts()))

    def test_paused_skips_scheduled_polls_but_not_fetch(self):
        h = Harness()
        h.track()
        h.tracker.state["usage"]["units"] = int(tracker.PAUSE_AT * tracker.MONTHLY_UNITS)
        h.calls.clear()
        h.now = T0 + timedelta(minutes=16)
        h.tracker.poll_due()
        self.assertEqual(h.calls, [])
        h.responses.append(payload())
        h.tracker.handle_message("/fetch LH123")
        self.assertEqual(h.calls, [("LH123", "2026-10-05")])

    def test_new_month_resets_the_counter(self):
        h = Harness()
        h.tracker.state["usage"] = {"month": "2026-09", "units": 590,
                                    "warned": True, "paused_notified": True}
        h.track()
        usage = h.tracker.state["usage"]
        self.assertEqual((usage["month"], usage["units"], usage["paused_notified"]),
                         ("2026-10", tracker.UNITS_PER_CALL, False))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_fetch_with_nothing_tracked(self):
        self.h.tracker.handle_message("/fetch")
        self.assertEqual(self.h.texts(), ["No tracked flights"])

    def test_fetch_with_one_flight_fetches_directly(self):
        self.h.track()
        self.h.sent.clear()
        self.h.responses.append(payload(dep_gate="B24"))
        self.h.tracker.handle_message("/fetch")
        text, buttons = self.h.sent[-1]
        self.assertIn("Gate B24", text)
        self.assertIsNone(buttons)

    def test_fetch_with_several_flights_shows_buttons_soonest_first(self):
        self.h.track("BA456", dep_sched_utc="2026-10-05 09:00Z", dep_sched="2026-10-05 11:00+02:00")
        self.h.track("LH123")
        self.h.sent.clear()
        self.h.tracker.handle_message("/fetch")
        text, buttons = self.h.sent[-1]
        self.assertEqual(text, "Which flight?")
        self.assertEqual(buttons, [("LH123 · 2026-10-05", "f:LH123:2026-10-05"),
                                   ("BA456 · 2026-10-05", "f:BA456:2026-10-05")])

    def test_fetch_of_untracked_flight_makes_no_call(self):
        self.h.tracker.handle_message("/fetch XX1")
        self.assertEqual(self.h.texts(), ["Not tracked: XX1"])
        self.assertEqual(self.h.calls, [])

    def test_fetch_replies_even_when_unchanged(self):
        self.h.track()
        self.h.sent.clear()
        self.h.responses.append(payload())
        self.h.tracker.handle_message("/fetch LH123")
        self.assertIn("✈ LH123 · 2026-10-05 · Expected", self.h.texts()[-1])

    def test_fetch_failure_keeps_the_flight(self):
        self.h.track()
        self.h.sent.clear()
        self.h.responses.append(tracker.FetchError("x"))
        self.h.tracker.handle_message("/fetch LH123")
        self.assertEqual(self.h.texts(), ["Failed to fetch API"])
        self.assertEqual(len(self.h.tracker.state["flights"]), 1)

    def test_button_tap_fetches(self):
        self.h.track()
        self.h.sent.clear()
        self.h.responses.append(payload())
        self.h.tracker.handle_callback("f:LH123:2026-10-05")
        self.assertIn("✈ LH123", self.h.texts()[-1])

    def test_stale_button_for_dropped_flight(self):
        self.h.tracker.handle_callback("f:LH123:2026-10-05")
        self.assertEqual(self.h.texts(), ["Not tracked: LH123"])
        self.assertEqual(self.h.calls, [])

    def test_garbage_callback_is_ignored(self):
        self.h.tracker.handle_callback("nonsense")
        self.assertEqual(self.h.sent, [])

    def test_untrack(self):
        self.h.track()
        self.h.tracker.handle_message("/untrack LH123")
        self.assertEqual(self.h.tracker.state["flights"], [])
        self.h.tracker.handle_message("/untrack LH123")
        self.assertEqual(self.h.texts()[-1], "Not tracked: LH123")

    def test_list(self):
        self.h.track()
        self.h.tracker.handle_message("/list")
        text = self.h.texts()[-1]
        self.assertIn("LH123 · 2026-10-05 · Expected", text)
        self.assertIn(f"API units this month: {tracker.UNITS_PER_CALL}/{tracker.MONTHLY_UNITS}", text)

    def test_help(self):
        self.h.tracker.handle_message("/help")
        self.assertIn("/track", self.h.texts()[0])


class PersistenceTests(unittest.TestCase):
    def test_state_survives_restart(self):
        h = Harness()
        h.track()
        h.tracker.set_offset(42)
        again = h.make()
        self.assertEqual(len(again.state["flights"]), 1)
        self.assertEqual(again.state["offset"], 42)

    def test_missing_state_starts_empty(self):
        self.assertEqual(Harness().tracker.state["flights"], [])

    def test_corrupt_state_is_moved_aside(self):
        h = Harness()
        with open(h.path, "w") as f:
            f.write("{not json")
        again = h.make()
        self.assertEqual(again.state["flights"], [])
        self.assertTrue(os.path.exists(h.path + ".corrupt"))


def fake_response(body, status=200):
    resp = mock.MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


class FetchFlightTests(unittest.TestCase):
    def call(self, side_effect=None, return_value=None):
        with mock.patch("tracker.urllib.request.urlopen",
                        side_effect=side_effect, return_value=return_value) as m:
            try:
                result = tracker.fetch_flight("LH123", "2026-10-05", "SECRET-KEY")
            finally:
                self.request = m.call_args[0][0]
        return result

    def test_200_returns_the_list(self):
        self.assertEqual(self.call(return_value=fake_response(b'[{"number": "LH 123"}]')),
                         [{"number": "LH 123"}])
        self.assertIn("/flights/number/LH123/2026-10-05", self.request.full_url)
        self.assertEqual(self.request.get_header("X-rapidapi-key"), "SECRET-KEY")

    def test_204_and_empty_body_mean_no_data(self):
        self.assertEqual(self.call(return_value=fake_response(b"", 204)), [])

    def test_404_means_no_data(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        self.assertEqual(self.call(side_effect=err), [])

    def test_server_error_raises_without_leaking_the_key(self):
        err = urllib.error.HTTPError("https://x/?k=SECRET-KEY", 500, "boom", {}, None)
        with self.assertRaises(tracker.FetchError) as ctx:
            self.call(side_effect=err)
        self.assertNotIn("SECRET-KEY", str(ctx.exception))

    def test_network_error_raises(self):
        with self.assertRaises(tracker.FetchError):
            self.call(side_effect=urllib.error.URLError("dns"))

    def test_garbage_body_raises(self):
        with self.assertRaises(tracker.FetchError):
            self.call(return_value=fake_response(b"<html>"))


class TelegramTests(unittest.TestCase):
    def test_send_with_buttons_builds_inline_keyboard(self):
        tg = tracker.Telegram("TOKEN", "123")
        with mock.patch.object(tg, "_call") as call:
            tg.send("Which flight?", [("LH123 · 2026-10-05", "f:LH123:2026-10-05")])
        method, body = call.call_args[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(body["chat_id"], "123")
        self.assertEqual(body["reply_markup"],
                         {"inline_keyboard": [[{"text": "LH123 · 2026-10-05",
                                                "callback_data": "f:LH123:2026-10-05"}]]})

    def test_send_failure_is_swallowed(self):
        tg = tracker.Telegram("TOKEN", "123")
        with mock.patch.object(tg, "_call", side_effect=OSError("down")):
            tg.send("hi")  # must not raise


class Recorder:
    def __init__(self):
        self.messages, self.callbacks, self.answered = [], [], []

    def handle_message(self, text):
        self.messages.append(text)

    def handle_callback(self, data):
        self.callbacks.append(data)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.r = Recorder()

    def go(self, update):
        tracker.dispatch(update, "123", self.r, self.r.answered.append)

    def test_message_from_owner_is_handled(self):
        self.go({"message": {"chat": {"id": 123}, "text": "/list"}})
        self.assertEqual(self.r.messages, ["/list"])

    def test_message_from_stranger_is_ignored(self):
        self.go({"message": {"chat": {"id": 999}, "text": "/list"}})
        self.assertEqual(self.r.messages, [])

    def test_button_from_owner_is_answered_and_handled(self):
        self.go({"callback_query": {"id": "cb1", "from": {"id": 123}, "data": "f:LH123:2026-10-05"}})
        self.assertEqual(self.r.answered, ["cb1"])
        self.assertEqual(self.r.callbacks, ["f:LH123:2026-10-05"])

    def test_button_from_stranger_is_ignored(self):
        self.go({"callback_query": {"id": "cb1", "from": {"id": 999}, "data": "f:LH123:2026-10-05"}})
        self.assertEqual((self.r.answered, self.r.callbacks), ([], []))

    def test_unknown_update_kinds_are_ignored(self):
        self.go({"edited_message": {"chat": {"id": 123}}})
        self.assertEqual((self.r.messages, self.r.callbacks), ([], []))


if __name__ == "__main__":
    unittest.main()
