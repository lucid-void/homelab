# Telegram bot core + modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (this plan is run inline by one CLI session inside tmux) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `flight-tracker` into a stdlib-only bot core plus pluggable modules, and move the flight tracker onto it with zero user-visible behavior change.

**Architecture:** `core.py` owns the Telegram client, owner check, offset (`core.json`), command/callback registry and receive loop. `flights.py` is today's `tracker.py` with a thin `Flights` module wrapper. `main.py` wires them. Same Deployment, PVC, secrets and app name.

**Tech Stack:** Python 3 stdlib only (`unittest`), Kustomize `configMapGenerator`, Flux.

**Spec:** `docs/superpowers/specs/2026-09-30-telegram-bot-core-design.md` (read it first, it is short).

## Handoff: running this in tmux (read before anything else)

Start or reattach (connection may drop, work survives):

```bash
tmux new -s telegram-bot -c ~/repos/Homelab    # first time
tmux attach -t telegram-bot                      # after a disconnect
```

Inside tmux run `claude` and paste this prompt:

> Read `docs/superpowers/plans/2026-09-30-telegram-bot-core.md` and the spec it links. Follow superpowers:executing-plans. Resume at the first unchecked `- [ ]` step. Commit at the end of every task and tick that task's boxes in the same commit. Stop before Task 6 step 3 (push) and ask me.

**Resume protocol (for the agent after any interruption):**

1. `git status --short` and `git log --oneline -8`.
2. `grep -n '^\s*- \[ \]' docs/superpowers/plans/2026-09-30-telegram-bot-core.md | head -3` shows the next step.
3. Run the test suite (command in Global Constraints). If it is red on entry, the last task was interrupted mid-edit: `git diff` and finish or `git checkout -- <file>` that task's files, then redo the task.
4. Never restart a task from scratch if its commit already exists in `git log`.

**Repo rules that apply to this run** (from `.claude/CLAUDE.md`):

- Answer in the ADHD output shape (`.agents/skills/i-have-adhd/SKILL.md`): next action first, numbered steps, one concrete next step at the end.
- The working tree has **unrelated uncommitted changes** (`design/decisions/jellyfin.md`, `design/docs/storage.md`). Never `git add -A` or `git commit -a`. Add explicit paths only.
- k8s tools are not on `PATH`: use `mise exec -- kubectl|flux|...`. `kubectl` is diagnostics only; never `kubectl apply` a config change.
- Never write a raw `Secret`; this plan touches no secrets.
- Never record a deployed version number in docs.

## Global Constraints

- Stdlib-only Python; no `pip install`; the container has a read-only root filesystem (`PYTHONDONTWRITEBYTECODE=1` is already set).
- `flight-tracker` is the only `getUpdates` consumer of the bot token; a second poller gets `409`.
- Code ships flat in one ConfigMap at `/scripts` via `configMapGenerator` `files:`; modules import each other with plain `import core` / `import flights`.
- Do NOT rename the app, Kustomization, Deployment, PVC (`flight-tracker-data`) or secrets. A rename makes Flux prune the PVC holding live flight state.
- Deployment strategy stays `Recreate`. `telegram-secret` stays owned by `gotify-telegram`; do not touch the bridge.
- Flights keeps `/data/state.json` with the same shape (`STATE_PATH` env, already set in the Deployment). The core keeps the offset in `/data/core.json`.
- Log exception **type only**, never exception text (it embeds credentials).
- Inline-button data keeps the `f:NUMBER:DAY` format.
- Test command (run from repo root, must end `OK`): `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
- Baseline before any change: 88 tests, all pass.

## Review Focus

Inputs the spec implies but a straight implementation would not test. Each has a test in the task named in brackets.

1. First start after the upgrade: no `core.json`, legacy `state.json` has `offset: 42`. Offset must be 42, not 0, or Telegram replays old `/track` commands. [Task 1]
2. `core.json` corrupt, or valid JSON of the wrong shape. Must fall back to the legacy offset, not 0, and not crash. [Task 1]
3. A message with no `text` (photo, sticker) must be ignored silently, no help spam. [Task 1]
4. Malformed or foreign button data (`None`, `""`, `"zz:1"`, `"f:LH123"` with a missing part) must never crash and must not fetch anything. [Task 1 core side, Task 2 flights arity]
5. `/LIST@MyBot` (uppercase, bot suffix) must route to `list`. [Task 1]

---

### Task 0: Branch and baseline

**Files:** none changed.

- [x] **Step 1: Create the working branch** (the plan and spec are already committed on `main`)

```bash
cd ~/repos/Homelab
git switch -c feat/telegram-bot-core
git status --short
```

Expected: `M design/decisions/jellyfin.md` and `M design/docs/storage.md` carried over. Leave them alone.

- [x] **Step 2: Confirm the baseline is green**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `Ran 88 tests` ... `OK`

- [x] **Step 3: Find every reference to the files being renamed**

```bash
grep -rn "tracker\.py\|test_tracker" --exclude-dir=.git --exclude-dir=__pycache__ . | grep -v "docs/superpowers"
```

Write down each hit outside `kubernetes/apps/monitoring/flight-tracker/`. Task 5 fixes them. Expected: possibly none.

---

### Task 1: `core.py` with its tests

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/core.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_core.py`

