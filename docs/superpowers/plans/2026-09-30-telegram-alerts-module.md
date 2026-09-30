# Telegram alerts module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `alerts` module to the Telegram bot that forwards Gotify messages to Telegram with an `/alerts` mute toggle, and retire the separate `gotify-telegram` bridge Deployment.

**Architecture:** `alerts.py` is a core module (step 1 registry). A daemon thread polls Gotify `GET /message` every 10 s with the client token, forwards messages newer than a persisted `last_id`, and drops them while muted. Mute state and `last_id` live in `/data/alerts.json`.

**Tech Stack:** Python 3 stdlib only (`unittest`), Kustomize, Flux.

**Spec:** `docs/superpowers/specs/2026-09-30-telegram-alerts-module-design.md`. Read it and `2026-09-30-telegram-bot-core-design.md` first.

## Global Constraints

- Stdlib-only Python; no `pip`; read-only rootfs.
- `flight-tracker` stays the only `getUpdates` consumer. Do NOT rename the app, Kustomization, Deployment, PVC or secrets.
- `telegram-secret` stays owned by the `gotify-telegram` Kustomization (`flight-tracker` `dependsOn` it). Keep that Kustomization; delete only its Deployment and script ConfigMap.
- The bot loads `gotify-client-secret` (key `CLIENT_TOKEN`) via a new `envFrom`. No new sealed secret.
- Never log the token or exception text: exception **type** only.
- Muted alerts are dropped. Flight messages are never muted.
- Missing, unreadable or wrong-shape `alerts.json` means alerts are ON, never muted.
- Never `git add -A`; the main checkout has unrelated uncommitted changes (`design/decisions/jellyfin.md`, `design/docs/storage.md`). Never record deployed version numbers in docs. `kubectl` is diagnostics only.
- Test command (repo root, must end `OK`): `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
- Baseline: 116 tests pass.

## Review Focus

Each has a test in the task named in brackets.

1. First start with existing Gotify history: forwards nothing, and `last_id` is the newest id. [Task 1]
2. Gotify DB reset (server top id below stored `last_id`): everything on the server is forwarded once, not skipped forever. [Task 1]
3. Corrupt or wrong-shape `alerts.json`, including `{"muted": true, "until": "junk"}`: alerts ON, no crash. [Task 1]
4. A message with no `title`, no `priority`, or a non-dict / id-less entry in the list: no crash, sensible output. [Task 1]
5. A poll failure whose exception text contains the token: only the type is logged, once per streak. [Task 1]

---

### Task 1: `alerts.py` with tests

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/alerts.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py`

**Interfaces:**
- Consumes: `core.log`, `core.Ctx(name, send, state_path)`.
- Produces (Task 2): `alerts.fetch_messages(host, token, timeout=10) -> list`, `alerts.Alerts(ctx, fetch, now=<utc clock>)` implementing the module protocol (`name="alerts"`, `help`, `commands={"alerts": fn}`, `callbacks={}`, `start()`), plus `Alerts.poll_once() -> bool`.

- [x] **Step 1: Write the failing tests**

Create `kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py`:

