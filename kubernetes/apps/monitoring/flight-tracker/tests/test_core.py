import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import core  # noqa: E402


class FakeTg:
    def __init__(self):
        self.sent, self.answered, self.asked, self.batches = [], [], [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))

    def answer(self, callback_id):
        self.answered.append(callback_id)

    def updates(self, offset):
        self.asked.append(offset)
        r = self.batches.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Mod:
    def __init__(self, name="m", commands=None, callbacks=None, help=()):
        self.name = name
        self.commands = commands or {}
        self.callbacks = callbacks or {}
        self.help = list(help)


def msg(text, chat=123, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def press(data, sender=123, uid=1, cb="cb1"):
    return {"update_id": uid,
            "callback_query": {"id": cb, "from": {"id": sender}, "data": data}}


class BotCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.tg = FakeTg()
        self.bot = core.Bot(self.tg, "123", self.dir)
        self.calls = []

    def rec(self, tag):
        return lambda arg: self.calls.append((tag, arg))

    def write(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path


class ParseTests(unittest.TestCase):
    def test_command_with_bot_suffix(self):
        self.assertEqual(core.parse_command("/track@mybot LH123 2026-10-05"),
                         ("track", ["LH123", "2026-10-05"]))

    def test_command_is_case_insensitive(self):
        self.assertEqual(core.parse_command("/LIST@MyBot"), ("list", []))

    def test_plain_text_is_not_a_command(self):
        self.assertIsNone(core.parse_command("hello"))
        self.assertIsNone(core.parse_command(None))
        self.assertIsNone(core.parse_command(""))


class TelegramTests(unittest.TestCase):
    def test_send_with_buttons_builds_inline_keyboard(self):
        tg = core.Telegram("TOKEN", "123")
        with mock.patch.object(tg, "_call") as call:
            tg.send("Which flight?", [("LH123 · 2026-10-05", "f:LH123:2026-10-05")])
        method, body = call.call_args[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(body["chat_id"], "123")
        self.assertEqual(body["reply_markup"],
                         {"inline_keyboard": [[{"text": "LH123 · 2026-10-05",
                                                "callback_data": "f:LH123:2026-10-05"}]]})

    def test_send_failure_is_swallowed(self):
        tg = core.Telegram("TOKEN", "123")
        with mock.patch.object(tg, "_call", side_effect=OSError("down")):
            tg.send("hi")  # must not raise


class RegistryTests(BotCase):
    def test_command_routes_to_handler_with_args(self):
        self.bot.register(Mod(commands={"track": self.rec("t")}))
        self.bot.dispatch(msg("/track LH1 2026-10-05"))
        self.assertEqual(self.calls, [("t", ["LH1", "2026-10-05"])])

    def test_uppercase_command_with_bot_suffix_routes(self):
        self.bot.register(Mod(commands={"list": self.rec("l")}))
        self.bot.dispatch(msg("/LIST@MyBot"))
        self.assertEqual(self.calls, [("l", [])])

    def test_unknown_command_sends_combined_help_in_registration_order(self):
        self.bot.register(Mod("a", help=["/a1 - one", "/a2 - two"]))
        self.bot.register(Mod("b", help=["/b1 - three"]))
        self.bot.dispatch(msg("/nope"))
        self.assertEqual(self.tg.sent, [("/a1 - one\n/a2 - two\n/b1 - three", None)])

    def test_duplicate_command_raises_and_registers_nothing(self):
        self.bot.register(Mod("a", commands={"x": self.rec("a")}))
        with self.assertRaises(ValueError):
            self.bot.register(Mod("b", commands={"y": self.rec("b"), "x": self.rec("b")}))
        self.assertEqual(sorted(self.bot.commands), ["x"])
        self.assertEqual([m.name for m in self.bot.modules], ["a"])

    def test_duplicate_callback_prefix_raises(self):
        self.bot.register(Mod("a", callbacks={"f": self.rec("a")}))
        with self.assertRaises(ValueError):
            self.bot.register(Mod("b", callbacks={"f": self.rec("b")}))

    def test_callback_routes_by_prefix_and_is_answered(self):
        self.bot.register(Mod(callbacks={"f": self.rec("c")}))
        self.bot.dispatch(press("f:LH123:2026-10-05"))
        self.assertEqual(self.calls, [("c", ["LH123", "2026-10-05"])])
        self.assertEqual(self.tg.answered, ["cb1"])

    def test_malformed_callback_data_never_reaches_a_handler(self):
        self.bot.register(Mod(callbacks={"f": self.rec("c")}))
        for data in (None, "", ":", "zz:1", "F:LH123:2026-10-05"):
            self.bot.dispatch(press(data))
        self.assertEqual(self.calls, [])

    def test_message_without_text_is_ignored(self):
        self.bot.register(Mod("a", help=["/a1 - one"]))
        self.bot.dispatch({"update_id": 1, "message": {"chat": {"id": 123}}})
        self.assertEqual(self.tg.sent, [])

    def test_plain_text_is_ignored_not_helped(self):
        self.bot.register(Mod("a", help=["/a1 - one"]))
        self.bot.dispatch(msg("hello there"))
        self.assertEqual(self.tg.sent, [])

    def test_stranger_message_and_button_are_ignored(self):
        self.bot.register(Mod(commands={"x": self.rec("m")}, callbacks={"f": self.rec("c")}))
        self.bot.dispatch(msg("/x", chat=999))
        self.bot.dispatch(press("f:A:B", sender=999))
        self.assertEqual((self.calls, self.tg.answered, self.tg.sent), ([], [], []))

    def test_unknown_update_kind_is_ignored(self):
        self.bot.dispatch({"update_id": 1, "edited_message": {"chat": {"id": 123}}})
        self.assertEqual(self.tg.sent, [])


class CtxTests(BotCase):
    def test_state_path_is_per_module_json_in_data_dir(self):
        self.assertEqual(self.bot.ctx("alerts").state_path, os.path.join(self.dir, "alerts.json"))

    def test_send_goes_to_the_owner_chat(self):
        self.bot.ctx("alerts").send("hi", [("a", "b")])
        self.assertEqual(self.tg.sent, [("hi", [("a", "b")])])

    def test_log_is_prefixed_with_the_module_name(self):
        with mock.patch("core.log") as lg:
            self.bot.ctx("alerts").log("started")
        lg.assert_called_once_with("[alerts] started")


class ProcessTests(BotCase):
    def boom(self, args):
        raise RuntimeError("secret-token-text")

    def test_raising_handler_is_logged_by_type_only_and_loop_survives(self):
        self.bot.register(Mod(commands={"boom": self.boom, "ok": self.rec("ok")}))
        with mock.patch("core.log") as lg:
            self.bot.process(msg("/boom", uid=1))
        lg.assert_called_once_with("update failed: RuntimeError")
        self.bot.process(msg("/ok", uid=2))
        self.assertEqual(self.calls, [("ok", [])])

    def test_offset_advances_before_dispatch_even_if_handler_raises(self):
        self.bot.register(Mod(commands={"boom": self.boom}))
        self.bot.process(msg("/boom", uid=5))
        self.assertEqual(self.bot.offset, 6)
        with open(os.path.join(self.dir, "core.json")) as f:
            self.assertEqual(json.load(f), {"offset": 6})


class OffsetTests(BotCase):
    def make(self, legacy=None):
        return core.Bot(self.tg, "123", self.dir, legacy_state=legacy)

    def test_fresh_start_is_zero(self):
        self.assertEqual(self.make().offset, 0)

    def test_first_start_seeds_from_legacy_state_json(self):
        legacy = self.write("state.json", json.dumps({"flights": [], "offset": 42}))
        self.assertEqual(self.make(legacy).offset, 42)

    def test_core_json_wins_over_legacy_once_it_exists(self):
        legacy = self.write("state.json", json.dumps({"offset": 42}))
        self.write("core.json", json.dumps({"offset": 99}))
        self.assertEqual(self.make(legacy).offset, 99)

    def test_offset_survives_restart(self):
        legacy = self.write("state.json", json.dumps({"offset": 42}))
        self.make(legacy).process(msg("/x", uid=50))
        self.assertEqual(self.make(legacy).offset, 51)

    def test_corrupt_core_json_falls_back_to_legacy_not_zero(self):
        legacy = self.write("state.json", json.dumps({"offset": 42}))
        self.write("core.json", "{not json")
        self.assertEqual(self.make(legacy).offset, 42)

    def test_wrong_shape_core_json_falls_back_to_legacy(self):
        legacy = self.write("state.json", json.dumps({"offset": 42}))
        for bad in ("[]", '{"offset": "x"}', '{"nope": 1}', "null"):
            self.write("core.json", bad)
            self.assertEqual(self.make(legacy).offset, 42, bad)

    def test_unreadable_legacy_state_means_zero(self):
        self.assertEqual(self.make(os.path.join(self.dir, "missing.json")).offset, 0)
        legacy = self.write("state.json", "{not json")
        self.assertEqual(self.make(legacy).offset, 0)
        legacy = self.write("state.json", "[]")
        self.assertEqual(self.make(legacy).offset, 0)


class PollTests(BotCase):
    def test_poll_once_processes_updates_and_asks_from_the_new_offset(self):
        self.bot.register(Mod(commands={"x": self.rec("x")}))
        self.tg.batches = [[msg("/x", uid=7)], []]
        self.assertTrue(self.bot.poll_once())
        self.assertTrue(self.bot.poll_once())
        self.assertEqual(self.tg.asked, [0, 8])
        self.assertEqual(self.calls, [("x", [])])

    def test_poll_failure_returns_false_and_does_not_raise(self):
        for err in (OSError("down"), ValueError("bad json"), KeyError("result")):
            self.tg.batches = [err]
            with mock.patch("core.log") as lg:
                self.assertFalse(self.bot.poll_once())
            lg.assert_called_once_with(f"getUpdates failed: {type(err).__name__}")


class StartTests(BotCase):
    def test_start_calls_start_on_modules_that_have_it_and_skips_the_rest(self):
        started = []
        with_start = Mod("a")
        with_start.start = lambda: started.append("a")
        self.bot.register(with_start)
        self.bot.register(Mod("b"))
        self.bot.start()
        self.assertEqual(started, ["a"])


class MainWiringTests(unittest.TestCase):
    def test_main_registers_flights_and_alerts_without_clashes(self):
        import alerts
        import flights
        import status
        import main  # noqa: F401  (import must not run the bot)
        with tempfile.TemporaryDirectory() as d:
            bot = core.Bot(FakeTg(), "123", d)
            bot.register(flights.Flights(bot.ctx("flights"), lambda n, day: [], os.path.join(d, "state.json")))
            bot.register(alerts.Alerts(bot.ctx("alerts"), lambda: []))
            bot.register(status.Status(bot.ctx("status"), lambda q: []))
            bot.handle_message("/alerts off 4h")
        self.assertEqual(sorted(bot.commands), ["alerts", "fetch", "list", "status", "track", "untrack"])
        self.assertEqual(sorted(bot.callbacks), ["f"])
        self.assertTrue(bot.tg.sent[-1][0].startswith("Alerts: OFF until"))


if __name__ == "__main__":
    unittest.main()