**Interfaces:**
- Produces (used by Tasks 2 and 3):
  - `core.log(msg: str) -> None`
  - `core.parse_command(text: str | None) -> tuple[str, list[str]] | None`
  - `core.Telegram(token, chat_id)` with `send(text, buttons=None)`, `answer(callback_id)`, `updates(offset) -> list`
  - `core.Ctx` with attributes `send`, `state_path` and method `log(msg)`
  - `core.Bot(tg, chat_id, data_dir, legacy_state=None)` with `.offset`, `.modules`, `.commands`, `.callbacks`, `register(module)`, `ctx(name) -> Ctx`, `help_text() -> str`, `handle_message(text)`, `handle_callback(data)`, `dispatch(update)`, `process(update)`, `poll_once() -> bool`, `start()`, `run()`
  - Module protocol: attributes `name: str`, `help: list[str]`, `commands: dict[str, fn(args: list[str])]`, `callbacks: dict[str, fn(parts: list[str])]`, optional `start()`
- Note: `tracker.py` is untouched in this task and still has its own copies of `parse_command`, `Telegram` and `dispatch`. That duplication ends in Task 2.

- [x] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_core.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -p 'test_core.py'`
Expected: `ModuleNotFoundError: No module named 'core'`

- [x] **Step 3: Write `core.py`**

Create `kubernetes/apps/monitoring/flight-tracker/app/core.py`:

```python
#!/usr/bin/env python3
"""Telegram bot core: owner-only command and button routing for pluggable modules.

A module is any object with:
  name      str, used for its state file (/data/<name>.json) and log prefix
  help      list of help lines
  commands  {"track": fn(args)}   args = words after the command
  callbacks {"f": fn(parts)}      parts = callback data split on ":" without the prefix
  start()   optional, spawn background threads here
"""
import json
import os
import time
import urllib.request


def log(msg):
    print(msg, flush=True)


def parse_command(text):
    parts = (text or "").split()
    if not parts or not parts[0].startswith("/"):
        return None
    return parts[0][1:].split("@")[0].lower(), parts[1:]


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    def _call(self, method, body, timeout=20):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)

    def send(self, text, buttons=None):
        body = {"chat_id": self.chat_id, "text": text}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data}] for label, data in buttons]}
        try:
            self._call("sendMessage", body)
        except (OSError, ValueError) as e:
            log(f"send failed: {type(e).__name__}")

    def answer(self, callback_id):
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        except (OSError, ValueError) as e:
            log(f"answer failed: {type(e).__name__}")

    def updates(self, offset):
        body = {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}
        return self._call("getUpdates", body, timeout=65)["result"]


class Ctx:
    """What a module gets from the bot: send, log, and a state file path of its own."""

    def __init__(self, name, send, state_path):
        self.name, self.send, self.state_path = name, send, state_path

    def log(self, msg):
        log(f"[{self.name}] {msg}")