```python
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import alerts  # noqa: E402
import core  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TOKEN = "SECRET-CLIENT-TOKEN"


def m(id, title="t", message="b", priority=5):
    return {"id": id, "title": title, "message": message, "priority": priority}


class Harness:
    """Alerts wired to a fake clock, a scripted Gotify and a message log."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        unittest.addModuleCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "alerts.json")
        self.now = T0
        self.sent = []
        self.batches = []  # queue of message lists (newest first) or exceptions
        self.alerts = self.make()

    def make(self):
        ctx = core.Ctx("alerts", lambda text, buttons=None: self.sent.append(text), self.path)
        return alerts.Alerts(ctx, self._fetch, lambda: self.now)

    def _fetch(self):
        r = self.batches.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def poll(self, batch):
        self.batches.append(batch)
        return self.alerts.poll_once()

    def say(self, *args):
        self.alerts.commands["alerts"](list(args))

    def seed(self, top=1):
        self.poll([m(top)] if top else [])
        self.sent.clear()

    def state(self):
        with open(self.path) as f:
            return json.load(f)

    def write(self, content):
        with open(self.path, "w") as f:
            f.write(content)


class DurationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(alerts.parse_duration("1h"), timedelta(hours=1))
        self.assertEqual(alerts.parse_duration("24h"), timedelta(hours=24))
        self.assertEqual(alerts.parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(alerts.parse_duration("168h"), timedelta(days=7))

    def test_invalid(self):
        for bad in ("", "0h", "0m", "x", "8d", "169h", "-1h", "1.5h", "1h30m", "h", "1", None):
            self.assertIsNone(alerts.parse_duration(bad), bad)


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_first_start_forwards_nothing_and_remembers_the_newest_id(self):
        self.h.poll([m(5), m(3)])
        self.assertEqual(self.h.sent, [])
        self.assertEqual(self.h.state()["last_id"], 5)
        self.h.poll([m(6, "new"), m(5), m(3)])
        self.assertEqual(self.h.sent, ["\U0001f7e1 new\nb"])

    def test_empty_server_seeds_zero_then_forwards_the_first_message(self):
        self.h.poll([])
        self.assertEqual(self.h.state()["last_id"], 0)
        self.h.poll([m(1, "first")])
        self.assertEqual(self.h.sent, ["\U0001f7e1 first\nb"])

    def test_forwards_oldest_first(self):
        self.h.seed(1)
        self.h.poll([m(4, "d"), m(3, "c"), m(2, "b"), m(1, "a")])
        self.assertEqual([s.split("\n")[0][2:] for s in self.h.sent], ["b", "c", "d"])

    def test_priority_picks_the_emoji(self):
        self.h.seed(0)
        self.h.poll([m(3, priority=9), m(2, priority=5), m(1, priority=1)])
        self.assertEqual([s[0] for s in self.h.sent], ["\U0001f7e2", "\U0001f7e1", "\U0001f534"])

    def test_message_without_title_message_or_priority(self):
        self.h.seed(0)
        self.h.poll([{"id": 1}])
        self.assertEqual(self.h.sent, ["\U0001f7e1"])

    def test_entries_without_a_usable_id_are_skipped(self):
        self.h.seed(0)
        self.h.poll([m(2, "ok"), "junk", None, {"title": "no id"}, {"id": "7"}, {"id": True}])
        self.assertEqual(self.h.sent, ["\U0001f7e1 ok\nb"])
        self.assertEqual(self.h.state()["last_id"], 2)

    def test_nothing_new_sends_nothing(self):
        self.h.seed(4)
        self.h.poll([m(4)])
        self.assertEqual(self.h.sent, [])

    def test_gotify_db_reset_forwards_what_the_server_now_holds(self):
        self.h.seed(500)
        with mock.patch("alerts.log") as lg:
            self.h.poll([m(2, "DRIFT", priority=8), m(1, "hello")])
        self.assertEqual(len(self.h.sent), 2)
        self.assertIn("DRIFT", self.h.sent[1])
        self.assertEqual(self.h.state()["last_id"], 2)
        lg.assert_called_once()

    def test_last_id_survives_restart_without_replaying(self):
        self.h.seed(4)
        self.h.poll([m(5), m(4)])
        self.h.sent.clear()
        self.h.alerts = self.h.make()
        self.h.poll([m(5), m(4)])
        self.assertEqual(self.h.sent, [])


class PollFailureTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.seed(1)

    def test_failure_keeps_last_id_and_returns_false(self):
        with mock.patch("alerts.log"):
            self.assertFalse(self.h.poll(OSError("down")))
        self.assertEqual(self.h.state()["last_id"], 1)

    def test_only_the_exception_type_is_logged_once_per_streak(self):
        with mock.patch("alerts.log") as lg:
            for _ in range(3):
                self.h.poll(OSError(f"connect to http://x?token={TOKEN} refused"))
        lg.assert_called_once_with("gotify poll failed: OSError")
        self.assertNotIn(TOKEN, str(lg.call_args))

    def test_a_success_ends_the_streak(self):
        with mock.patch("alerts.log") as lg:
            self.h.poll(OSError("a"))
            self.h.poll([m(1)])
            self.h.poll(OSError("b"))
        self.assertEqual(lg.call_count, 2)

    def test_bad_shapes_are_failures_not_crashes(self):
        with mock.patch("alerts.log"):
            for bad in (ValueError("json"), KeyError("messages"), TypeError("x"), {"messages": []}, None):
                self.assertFalse(self.h.poll(bad), bad)
        self.assertEqual(self.h.state()["last_id"], 1)


class MuteTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.seed(1)

    def test_status_when_on(self):
        self.h.say()
        self.assertEqual(self.h.sent, ["Alerts: ON"])

    def test_off_without_duration_is_indefinite(self):
        self.h.say("off")
        self.assertEqual(self.h.sent[-1], "Alerts: OFF (until you turn them on)")
        self.h.now = T0 + timedelta(days=30)
        self.h.say()
        self.assertEqual(self.h.sent[-1], "Alerts: OFF (until you turn them on)")

    def test_off_with_duration_shows_the_expiry(self):
        self.h.say("off", "1h")
        self.assertEqual(self.h.sent[-1], "Alerts: OFF until 13:00 UTC")

    def test_expiry_on_another_day_includes_the_date(self):
        self.h.say("off", "24h")
        self.assertEqual(self.h.sent[-1], "Alerts: OFF until 06 Oct 12:00 UTC")

    def test_on_unmutes(self):
        self.h.say("off")
        self.h.say("on")
        self.assertEqual(self.h.sent[-1], "Alerts: ON")

    def test_muted_messages_are_dropped_and_never_replayed(self):
        self.h.say("off")
        self.h.sent.clear()
        self.h.poll([m(2, "quiet"), m(1)])
        self.assertEqual(self.h.sent, [])
        self.h.say("on")
        self.h.sent.clear()
        self.h.poll([m(3, "loud"), m(2, "quiet"), m(1)])
        self.assertEqual(self.h.sent, ["\U0001f7e1 loud\nb"])

    def test_timed_mute_expires_by_itself(self):
        self.h.say("off", "1h")
        self.h.sent.clear()
        self.h.now = T0 + timedelta(minutes=30)
        self.h.poll([m(2, "during"), m(1)])
        self.assertEqual(self.h.sent, [])
        self.h.now = T0 + timedelta(minutes=61)
        self.h.poll([m(3, "after"), m(2), m(1)])
        self.assertEqual(self.h.sent, ["\U0001f7e1 after\nb"])
        self.h.say()
        self.assertEqual(self.h.sent[-1], "Alerts: ON")

    def test_mute_survives_a_restart(self):
        self.h.say("off", "4h")
        self.h.alerts = self.h.make()
        self.h.sent.clear()
        self.h.poll([m(2), m(1)])
        self.assertEqual(self.h.sent, [])

    def test_bad_arguments_get_the_usage_line(self):
        for args in (("off", "0h"), ("off", "1h", "x"), ("maybe",), ("on", "1h"), ("off", "8d")):
            self.h.sent.clear()
            self.h.say(*args)
            self.assertEqual(self.h.sent, [alerts.USAGE], args)
        self.h.say()
        self.assertEqual(self.h.sent[-1], "Alerts: ON")


class FailOpenTests(unittest.TestCase):
    def test_missing_state_means_on(self):
        h = Harness()
        h.say()
        self.assertEqual(h.sent, ["Alerts: ON"])

    def test_corrupt_or_wrong_shape_state_means_on_and_reseeds(self):
        for bad in ("{not json", "[]", "null", '{"muted": "yes"}', '{"muted": true, "until": "junk"}',
                    '{"muted": false, "last_id": "x"}', '{"muted": false, "last_id": true}'):
            h = Harness()
            h.write(bad)
            h.alerts = h.make()
            with mock.patch("alerts.log"):
                h.say()
                h.poll([m(9)])
                h.poll([m(10, "after"), m(9)])
            self.assertEqual(h.sent[0], "Alerts: ON", bad)
            self.assertEqual(h.sent[1:], ["\U0001f7e1 after\nb"], bad)


class FetchTests(unittest.TestCase):
    def call(self, body=b'{"messages": [{"id": 1}]}', side_effect=None):
        resp = mock.MagicMock()
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        with mock.patch("alerts.urllib.request.urlopen", side_effect=side_effect,
                        return_value=resp) as urlopen:
            try:
                return alerts.fetch_messages("http://gotify", TOKEN), urlopen
            finally:
                pass

    def test_request_shape_and_result(self):
        result, urlopen = self.call()
        req = urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://gotify/message?limit=100")
        self.assertEqual(req.get_header("X-gotify-key"), TOKEN)
        self.assertEqual(result, [{"id": 1}])

    def test_http_error_propagates_as_oserror(self):
        err = urllib.error.HTTPError("http://gotify", 401, "no", {}, None)
        with self.assertRaises(OSError):
            self.call(side_effect=err)

    def test_body_without_messages_key_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.call(body=b"{}")


class ModuleTests(unittest.TestCase):
    def test_module_protocol(self):
        a = Harness().alerts
        self.assertEqual(a.name, "alerts")
        self.assertEqual(list(a.commands), ["alerts"])
        self.assertEqual(a.callbacks, {})
        self.assertTrue(a.help and all(line.startswith("/alerts") for line in a.help))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -p test_alerts.py`
