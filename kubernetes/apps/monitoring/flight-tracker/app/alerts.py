#!/usr/bin/env python3
"""Gotify -> Telegram forwarding with a mute toggle (/alerts)."""
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import triage as triage_mod
from core import log

UTC = timezone.utc
POLL_SECONDS = 10
MAX_MUTE = timedelta(days=7)
MAX_DROPS = 20
JUDGE_BUDGET = 20.0   # seconds of judging per poll; the rest is forwarded unjudged
USAGE = "Usage: /alerts [on | off [1h|4h|24h] | dropped]"
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
        "/alerts dropped - the last messages triage kept out of Telegram",
    ]

    def __init__(self, ctx, fetch, now=lambda: datetime.now(UTC), triage=None,
                 clock=time.monotonic):
        self.ctx, self.fetch, self.now, self.triage = ctx, fetch, now, triage
        self.clock = clock
        self.lock = threading.RLock()
        self.failing = False
        self.commands = {"alerts": self._command}
        self.callbacks = {}
        self.state = self._load()

    # -- persistence --

    def _load(self):
        default = {"last_id": None, "muted": False, "until": None, "dropped": []}
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
        state = {key: raw.get(key) for key in default}
        if not isinstance(state["dropped"], list):
            state["dropped"] = []
        else:
            state["dropped"] = [d for d in state["dropped"] if isinstance(d, dict)][:MAX_DROPS]
        return state

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
            elif args == ["dropped"]:
                self.ctx.send(self._dropped_text())
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

    # -- triage drops --

    def _record_drop(self, msg):
        with self.lock:
            entry = {"id": msg["id"], "title": msg.get("title") or "", "priority": msg.get("priority"),
                     "at": self.now().isoformat()}
            self.state["dropped"] = ([entry] + self.state["dropped"])[:MAX_DROPS]
            self._save()

    def _dropped_text(self):
        if self.triage is None:
            return "Triage is off: every message is forwarded."
        drops = self.state["dropped"]
        if not drops:
            return "Triage has dropped nothing yet."
        lines = [f"- {str(d.get('at') or '')[5:16].replace('T', ' ')} {str(d.get('title') or '(no title)')}" for d in drops]
        return f"Last {len(drops)} messages kept out of Telegram (still in Gotify) (times UTC):\n" + "\n".join(lines)

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
            new = [x for x in valid if x["id"] > last]
            muted = self._is_muted(self.now())
        if not muted:
            self._deliver(new)  # outside the lock: judging is slow and /alerts must stay responsive
        with self.lock:
            self.state["last_id"] = max(last, top)
            self._save()
        return True

    def _deliver(self, new):
        if self.triage is not None:  # critical messages first, so a slow burst never delays them
            new = sorted(new, key=lambda x: (not triage_mod.is_critical(x), x["id"]))
        start = self.clock()
        for msg in new:
            with self.lock:
                if self._is_muted(self.now()):  # muted since the last message: stop judging too
                    return
            if (self.triage is not None and self.clock() - start <= JUDGE_BUDGET
                    and not self.triage.judge(msg)):  # over budget: forward unjudged (fail open)
                self._record_drop(msg)
                continue
            with self.lock:
                if self._is_muted(self.now()):  # muted while we were judging
                    return
            self.ctx.send(format_message(msg))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:  # keep the forwarder alive
                log(f"alerts loop failed: {type(e).__name__}")
            time.sleep(POLL_SECONDS)