class Bot:
    def __init__(self, tg, chat_id, data_dir, legacy_state=None):
        self.tg, self.chat_id, self.data_dir = tg, str(chat_id), data_dir
        self.modules, self.commands, self.callbacks = [], {}, {}
        self._core_path = os.path.join(data_dir, "core.json")
        self._legacy_state = legacy_state
        self.offset = self._load_offset()

    # -- offset --

    def _load_offset(self):
        try:
            with open(self._core_path) as f:
                return int(json.load(f)["offset"])
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError):  # JSONDecodeError is a ValueError
            log("core state unreadable, seeding offset from legacy state")
        return self._legacy_offset()

    def _legacy_offset(self):
        """First start after the split: keep the offset the old single-file bot had reached."""
        if not self._legacy_state:
            return 0
        try:
            with open(self._legacy_state) as f:
                return int(json.load(f).get("offset", 0))
        except (OSError, ValueError, AttributeError, TypeError):
            return 0

    def _save_offset(self):
        tmp = self._core_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"offset": self.offset}, f)
        os.replace(tmp, self._core_path)

    # -- registry --

    def register(self, module):
        for name in module.commands:
            if name in self.commands:
                raise ValueError(f"duplicate command: /{name}")
        for prefix in module.callbacks:
            if prefix in self.callbacks:
                raise ValueError(f"duplicate callback prefix: {prefix}")
        self.commands.update(module.commands)
        self.callbacks.update(module.callbacks)
        self.modules.append(module)

    def ctx(self, name):
        return Ctx(name, self.tg.send, os.path.join(self.data_dir, f"{name}.json"))

    def help_text(self):
        return "\n".join(line for m in self.modules for line in m.help)

    def start(self):
        for m in self.modules:
            start = getattr(m, "start", None)
            if start:
                start()

    # -- routing --

    def handle_message(self, text):
        cmd = parse_command(text)
        if cmd is None:
            return
        name, args = cmd
        handler = self.commands.get(name)
        if handler is None:
            self.tg.send(self.help_text())
        else:
            handler(args)

    def handle_callback(self, data):
        prefix, *parts = (data or "").split(":")
        handler = self.callbacks.get(prefix)
        if handler is not None:
            handler(parts)

    def dispatch(self, update):
        """Route one Telegram update. Anything not from the owner's chat is ignored."""
        msg = update.get("message")
        if msg and str(msg.get("chat", {}).get("id")) == self.chat_id:
            self.handle_message(msg.get("text"))
            return
        cq = update.get("callback_query")
        if cq and str(cq.get("from", {}).get("id")) == self.chat_id:
            self.tg.answer(cq.get("id"))
            self.handle_callback(cq.get("data"))

    def process(self, update):
        self.offset = update["update_id"] + 1
        self._save_offset()
        try:
            self.dispatch(update)
        except Exception as e:  # a bad update must not kill the bot
            log(f"update failed: {type(e).__name__}")

    # -- loop --

    def poll_once(self):
        try:
            updates = self.tg.updates(self.offset)
        except (OSError, ValueError, KeyError) as e:
            log(f"getUpdates failed: {type(e).__name__}")
            return False
        for update in updates:
            self.process(update)
        return True

    def run(self):
        while True:
            if not self.poll_once():
                time.sleep(30)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`. The old `test_tracker.py` still passes (88 tests) plus the new core tests.

- [x] **Step 5: Commit** (also tick this task's boxes in this plan file first)

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/core.py \
        kubernetes/apps/monitoring/flight-tracker/tests/test_core.py \
        docs/superpowers/plans/2026-09-30-telegram-bot-core.md
git commit -m "feat(telegram-bot): add core with module registry and persisted offset

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Move the tracker onto the core as `flights.py`

**Files:**
- Rename: `app/tracker.py` → `app/flights.py` (with `git mv`, history follows)
- Rename: `tests/test_tracker.py` → `tests/test_flights.py`
- Modify both (paths relative to `kubernetes/apps/monitoring/flight-tracker/`)

**Interfaces:**
- Consumes: `core.log`, `core.Bot`, `core.Ctx` from Task 1.
- Produces (used by Task 3): `flights.Flights(ctx, fetch, state_path, now=<utc clock>)` implementing the module protocol (`name = "flights"`, `help`, `commands` = `track`/`untrack`/`list`/`fetch`, `callbacks` = `{"f": ...}`, `start()`), attribute `.tracker`; `flights.fetch_flight(number, day, key, timeout=20)` unchanged.

- [x] **Step 1: Rename with git mv**

```bash
cd ~/repos/Homelab/kubernetes/apps/monitoring/flight-tracker
git mv app/tracker.py app/flights.py
git mv tests/test_tracker.py tests/test_flights.py
```

- [x] **Step 2: Strip the parts that moved to the core, in `app/flights.py`**

Make these edits (line numbers are from the original `tracker.py`; match on the text):

1. Delete the whole `HELP = ( ... )` block (lines 26-32).
2. Replace
   ```python
   def log(msg):
       print(msg, flush=True)
   ```
   (lines 35-36) with nothing, and add `from core import log` directly below the last stdlib import (`from datetime import ...`).
3. Delete `def parse_command(text): ...` (lines 41-45).
4. In `Tracker._load`, delete the line `state.setdefault("offset", 0)`.
5. In `Tracker`, delete the `set_offset` method (the offset now lives in the core).
6. In `Tracker`, delete `handle_message` and `handle_callback` (the `# -- commands --` section keeps `_track`, `_untrack`, `_list`, `_fetch_cmd`).
7. Delete from `class Telegram:` through the end of the file **except** `scheduler_loop`. That removes `Telegram`, `dispatch`, `receiver_loop`, `run` and the `if __name__ == "__main__":` block. Keep the `# ---------- main ----------` header renamed to `# ---------- module ----------` and keep `scheduler_loop` exactly as is.