Expected: `ModuleNotFoundError: No module named 'alerts'`

- [x] **Step 3: Write `alerts.py`**

Create `kubernetes/apps/monitoring/flight-tracker/app/alerts.py`:

```python
#!/usr/bin/env python3
"""Gotify -> Telegram forwarding with a mute toggle (/alerts)."""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from core import log

UTC = timezone.utc
POLL_SECONDS = 10
MAX_MUTE = timedelta(days=7)
USAGE = "Usage: /alerts [on | off [1h|4h|24h]]"
DURATION_RE = re.compile(r"^(\d{1,4})([hm])$")


def parse_duration(text):
    match = DURATION_RE.match(text or "")
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2)
    delta = timedelta(hours=n) if unit == "h" else timedelta(minutes=n)
    return delta if timedelta(0) < delta <= MAX_MUTE else None


def fetch_messages(host, token, timeout=10):
    """Newest-first list of Gotify messages. Raises OSError, ValueError, KeyError on failure."""
    req = urllib.request.Request(f"{host}/message?limit=100", headers={"X-Gotify-Key": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["messages"]


def format_message(msg):
    priority = msg.get("priority")
    if not isinstance(priority, int):
        priority = 5
    emoji = "\U0001f534" if priority >= 8 else "\U0001f7e1" if priority >= 5 else "\U0001f7e2"
    return f"{emoji} {msg.get('title') or ''}\n{msg.get('message') or ''}".strip()


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _time(text):
    try:
        value = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Alerts:
    name = "alerts"
    help = [
        "/alerts - show whether Gotify alerts are on",
        "/alerts off [1h|4h|24h] - mute them (no duration = until on)",
        "/alerts on - unmute",
    ]

    def __init__(self, ctx, fetch, now=lambda: datetime.now(UTC)):
        self.ctx, self.fetch, self.now = ctx, fetch, now
        self.lock = threading.RLock()
        self.failing = False
        self.commands = {"alerts": self._command}
        self.callbacks = {}
        self.state = self._load()

    # -- persistence --

    def _load(self):
        default = {"last_id": None, "muted": False, "until": None}
        try:
            with open(self.ctx.state_path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return default
        except ValueError:
            log("alerts state unreadable, alerts stay on")
            return default
        ok = (isinstance(raw, dict) and isinstance(raw.get("muted"), bool)
              and (raw.get("last_id") is None or _int(raw["last_id"]))
              and (raw.get("until") is None or _time(raw["until"]) is not None))
        if not ok:
            log("alerts state has the wrong shape, alerts stay on")
            return default
        return {key: raw.get(key) for key in default}

    def _save(self):
        tmp = self.ctx.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.ctx.state_path)

    # -- mute --

    def _is_muted(self, now):
        s = self.state
        if not s["muted"]:
            return False
        until = _time(s["until"]) if s["until"] else None
        if until is not None and now >= until:
            s["muted"], s["until"] = False, None
            self._save()
            return False
        return True

    def _status(self, now):
        if not self._is_muted(now):
            return "Alerts: ON"
        until = _time(self.state["until"]) if self.state["until"] else None
        if until is None:
            return "Alerts: OFF (until you turn them on)"
        fmt = "%H:%M UTC" if until.date() == now.date() else "%d %b %H:%M UTC"
        return f"Alerts: OFF until {until.strftime(fmt)}"

    def _command(self, args):
        with self.lock:
            now = self.now()
            if not args:
                self.ctx.send(self._status(now))
            elif args == ["on"]:
                self.state["muted"], self.state["until"] = False, None
                self._save()
                self.ctx.send(self._status(now))
            elif args[0] == "off" and len(args) <= 2:
                until = None
                if len(args) == 2:
                    delta = parse_duration(args[1])
                    if delta is None:
                        self.ctx.send(USAGE)
                        return
                    until = (now + delta).isoformat()
                self.state["muted"], self.state["until"] = True, until
                self._save()
                self.ctx.send(self._status(now))
            else:
                self.ctx.send(USAGE)

    # -- forwarder --

    def poll_once(self):
        try:
            messages = self.fetch()
            if not isinstance(messages, list):
                raise TypeError("messages")
        except (OSError, ValueError, KeyError, TypeError) as e:
            if not self.failing:
                log(f"gotify poll failed: {type(e).__name__}")
            self.failing = True
            return False
        self.failing = False
        valid = sorted((x for x in messages if isinstance(x, dict) and _int(x.get("id"))),
                       key=lambda x: x["id"])
        top = valid[-1]["id"] if valid else 0
        with self.lock:
            last = self.state["last_id"]
            if last is None:
                last = top
            elif top < last:
                log("gotify ids went backwards, assuming a database reset")
                last = 0
            if not self._is_muted(self.now()):
                for msg in valid:
                    if msg["id"] > last:
                        self.ctx.send(format_message(msg))
            self.state["last_id"] = max(last, top)
            self._save()
        return True

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:  # keep the forwarder alive
                log(f"alerts loop failed: {type(e).__name__}")
            time.sleep(POLL_SECONDS)
```

