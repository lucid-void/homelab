#!/usr/bin/env python3
"""Personal flight tracker: Telegram commands in, AeroDataBox polling, change alerts out."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
API_HOST = "aerodatabox.p.rapidapi.com"
UNITS_PER_CALL = 2  # confirmed in Task 1: x-ratelimit-api-units drops by 2 per call
MONTHLY_UNITS = 400
WARN_AT = 0.80
PAUSE_AT = 0.95
JITTER = timedelta(minutes=5)
RETRY_AFTER_FAILURE = timedelta(minutes=15)
FAILED = "Failed to fetch API"
NO_FLIGHTS = "No tracked flights"
PRE_DEPARTURE = {"Unknown", "Expected", "CheckIn", "Boarding", "GateClosed", "Delayed"}
CANCELLED = {"Canceled", "CanceledUncertain"}
FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})0*(\d{1,4})([A-Z]?)$")  # leading zeros dropped: SK0486 == SK486
TIME_LABELS = {"Dep scheduled", "Dep expected", "Arr scheduled", "Arr expected"}
HELP = (
    "/track LH123 2026-10-05 - start tracking a flight\n"
    "/untrack LH123 - stop tracking\n"
    "/list - tracked flights\n"
    "/fetch - refresh a tracked flight now (pick from the list)\n"
    "/fetch LH123 - refresh that flight now"
)


def log(msg):
    print(msg, flush=True)


# ---------- parsing ----------

def parse_command(text):
    parts = (text or "").split()
    if not parts or not parts[0].startswith("/"):
        return None
    return parts[0][1:].split("@")[0].lower(), parts[1:]


def normalize_flight_number(text):
    match = FLIGHT_RE.match(re.sub(r"\s+", "", text or "").upper())
    return "".join(match.groups()) if match else None


def parse_ref(args):
    """`LH123`, `lh 123` or `LH123 2026-10-05` -> (number, day or None)."""
    if not args:
        return None
    day = None
    if len(args) > 1:
        try:
            day = date.fromisoformat(args[-1]).isoformat()
            args = args[:-1]
        except ValueError:
            pass
    number = normalize_flight_number("".join(args))
    return (number, day) if number else None


def parse_track_args(args):
    ref = parse_ref(args)
    return ref if ref and ref[1] else None


# ---------- API response -> snapshot ----------

def _utc(text):
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _time(node, key):
    t = (node or {}).get(key) or {}
    return t.get("utc"), t.get("local")


def _side(node):
    node = node or {}
    airport = node.get("airport") or {}
    sched_utc, sched = _time(node, "scheduledTime")
    rev_utc, rev = _time(node, "revisedTime")
    pred_utc, pred = _time(node, "predictedTime")
    return {
        "airport": airport.get("iata") or airport.get("icao"),
        "terminal": node.get("terminal"),
        "gate": node.get("gate"),
        "belt": node.get("baggageBelt"),
        "sched_utc": sched_utc,
        "sched": sched,
        "est_utc": rev_utc or pred_utc,
        "est": rev or pred,
    }


def parse_leg(payload):
    """API list of legs -> one snapshot (earliest departure), or None when there is no data."""
    if not isinstance(payload, list):
        return None
    legs = [leg for leg in payload if isinstance(leg, dict)]
    if not legs:
        return None
    leg = min(legs, key=lambda l: _time(l.get("departure"), "scheduledTime")[0] or "~")
    return {
        "number": leg.get("number"),
        "status": leg.get("status") or "Unknown",
        "dep": _side(leg.get("departure")),
        "arr": _side(leg.get("arrival")),
    }


# ---------- change detection ----------

def flatten(s):
    d, a = s["dep"], s["arr"]
    return {
        "Status": s["status"],
        "Dep terminal": d["terminal"],
        "Dep gate": d["gate"],
        "Dep scheduled": d["sched"],
        "Dep expected": d["est"],
        "Arr terminal": a["terminal"],
        "Arr gate": a["gate"],
        "Belt": a["belt"],
        "Arr scheduled": a["sched"],
        "Arr expected": a["est"],
    }


def suppress_jitter(old, new):
    """Keep the old expected time when the API only nudged it by a few minutes.

    The baseline stays put, so slow drift still alerts once it passes JITTER from it.
    """
    out = json.loads(json.dumps(new))
    for side in ("dep", "arr"):
        o, n = old[side], out[side]
        before = _utc(o["est_utc"]) or _utc(o["sched_utc"])
        after = _utc(n["est_utc"])
        if before and after and abs(after - before) < JITTER:
            n["est_utc"], n["est"] = o["est_utc"], o["est"]
    return out


def diff_snapshots(old, new):
    a, b = flatten(old), flatten(new)
    return [(k, a[k], b[k]) for k in a if a[k] != b[k]]


# ---------- messages ----------

def fmt_time(raw):
    return raw[5:16] if raw and len(raw) >= 16 else "—"


def _show(label, value):
    return fmt_time(value) if label in TIME_LABELS else (value or "—")


def format_snapshot(number, day, s):
    d, a = s["dep"], s["arr"]
    return "\n".join([
        f"✈ {number} · {day} · {s['status']}",
        f"{d['airport'] or '?'} → {a['airport'] or '?'}",
        f"Dep {fmt_time(d['est'] or d['sched'])} (sched {fmt_time(d['sched'])})"
        f" · Terminal {d['terminal'] or '—'} · Gate {d['gate'] or '—'}",
        f"Arr {fmt_time(a['est'] or a['sched'])} (sched {fmt_time(a['sched'])})"
        f" · Terminal {a['terminal'] or '—'} · Gate {a['gate'] or '—'} · Belt {a['belt'] or '—'}",
    ])


def format_changes(number, day, changes):
    lines = [f"✈ {number} · {day}"]
    lines += [f"{label}: {_show(label, old)} → {_show(label, new)}" for label, old, new in changes]
    return "\n".join(lines)


# ---------- scheduling ----------

# (floor, interval): while more than `floor` remains before departure, poll every
# `interval`, but never step past the floor, so no window is skipped. The 6h-2h
# window matters most: gates are published 1-3 h out.
WINDOWS = [
    (timedelta(hours=72), timedelta(hours=48)),
    (timedelta(hours=24), timedelta(hours=12)),
    (timedelta(hours=6), timedelta(hours=6)),
    (timedelta(hours=2), timedelta(minutes=60)),
    (timedelta(0), timedelta(minutes=15)),
]
MIN_STEP = timedelta(minutes=10)


def poll_interval(now, snap):
    """How long to wait after a poll made at `now`."""
    if snap["status"] == "Arrived":
        return timedelta(minutes=15)
    dep = _utc(snap["dep"]["est_utc"]) or _utc(snap["dep"]["sched_utc"])
    arr = _utc(snap["arr"]["est_utc"]) or _utc(snap["arr"]["sched_utc"])
    if dep is None:
        return timedelta(hours=6)
    until = dep - now
    for floor, interval in WINDOWS:
        if until > floor:
            return min(interval, max(until - floor, MIN_STEP))
    if snap["status"] in PRE_DEPARTURE:
        return timedelta(minutes=15)
    if arr is not None:  # in flight: look again around landing, not an hour after it
        return min(timedelta(minutes=120), max(arr - now, timedelta(minutes=15)))
    return timedelta(minutes=120)


def finish_reason(now, flight):
    """Why a tracked flight should be dropped, or None to keep tracking."""
    snap = flight["snapshot"]
    if snap["status"] in CANCELLED:
        return "cancelled"
    if snap["status"] == "Arrived":
        seen = _utc(flight.get("arrived_seen"))
        if snap["arr"]["belt"] or (seen and now - seen >= timedelta(hours=1)):
            return "landed"
        return None
    arr = _utc(snap["arr"]["est_utc"]) or _utc(snap["arr"]["sched_utc"])
    dep = _utc(snap["dep"]["est_utc"]) or _utc(snap["dep"]["sched_utc"])
    end = arr + timedelta(hours=6) if arr else (dep + timedelta(hours=24) if dep else None)
    if end and now > end:
        return "no data after the arrival window"
    return None


# ---------- the tracker ----------

class FetchError(Exception):
    """The API call failed. `spent` is False when the request never reached the API."""

    def __init__(self, message="", spent=True):
        super().__init__(message)
        self.spent = spent


class Tracker:
    def __init__(self, path, fetch, send, now=lambda: datetime.now(UTC)):
        self.path, self.fetch, self.send, self.now = path, fetch, send, now
        self.lock = threading.RLock()
        self.state = self._load()

    # -- persistence --

    def _load(self):
        try:
            with open(self.path) as f:
                state = json.load(f)
        except FileNotFoundError:
            state = {}
        except json.JSONDecodeError:
            os.replace(self.path, self.path + ".corrupt")
            log("state file corrupt, moved aside")
            state = {}
        state.setdefault("flights", [])
        state.setdefault("offset", 0)
        state.setdefault("failing", False)
        state.setdefault("usage", {"month": None, "units": 0, "warned": False, "paused_notified": False})
        return state

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    def set_offset(self, offset):
        with self.lock:
            self.state["offset"] = offset
            self._save()

    # -- API budget --

    def _roll(self, now):
        usage = self.state["usage"]
        month = now.strftime("%Y-%m")
        if usage["month"] != month:
            usage.update(month=month, units=0, warned=False, paused_notified=False)
        return usage

    def _paused(self, now):
        return self._roll(now)["units"] >= PAUSE_AT * MONTHLY_UNITS

    def _spend(self, now):
        usage = self._roll(now)
        usage["units"] += UNITS_PER_CALL
        if usage["units"] >= PAUSE_AT * MONTHLY_UNITS and not usage["paused_notified"]:
            usage["warned"] = usage["paused_notified"] = True
            self.send(f"API budget 95% used ({usage['units']}/{MONTHLY_UNITS} units): "
                      "scheduled checks paused, /fetch still works")
        elif usage["units"] >= WARN_AT * MONTHLY_UNITS and not usage["warned"]:
            usage["warned"] = True
            self.send(f"API budget 80% used ({usage['units']}/{MONTHLY_UNITS} units)")

    def _lookup(self, number, day, now):
        """One API call. Returns a snapshot, or None on any failure or empty answer."""
        self._roll(now)
        try:
            raw, spent = self.fetch(number, day), True
        except FetchError as e:
            raw, spent = None, e.spent
        if spent:
            self._spend(now)
        snap = parse_leg(raw)
        if snap is not None:
            self.state["failing"] = False
        self._save()
        return snap

    # -- helpers --

    def _find(self, number, day):
        for f in self.state["flights"]:
            if f["number"] == number and f["date"] == day:
                return f
        return None

    def _matches(self, number, day):
        return [f for f in self.state["flights"]
                if f["number"] == number and (day is None or f["date"] == day)]

    def _sorted(self):
        def key(f):
            d = f["snapshot"]["dep"]
            return d["est_utc"] or d["sched_utc"] or ""
        return sorted(self.state["flights"], key=key)

    def _schedule(self, flight, now):
        snap = flight["snapshot"]
        if snap["status"] == "Arrived" and not flight.get("arrived_seen"):
            flight["arrived_seen"] = now.isoformat()
        flight["next_poll"] = (now + poll_interval(now, snap)).isoformat()

    def _apply(self, flight, snap, now, notify):
        old = flight["snapshot"]
        snap = suppress_jitter(old, snap)
        changes = diff_snapshots(old, snap)
        flight["snapshot"] = snap
        flight["failures"] = 0
        self._schedule(flight, now)
        self._save()
        if notify and changes:
            self.send(format_changes(flight["number"], flight["date"], changes))

    def _hard_fetch(self, flight):
        now = self.now()
        snap = self._lookup(flight["number"], flight["date"], now)
        if snap is None:
            self.send(FAILED)
            return
        self.send(format_snapshot(flight["number"], flight["date"], snap))
        self._apply(flight, snap, now, notify=False)

    def _choose(self, flights):
        buttons = [(f"{f['number']} · {f['date']}", f"f:{f['number']}:{f['date']}") for f in flights]
        self.send("Which flight?", buttons)

    # -- commands --

    def handle_message(self, text):
        cmd = parse_command(text)
        if cmd is None:
            return
        name, args = cmd
        with self.lock:
            if name == "track":
                self._track(args)
            elif name == "untrack":
                self._untrack(args)
            elif name == "list":
                self._list()
            elif name == "fetch":
                self._fetch_cmd(args)
            else:
                self.send(HELP)

    def handle_callback(self, data):
        parts = (data or "").split(":")
        if len(parts) != 3 or parts[0] != "f":
            return
        with self.lock:
            flight = self._find(parts[1], parts[2])
            if flight is None:
                self.send(f"Not tracked: {parts[1]}")
                return
            self._hard_fetch(flight)

    def _track(self, args):
        parsed = parse_track_args(args)
        if parsed is None:
            self.send("Usage: /track LH123 2026-10-05")
            return
        number, day = parsed
        now = self.now()
        if self._find(number, day):
            self.send(f"Already tracking {number} {day}")
            return
        if date.fromisoformat(day) < now.date() - timedelta(days=1):
            self.send("That date is in the past")
            return
        snap = self._lookup(number, day, now)
        if snap is None:
            self.send(FAILED)
            return
        flight = {"number": number, "date": day, "snapshot": snap,
                  "next_poll": None, "arrived_seen": None}
        self._schedule(flight, now)
        self.state["flights"].append(flight)
        self._save()
        self.send(format_snapshot(number, day, snap))

    def _untrack(self, args):
        ref = parse_ref(args)
        if ref is None:
            self.send("Usage: /untrack LH123")
            return
        number, day = ref
        matches = self._matches(number, day)
        if not matches:
            self.send(f"Not tracked: {number}")
        elif len(matches) > 1:
            self.send(f"Several dates tracked, use /untrack {number} <date>")
        else:
            self.state["flights"].remove(matches[0])
            self._save()
            self.send(f"Stopped tracking {number} {matches[0]['date']}")

    def _list(self):
        flights = self._sorted()
        if not flights:
            self.send(NO_FLIGHTS)
            return
        lines = []
        for f in flights:
            s = f["snapshot"]
            nxt = _utc(f["next_poll"])
            when = nxt.strftime("%H:%M UTC") if nxt else "—"
            lines.append(f"{f['number']} · {f['date']} · {s['status']}"
                         f" · dep {fmt_time(s['dep']['est'] or s['dep']['sched'])} · next check {when}")
        units = self._roll(self.now())["units"]
        lines.append(f"API units this month: {units}/{MONTHLY_UNITS}")
        self.send("\n".join(lines))

    def _fetch_cmd(self, args):
        if not args:
            flights = self._sorted()
        else:
            ref = parse_ref(args)
            if ref is None:
                self.send("Usage: /fetch [LH123]")
                return
            flights = self._matches(*ref)
            if not flights:
                self.send(f"Not tracked: {ref[0]}")
                return
        if not flights:
            self.send(NO_FLIGHTS)
        elif len(flights) == 1:
            self._hard_fetch(flights[0])
        else:
            self._choose(flights)

    # -- scheduler --

    def poll_due(self):
        with self.lock:
            now = self.now()
            for flight in list(self.state["flights"]):
                reason = finish_reason(now, flight)
                if reason:
                    self.state["flights"].remove(flight)
                    self._save()
                    self.send(f"Stopped tracking {flight['number']} {flight['date']}: {reason}")
                    continue
                due = _utc(flight["next_poll"])
                if (due and now < due) or self._paused(now):
                    continue
                snap = self._lookup(flight["number"], flight["date"], now)
                if snap is None:
                    flight["failures"] = flight.get("failures", 0) + 1
                    backoff = RETRY_AFTER_FAILURE * 2 ** min(flight["failures"] - 1, 12)
                    retry = min(backoff, poll_interval(now, flight["snapshot"]))
                    flight["next_poll"] = (now + retry).isoformat()
                    if not self.state["failing"]:
                        self.state["failing"] = True
                        self._save()
                        self.send(FAILED)
                    else:
                        self._save()
                    continue
                self._apply(flight, snap, now, notify=True)


# ---------- network edges ----------

def fetch_flight(number, day, key, timeout=20):
    """One AeroDataBox lookup. [] means the API has no data; FetchError means it failed."""
    url = (f"https://{API_HOST}/flights/number/{number}/{day}"
           "?dateLocalRole=Departure&withAircraftImage=false&withLocation=false")
    req = urllib.request.Request(url, headers={
        "x-rapidapi-key": key,
        "x-rapidapi-host": API_HOST,
        "user-agent": "homelab-flight-tracker/1",  # the default Python UA gets a Cloudflare 403 (1010)
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.status == 204 or not body:
                return []
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise FetchError(f"HTTP {e.code}") from None
    except OSError as e:  # DNS, refused, timeout: nothing reached RapidAPI
        raise FetchError(type(e).__name__, spent=False) from None
    except ValueError as e:  # unreadable body: the call was answered and billed
        raise FetchError(type(e).__name__) from None


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


def dispatch(update, chat_id, tracker, answer):
    """Route one Telegram update. Anything not from the owner's chat is ignored."""
    msg = update.get("message")
    if msg and str(msg.get("chat", {}).get("id")) == chat_id:
        tracker.handle_message(msg.get("text"))
        return
    cq = update.get("callback_query")
    if cq and str(cq.get("from", {}).get("id")) == chat_id:
        answer(cq.get("id"))
        tracker.handle_callback(cq.get("data"))


# ---------- main ----------

def scheduler_loop(tracker):
    while True:
        try:
            tracker.poll_due()
        except Exception as e:  # keep the scheduler alive
            log(f"poll failed: {type(e).__name__}")
        time.sleep(60)


def receiver_loop(tracker, tg, chat_id):
    while True:
        try:
            updates = tg.updates(tracker.state["offset"])
        except (OSError, ValueError, KeyError) as e:
            log(f"getUpdates failed: {type(e).__name__}")
            time.sleep(30)
            continue
        for update in updates:
            tracker.set_offset(update["update_id"] + 1)
            try:
                dispatch(update, chat_id, tracker, tg.answer)
            except Exception as e:  # a bad update must not kill the bot
                log(f"update failed: {type(e).__name__}")


def run():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    key = os.environ["AERODATABOX_KEY"]
    path = os.environ.get("STATE_PATH", "/data/state.json")
    tg = Telegram(token, chat_id)
    tracker = Tracker(path, lambda number, day: fetch_flight(number, day, key), tg.send)
    threading.Thread(target=scheduler_loop, args=(tracker,), daemon=True).start()
    log("flight-tracker started")
    receiver_loop(tracker, tg, chat_id)


if __name__ == "__main__":
    run()