- [x] **Step 3: Add the `Flights` module at the end of `app/flights.py`**

```python
class Flights:
    """The flight tracker as a bot module: wires Tracker's handlers into the core registry."""

    name = "flights"
    help = [
        "/track LH123 2026-10-05 - start tracking a flight",
        "/untrack LH123 - stop tracking",
        "/list - tracked flights",
        "/fetch - refresh a tracked flight now (pick from the list)",
        "/fetch LH123 - refresh that flight now",
    ]

    def __init__(self, ctx, fetch, state_path, now=lambda: datetime.now(UTC)):
        self.tracker = Tracker(state_path, fetch, ctx.send, now)
        self.commands = {
            "track": self._locked(self.tracker._track),
            "untrack": self._locked(self.tracker._untrack),
            "list": self._locked(lambda args: self.tracker._list()),
            "fetch": self._locked(self.tracker._fetch_cmd),
        }
        self.callbacks = {"f": self._fetch_button}

    def _locked(self, fn):
        def run(args):
            with self.tracker.lock:
                fn(args)
        return run

    def _fetch_button(self, parts):
        if len(parts) != 2:
            return
        number, day = parts
        with self.tracker.lock:
            flight = self.tracker._find(number, day)
            if flight is None:
                self.tracker.send(f"Not tracked: {number}")
                return
            self.tracker._hard_fetch(flight)

    def start(self):
        threading.Thread(target=scheduler_loop, args=(self.tracker,), daemon=True).start()
```

- [x] **Step 4: Rewire the tests in `tests/test_flights.py`**

1. Replace the line `import tracker  # noqa: E402` with two lines:
   ```python
   import core  # noqa: E402
   import flights  # noqa: E402
   ```
2. Rewrite module references (`tracker.parse_leg`, `tracker.Tracker`, the `"tracker.urllib..."` patch targets) without touching attributes named `.tracker`:
   ```bash
   sed -i -E 's/(^|[^.A-Za-z_])tracker\./\1flights./g' tests/test_flights.py
   grep -n "flights\.\(Telegram\|dispatch\|parse_command\)" tests/test_flights.py   # these classes are deleted in item 5
   ```
3. Replace the Harness so it builds a real bot around a `Flights` module. Replace the `make` method and add `say` / `press`, plus a `FakeTg` class above `Harness`:
   ```python
   class FakeTg:
       def __init__(self, sent):
           self.sent = sent

       def send(self, text, buttons=None):
           self.sent.append((text, buttons))

       def answer(self, callback_id):
           pass
   ```
   ```python
       def make(self):
           self.bot = core.Bot(FakeTg(self.sent), "123", self.dir.name)
           module = flights.Flights(self.bot.ctx("flights"), self._fetch, self.path, lambda: self.now)
           self.bot.register(module)
           return module.tracker

       def say(self, text):
           self.bot.handle_message(text)

       def press(self, data):
           self.bot.handle_callback(data)
   ```
   (`self.tracker = self.make()` in `__init__` stays; `PersistenceTests` calls `h.make()` again and still gets a `Tracker`.)