- [x] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests`
Expected: `OK`. If a test fails, fix the code, not the test, unless the test contradicts the spec (then ledger a ruling).

- [x] **Step 5: Commit** (tick this task's boxes first)

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/alerts.py \
        kubernetes/apps/monitoring/flight-tracker/tests/test_alerts.py \
        docs/superpowers/plans/2026-09-30-telegram-alerts-module.md
git commit -m "feat(telegram-bot): add alerts module with Gotify polling and mute toggle

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Wire it up and retire the bridge

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/main.py`
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_core.py` (`MainWiringTests`)
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml` (add `alerts.py` to `files:`)
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/deployment.yml` (add `envFrom`)
- Modify: `kubernetes/apps/monitoring/gotify-telegram/app/kustomization.yml`
- Delete: `kubernetes/apps/monitoring/gotify-telegram/app/deployment.yml`, `script-configmap.yml`

**Interfaces:** Consumes `alerts.Alerts`, `alerts.fetch_messages`.

- [x] **Step 1: Update the wiring test first** (in `tests/test_core.py`, replace `MainWiringTests`)

```python
class MainWiringTests(unittest.TestCase):
    def test_main_registers_flights_and_alerts_without_clashes(self):
        import alerts
        import flights
        import main  # noqa: F401  (import must not run the bot)
        with tempfile.TemporaryDirectory() as d:
            bot = core.Bot(FakeTg(), "123", d)
            bot.register(flights.Flights(bot.ctx("flights"), lambda n, day: [], os.path.join(d, "state.json")))
            bot.register(alerts.Alerts(bot.ctx("alerts"), lambda: []))
            bot.handle_message("/alerts off 4h")
        self.assertEqual(sorted(bot.commands), ["alerts", "fetch", "list", "track", "untrack"])
        self.assertEqual(sorted(bot.callbacks), ["f"])
        self.assertTrue(bot.tg.sent[-1][0].startswith("Alerts: OFF until"))