4. Route every command through the bot:
   ```bash
   sed -i -E 's/(self\.h|h|self)\.tracker\.handle_message\(/\1.say(/g; s/(self\.h|h)\.tracker\.handle_callback\(/\1.press(/g' tests/test_flights.py
   grep -n "handle_message\|handle_callback" tests/test_flights.py   # expect no output
   ```
5. Delete tests that moved to `test_core.py`:
   - in `ParseTests`: `test_command_with_bot_suffix` and `test_plain_text_is_not_a_command`
   - the whole `TelegramTests`, `Recorder` and `DispatchTests` classes (from `class TelegramTests` down to just before `if __name__ == "__main__":`)
6. In `PersistenceTests.test_state_survives_restart`, delete the line `h.tracker.set_offset(42)` and the line `self.assertEqual(again.state["offset"], 42)`.
7. Append this class before `if __name__ == "__main__":` (it pins "no behavior change" and the button arity guard):
   ```python
   OLD_HELP = (
       "/track LH123 2026-10-05 - start tracking a flight\n"
       "/untrack LH123 - stop tracking\n"
       "/list - tracked flights\n"
       "/fetch - refresh a tracked flight now (pick from the list)\n"
       "/fetch LH123 - refresh that flight now"
   )


   class ModuleTests(unittest.TestCase):
       def setUp(self):
           self.h = Harness()

       def test_unknown_command_shows_exactly_the_old_help_text(self):
           self.h.say("/x")
           self.assertEqual(self.h.texts(), [OLD_HELP])

       def test_start_and_help_commands_show_help_too(self):
           self.h.say("/start")
           self.h.say("/help")
           self.assertEqual(self.h.texts(), [OLD_HELP, OLD_HELP])

       def test_plain_text_gets_no_reply(self):
           self.h.say("hello")
           self.assertEqual(self.h.sent, [])

       def test_button_with_a_missing_part_does_nothing(self):
           self.h.track()
           self.h.sent.clear()
           self.h.press("f:LH123")
           self.h.press("f:LH123:2026-10-05:extra")
           self.assertEqual((self.h.sent, self.h.calls), ([], [("LH123", "2026-10-05")]))

       def test_button_for_an_untracked_flight_says_so(self):
           self.h.press("f:ZZ9:2026-10-05")
           self.assertEqual(self.h.texts(), ["Not tracked: ZZ9"])
   ```

- [x] **Step 5: Run the full suite**

Run (from repo root): `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`, zero failures. `test_flights.py` should have about 79 tests (88 minus the 9 moved) plus the 5 new ones; the exact number is not the check, zero failures is.

If a flights test fails on `handle_message` / `handle_callback` / `tracker.` leftovers, fix that call site by hand; do not weaken any assertion.

- [x] **Step 6: Commit** (tick boxes first)

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/flights.py \
        kubernetes/apps/monitoring/flight-tracker/tests/test_flights.py \
        docs/superpowers/plans/2026-09-30-telegram-bot-core.md
git commit -m "refactor(flight-tracker): move tracker onto the bot core as the flights module

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

`git add` on the renamed paths also stages the deletions of the old names; confirm with `git status --short` that only intended paths are staged (`R` entries for both renames, nothing under `design/`).

---

### Task 3: `main.py` and manifests

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/main.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml` (the `files:` list)
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/deployment.yml` (the container `command`)

**Interfaces:**
- Consumes: `core.Bot`, `core.Telegram`, `core.log`, `flights.Flights`, `flights.fetch_flight`.
- Produces: the process entrypoint `python -u /scripts/main.py`.

- [x] **Step 1: Create `app/main.py`**

```python
#!/usr/bin/env python3
"""Bot entrypoint: build the core, register modules, run. A new feature is one new file plus one
`bot.register(...)` line below."""
import os

import core
import flights


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    key = os.environ["AERODATABOX_KEY"]
    state_path = os.environ.get("STATE_PATH", "/data/state.json")
    data_dir = os.environ.get("DATA_DIR", os.path.dirname(state_path))

    bot = core.Bot(core.Telegram(token, chat_id), chat_id, data_dir, legacy_state=state_path)
    bot.register(flights.Flights(
        bot.ctx("flights"), lambda number, day: flights.fetch_flight(number, day, key), state_path))
    bot.start()
    core.log("flight-tracker started")
    bot.run()


if __name__ == "__main__":
    main()
```

- [x] **Step 2: Add a wiring test** in `tests/test_core.py` (append before `if __name__`), proving `main` imports and the pieces fit:

```python
class MainWiringTests(unittest.TestCase):
    def test_main_module_imports_and_flights_registers_without_clashes(self):
        import flights
        import main  # noqa: F401  (import must not run the bot)
        with tempfile.TemporaryDirectory() as d:
            bot = core.Bot(FakeTg(), "123", d)
            bot.register(flights.Flights(bot.ctx("flights"), lambda n, day: [], os.path.join(d, "state.json")))
        self.assertEqual(sorted(bot.commands), ["fetch", "list", "track", "untrack"])
        self.assertEqual(sorted(bot.callbacks), ["f"])
```

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [x] **Step 3: Update the manifests**

In `app/kustomization.yml` change the `files:` list to:

```yaml
    files:
      - core.py
      - flights.py
      - main.py
```

In `app/deployment.yml` change `command: ["python", "-u", "/scripts/tracker.py"]` to `command: ["python", "-u", "/scripts/main.py"]`. Change nothing else (keep `STATE_PATH`, `strategy: Recreate`, volumes, resources).

- [x] **Step 4: Verify the rendered ConfigMap and the schema**

```bash
cd ~/repos/Homelab
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | grep -E "^  (core|flights|main)\.py:|/scripts/main.py|strategy|type: Recreate"
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app
```

Expected: the ConfigMap lists `core.py`, `flights.py`, `main.py`; the command line shows `/scripts/main.py`; `type: Recreate` present; the validation script exits 0. If the ConfigMap is missing a file, the `files:` list is wrong.

- [x] **Step 5: Local start-up smoke test** (no network, proves the process starts and fails only on Telegram, not on imports)

```bash
cd kubernetes/apps/monitoring/flight-tracker/app
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=123 AERODATABOX_KEY=x STATE_PATH=/tmp/fs-smoke-state.json DATA_DIR=/tmp \
  timeout 5 python3 -u main.py; echo "exit=$?"
rm -f /tmp/fs-smoke-state.json /tmp/core.json
```

Expected: prints `flight-tracker started`, then `getUpdates failed: ...` lines (no network or bad token), and `exit=124` from `timeout`. A Python traceback means a wiring bug: fix before continuing.

- [x] **Step 6: Commit** (tick boxes first)

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/main.py \
        kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml \
        kubernetes/apps/monitoring/flight-tracker/app/deployment.yml \
        kubernetes/apps/monitoring/flight-tracker/tests/test_core.py \
        docs/superpowers/plans/2026-09-30-telegram-bot-core.md
git commit -m "feat(flight-tracker): run the bot from main.py with core + flights modules

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Docs

**Files:**
- Modify: `design/decisions/flight-tracker.md`
- Modify: `.claude/CLAUDE.md` (one routing-table row)
- Modify only if they name `tracker.py` or describe the file layout: any other hit from Task 0 step 3

- [x] **Step 1: Read `design/decisions/flight-tracker.md` in full** (it is short) and keep its existing rules. Design files describe the implemented state and must not record deployed versions.

- [x] **Step 2: Update the file**

1. Wherever it says the bot is "one stdlib-only Python file" / names `tracker.py`, replace with: three stdlib-only files (`core.py`, `flights.py`, `main.py`) delivered by one `configMapGenerator`.
2. Add a `## Layout and modules` section:

   ```markdown
   ## Layout and modules

   `core.py` owns the Telegram client, the owner-only check, the update offset (`/data/core.json`),
   and a registry of commands and inline-button prefixes. `flights.py` is the flight tracker as a
   module. `main.py` builds the bot and registers modules. A module is any object with `name`,
   `help` (lines), `commands` (`name -> fn(args)`), `callbacks` (`prefix -> fn(parts)`) and an
   optional `start()`; the core hands it a `Ctx` with `send`, `log` and its own state path
   `/data/<name>.json`.

   To add a feature: write `<feature>.py` with a module class, add it to `files:` in
   `app/kustomization.yml`, add one `bot.register(...)` line in `main.py`, and add its help
   lines to the class. The core needs no change.
   ```