```

Run the suite. Expected: this test PASSES already if Task 1 is done (it only uses classes); that is fine, it pins the registry. Then confirm `main.py` does not yet register alerts:
`grep -c alerts kubernetes/apps/monitoring/flight-tracker/app/main.py` → `0`.

- [x] **Step 2: Edit `main.py`** to read the client token and register the module

Replace the body of `main()` with:

```python
def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    key = os.environ["AERODATABOX_KEY"]
    gotify_token = os.environ["CLIENT_TOKEN"]
    gotify_host = os.environ.get("GOTIFY_HOST", "http://gotify.monitoring.svc.cluster.local")
    state_path = os.environ.get("STATE_PATH", "/data/state.json")
    data_dir = os.environ.get("DATA_DIR", os.path.dirname(state_path))

    bot = core.Bot(core.Telegram(token, chat_id), chat_id, data_dir, legacy_state=state_path)
    bot.register(flights.Flights(
        bot.ctx("flights"), lambda number, day: flights.fetch_flight(number, day, key), state_path))
    bot.register(alerts.Alerts(
        bot.ctx("alerts"), lambda: alerts.fetch_messages(gotify_host, gotify_token)))
    bot.start()
    core.log("flight-tracker started")
    bot.run()
```

and add `import alerts` above `import core`.

- [x] **Step 3: Manifests**

`app/kustomization.yml`: add `- alerts.py` to the `files:` list (order: `alerts.py`, `core.py`, `flights.py`, `main.py`).

`app/deployment.yml`: under the container's `envFrom:` add a third entry:

```yaml
            - secretRef:
                name: gotify-client-secret
```

`gotify-telegram/app/kustomization.yml` becomes:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./telegram-sealed.yml
```

```bash
git rm kubernetes/apps/monitoring/gotify-telegram/app/deployment.yml \
       kubernetes/apps/monitoring/gotify-telegram/app/script-configmap.yml
```

- [x] **Step 4: Verify**

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | grep -E "^  (alerts|core|flights|main)\.py:|gotify-client-secret|type: Recreate"
mise exec -- kubectl kustomize kubernetes/apps/monitoring/gotify-telegram/app | grep -E "^kind:|name:"
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/flight-tracker/app
.agents/scripts/validate-manifests.sh kubernetes/apps/monitoring/gotify-telegram/app
```

Expected: suite `OK`; the ConfigMap lists four files; `gotify-client-secret` and `type: Recreate` present; the gotify-telegram render shows only the `SealedSecret` `telegram-secret`; both validate scripts exit 0.

- [x] **Step 5: Local start-up smoke test** (fake tokens; must print `flight-tracker started` and fail only on the network)

```bash
cd kubernetes/apps/monitoring/flight-tracker/app
D=$(mktemp -d)
echo '{"flights": [], "offset": 42}' > $D/state.json
TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=123 AERODATABOX_KEY=x CLIENT_TOKEN=x GOTIFY_HOST=http://127.0.0.1:9 \
  STATE_PATH=$D/state.json timeout 5 python3 -u main.py 2>&1 | head -6
rm -rf $D
```

Expected: `flight-tracker started`, then `getUpdates failed: ...` and `gotify poll failed: URLError` (or `OSError` subclass name). A traceback means a wiring bug.

- [x] **Step 6: Commit** (tick boxes first)

```bash
git add kubernetes/apps/monitoring/flight-tracker kubernetes/apps/monitoring/gotify-telegram \
        docs/superpowers/plans/2026-09-30-telegram-alerts-module.md