3. Add these rules to the `## Rules` section:
   - **Do not rename the app, Kustomization, Deployment or `flight-tracker-data` PVC without copying the state first** — the rename changes the Flux Kustomization name and `prune: true` deletes the PVC with the live flight state.
   - **`core.json` is authoritative for the offset; `state.json["offset"]` is only the first-start seed** — on the first start after the split the core reads the old offset from `state.json`, so Telegram does not replay old commands.
   - **Callback data keeps the `f:NUMBER:DAY` format** — buttons in old chat messages still work.
   - **Duplicate command names or callback prefixes across modules fail at startup** — that is intentional.
4. Update any "Read before editing" path list to include the three files.

- [x] **Step 3: Update the routing row in `.claude/CLAUDE.md`**

Change the row text `flight tracker, AeroDataBox, its Telegram commands` to `Telegram bot (core, modules, commands), flight tracker, AeroDataBox`. Leave the file path column as is.

- [x] **Step 4: Fix other references** from Task 0 step 3 (`tracker.py`, `test_tracker`), if any. Re-run the grep; expected no hits outside `docs/superpowers/`.

- [x] **Step 5: Commit** (tick boxes first)

```bash
git add design/decisions/flight-tracker.md .claude/CLAUDE.md docs/superpowers/plans/2026-09-30-telegram-bot-core.md
git commit -m "docs(flight-tracker): document the core + module layout and the no-rename rule

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

Add any other file fixed in step 4 to that `git add`.

---

### Task 5: Final verification (local, no deploy)

**Files:** none changed unless a check fails.

- [ ] **Step 1: Full suite green**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`.

- [ ] **Step 2: Diff is only what the spec allows**

```bash
git diff --stat main...HEAD
git status --short
```

Expected in the diff: `core.py`, `flights.py` (renamed from `tracker.py`), `main.py`, `kustomization.yml`, `deployment.yml`, `test_core.py`, `test_flights.py` (renamed), `design/decisions/flight-tracker.md`, `.claude/CLAUDE.md`, plus the plan file. Not in the diff: any file under `gotify-telegram/`, any `*-sealed.yml`, `pvc.yml`, `ks.yml`, and the two unrelated modified files (they must still show as ` M` in `git status`, uncommitted).

- [ ] **Step 3: Confirm behavior is unchanged in the flights file**

```bash
git diff -M main...HEAD -- kubernetes/apps/monitoring/flight-tracker/app/flights.py | grep '^[-+]' | grep -v '^+++\|^---'
```

Expected: removals limited to `HELP`, `log`, `parse_command`, `set_offset`, the `offset` default, `handle_message`/`handle_callback`, and the `Telegram`/`dispatch`/`receiver_loop`/`run` block; additions limited to `from core import log` and the `Flights` class. Any other changed line is a regression risk: revert it.

- [ ] **Step 4: Commit any fixes** (skip if none), tick this task's boxes.

---

### Task 6: Ship (needs the user)

- [ ] **Step 1: Ask the user for a go.** Merging to `main` deploys to the live flight bot via Flux. Stop and ask; do not push without an explicit yes.

- [ ] **Step 2: Estimate to tell the user:** push and PR take 2 minutes; Flux reconciles within its 30 minute interval or immediately with `mise exec -- flux reconcile kustomization flight-tracker --with-source`; the pod swap is a few seconds.

- [ ] **Step 3 (after the yes): push and open the PR**

```bash
git push -u origin feat/telegram-bot-core
gh pr create --title "feat(telegram-bot): core + module registry, flights as first module" --body "Step 1 of 3. No behavior change. Spec: docs/superpowers/specs/2026-09-30-telegram-bot-core-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 4 (after the user merges): smoke test in the owner chat**, with the user:

1. `/list`: same tracked flights as before.
2. `/fetch`: buttons appear; tap one and it refreshes (costs one API call).
3. `/x`: the five flight help lines.
4. `mise exec -- kubectl -n monitoring logs deploy/flight-tracker --tail=30`: contains `flight-tracker started`, no `getUpdates failed`, no `409`.
5. `mise exec -- kubectl -n monitoring exec deploy/flight-tracker -- ls /data`: shows `state.json` and `core.json`.

- [ ] **Step 5: Rollback if any smoke check fails:** `git revert -m 1 <merge-commit>`, push, reconcile. Safe: Telegram drops updates below the confirmed offset, so the old image will not replay commands.

- [ ] **Step 6: Tell the user step 1 is done.** Next spec: the `alerts` module (`/alerts` mute toggle, retiring the gotify-telegram bridge).