git commit -m "feat(telegram-bot): register alerts and retire the gotify-telegram bridge

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git status --short
```

Confirm `git status --short` shows the two deletions committed and nothing under `design/`.

---

### Task 3: Docs

**Files:**
- Modify: `design/decisions/gotify.md`, `design/decisions/flight-tracker.md`, `AGENTS.md` (routing row), `kubernetes/apps/monitoring/flux-notifications/app/provider.yml` (comment only, if it names the bridge as a live thing)

- [ ] **Step 1: Read `design/decisions/gotify.md` and `design/decisions/flight-tracker.md` in full.** Keep all existing rules. Do not record deployed versions.

- [ ] **Step 2: `gotify.md`**
  - In `**Read before editing:**` keep `gotify-telegram/` and add `flight-tracker/`.
  - Replace the whole `**gotify-telegram bridge**` paragraph with: the Telegram side is the `alerts` module of the bot in `monitoring/flight-tracker`, polling `GET /message` every 10 s with the `gotify-client-secret` token, forwarding messages newer than a persisted `last_id`, and dropping them while muted with `/alerts off`. The `gotify-telegram` Kustomization survives only to own `telegram-secret`.
  - Delete the rule `Run gotify-telegram with python -u` (the bridge no longer exists).
  - Add rules: **a Gotify DB reset rewinds ids; the module detects `top < last_id` and forwards what the server holds** (the bootstrap `DRIFT` message follows a reset); **the client token rotating restarts the bot pod via Reloader, and the module seeds nothing from history on a fresh state**.

- [ ] **Step 3: `flight-tracker.md`**
  - Add `alerts.py` to the layout section and list `/alerts` among the commands.
  - Add rules: **`/alerts off` drops Gotify messages from Telegram (they stay in the Gotify UI) and never mutes flight messages**; **missing or corrupt `alerts.json` means alerts ON**; **first start with no `alerts.json` forwards nothing and records the newest Gotify id**; **the bot now also depends on `gotify-client-secret`** (provisioned by `gotify-bootstrap`).

- [ ] **Step 4: `AGENTS.md`** row `Gotify, its app tokens, the Telegram bridge` → `Gotify, its app tokens, the Telegram alerts module`. If `flux-notifications/app/provider.yml` line 10 comment calls the bridge live, reword it to `the bot's alerts module`; comment only.

- [ ] **Step 5: Verify and commit**

```bash
grep -rn "python -u\|gotify-telegram bridge" design/decisions/gotify.md   # expect no output
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests | tail -2
git add design/decisions/gotify.md design/decisions/flight-tracker.md AGENTS.md kubernetes/apps/monitoring/flux-notifications/app/provider.yml docs/superpowers/plans/2026-09-30-telegram-alerts-module.md
git commit -m "docs(telegram-bot): document the alerts module and the retired bridge

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

(Drop `provider.yml` from `git add` if unchanged.)

---

### Task 4: Final verification and ship (ship needs the user)

- [ ] **Step 1: Suite green and diff scope.** Run the test command; `git diff --stat main...HEAD` must show only: `alerts.py`, `test_alerts.py`, `main.py`, `test_core.py`, the two flight-tracker manifests, the gotify-telegram manifests (2 deleted, 1 edited), the docs above and the plan. No sealed secrets, no `ks.yml`, no PVC.

- [ ] **Step 2: Ask the user before pushing.** Merging to `main` deploys: it replaces the bridge with the in-bot forwarder. Estimate: push 1 min, Flux reconcile up to 30 min or immediate with `mise exec -- flux reconcile kustomization flight-tracker --with-source` and `gotify-telegram`.

- [ ] **Step 3 (after a yes): push, reconcile, verify**

```bash
git push origin main
mise exec -- flux reconcile kustomization gotify-telegram --with-source
mise exec -- flux reconcile kustomization flight-tracker --with-source
mise exec -- kubectl -n monitoring get deploy | grep -E "flight-tracker|gotify-telegram"
mise exec -- kubectl -n monitoring logs deploy/flight-tracker --tail=20
mise exec -- kubectl -n monitoring exec deploy/flight-tracker -- cat /data/alerts.json
```

Expected: `flight-tracker` Available, no `gotify-telegram` Deployment, log has `flight-tracker started` and no `gotify poll failed`, `alerts.json` has `last_id`.

- [ ] **Step 4: Smoke test with the user**: `/alerts` shows ON; post a test message in the Gotify web UI and it reaches Telegram within about 10 s; `/alerts off 1h`, post another, nothing arrives, `/alerts` shows the expiry; `/alerts on`, post a third, only the third arrives.

- [ ] **Step 5: Rollback if it fails:** `git revert` the merge range, push, reconcile. The bridge Deployment returns; the offset and flight state are untouched.
