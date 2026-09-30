# Flight Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-user Telegram bot on the cluster that tracks flights by number and date and pushes gate, terminal, time and status changes.

**Architecture:** One stdlib-only Python process (`tracker.py`) with two threads: a receiver long-polling Telegram `getUpdates` and a scheduler that polls the AeroDataBox API when a flight is due. State is a JSON file on an `nfs-client` PVC. Deployed by Flux in the `monitoring` namespace, reusing the existing `telegram-secret`.

**Tech Stack:** Python 3.14 stdlib (`urllib`, `threading`, `json`, `unittest`), AeroDataBox via RapidAPI free plan, Telegram Bot API, Kustomize `configMapGenerator`, SealedSecrets, FluxCD.

**Spec:** `docs/superpowers/specs/2026-09-30-flight-tracker-design.md`

## Global Constraints

- Only the chat in `TELEGRAM_CHAT_ID` is served; every other chat or callback is ignored silently.
- Free tier only: 400 API units/month (observed: 2 units per call); counter warns at 80%, pauses scheduled polls at 95%, `/fetch` keeps working.
- Fetch failure message is exactly `Failed to fetch API`; scheduled polls send it once per failure streak.
- `/track` stores a flight only after a successful fetch. `/fetch` never adds a flight.
- Python standard library only. No `pip install`, no `apk add` at startup.
- Script is delivered by `configMapGenerator` from a real `.py` file, never inside a YAML `|` block (Python at 0-indent breaks the kustomize parser).
- Never commit a raw `Secret`; the plaintext key file stays gitignored (`**/*secret.yml`) and is sealed with `kubeseal`.
- Never log request headers, URLs with tokens, or exception text from Telegram/RapidAPI calls; log the exception type only.
- Never use a rolling image tag (`3.14-alpine` is rolling; use the full tag).
- Never `kubectl apply` config; everything goes through git and Flux. `git push` waits for the owner's explicit go-ahead.
- After editing any manifest run `.agents/scripts/validate-manifests.sh <path>`.
- All commits end with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Review Focus

Inputs and conditions the spec implies but does not enumerate, most likely first. Each has a test in the task named in brackets.

1. Flight typed as `lh 123`, `sk0486` (leading zero) or `/track@botname LH123 …` is accepted and normalised (`LH123`, `SK486`). [Task 2: `ParseTests`]
2. API returns several legs, an empty list, or a 404 for the flight: earliest departure wins; empty/404 is `Failed to fetch API` and nothing is stored. [Task 2 `LegTests`, Task 3 `TrackTests`]
3. Expected times wobble by a minute or two between polls, or an expected time first appears equal to the scheduled one: no alert. [Task 2 `DiffTests`, Task 3 `PollTests`]
4. State file missing or corrupt: start empty, move a corrupt file aside, never crash-loop. [Task 3 `PersistenceTests`]
5. Duplicate `/track`, `/untrack` or `/fetch` of an unknown flight, a date in the past, a stale button for a flight already dropped: polite one-line reply, no API call. [Task 3 `TrackTests`, `CommandTests`]
6. The API never reports `Arrived`: the flight is dropped after the arrival window instead of polling forever. [Task 2 `FinishTests`]

## File Structure

```
kubernetes/apps/monitoring/flight-tracker/
├── ks.yml                      # Flux Kustomization
├── app/
│   ├── kustomization.yml       # resources + configMapGenerator for tracker.py
│   ├── tracker.py              # the whole bot
│   ├── deployment.yml
│   ├── pvc.yml
│   └── aerodatabox-sealed.yml  # SealedSecret (key AERODATABOX_KEY)
└── tests/
    ├── samples.py              # AeroDataBox response builders
    ├── fixtures/real_lookup.json   # one recorded real response
    └── test_tracker.py         # unittest suite
kubernetes/apps/monitoring/kustomization.yml     # modify: add ./flight-tracker/ks.yml
design/decisions/flight-tracker.md               # create
design/docs/services.md                          # modify: one inventory line
.claude/CLAUDE.md                                # modify: one routing row
```

Test command used throughout (from the repo root):

```bash
python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v
```

---

### Task 1: Record the real API response

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/fixtures/real_lookup.json`

**Interfaces:**
- Produces: the confirmed response shape (field names) and the units-per-call value that Task 2's `parse_leg` and `UNITS_PER_CALL` rely on.

- [ ] **Step 1: Get one of the owner's flights for today**

Ask the owner for one flight number and its date (a flight from today is best: it has live data). Use it below as `<FLIGHT>` and `<YYYY-MM-DD>`, e.g. `LH123` and `2026-09-30`.

- [ ] **Step 2: Make one real call and keep the headers**

```bash
S=/tmp/claude-1001/-home-void-repos-Homelab/2c547049-7cc8-4268-bf26-524eb3c6cd64/scratchpad
KEY=$(cat "$S/aerodatabox.key")
curl -sS -D "$S/headers.txt" -o "$S/real.json" \
  -H "x-rapidapi-key: $KEY" -H "x-rapidapi-host: aerodatabox.p.rapidapi.com" \
  "https://aerodatabox.p.rapidapi.com/flights/number/<FLIGHT>/<YYYY-MM-DD>?dateLocalRole=Departure&withAircraftImage=false&withLocation=false"
grep -i -E "^HTTP|ratelimit|x-rapidapi" "$S/headers.txt"
python3 -m json.tool "$S/real.json" | head -80
```

Expected: `HTTP/2 200` and a JSON array. If `401/403`, the key or subscription is wrong: stop and tell the owner. If `404` or `[]`, the flight has no data: try another flight.

- [ ] **Step 3: Confirm the field names Task 2 depends on**

In the output check that each leg has: `number`, `status`, `departure.{airport.iata, terminal, gate, scheduledTime.{utc,local}, revisedTime, predictedTime}`, `arrival.{airport.iata, terminal, gate, baggageBelt, scheduledTime, revisedTime, predictedTime}`. Optional fields may be absent. If any *name* differs, change the names in Task 2's `_side`/`parse_leg` and in `tests/samples.py` before writing them.

- [ ] **Step 4: Confirm units per call**

In `headers.txt`, find the RapidAPI quota headers (names containing `ratelimit`). Compare the remaining quota against the value shown on the RapidAPI dashboard, or make the same call once more and compare the drop. Set `UNITS_PER_CALL` in Task 2 to the observed cost (the plan assumes `2`). If no header shows a cost, keep `2` and say so in the commit message.

- [ ] **Step 5: Save the fixture**

```bash
mkdir -p kubernetes/apps/monitoring/flight-tracker/tests/fixtures
cp "$S/real.json" kubernetes/apps/monitoring/flight-tracker/tests/fixtures/real_lookup.json
```

The body has no secrets. The response is used by `test_real_response_parses` in Task 2.

- [ ] **Step 6: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/tests/fixtures/real_lookup.json
git commit -m "test(flight-tracker): record a real AeroDataBox response" -m "Units per call observed: <N or 'assumed 2, no header'>." -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Pure logic — parsing, diffing, scheduling, formatting

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/app/tracker.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/samples.py`
- Create: `kubernetes/apps/monitoring/flight-tracker/tests/test_tracker.py`

**Interfaces:**
- Produces (all in `tracker.py`, all pure):
  - `parse_command(text) -> (name, args) | None`
  - `normalize_flight_number(text) -> str | None`
  - `parse_ref(args) -> (number, day | None) | None`
  - `parse_track_args(args) -> (number, day) | None`
  - `parse_leg(payload) -> snapshot | None` where snapshot is `{"number","status","dep":side,"arr":side}` and side is `{"airport","terminal","gate","belt","sched_utc","sched","est_utc","est"}`
  - `suppress_jitter(old, new) -> snapshot`
  - `diff_snapshots(old, new) -> [(label, old_raw, new_raw)]`
  - `fmt_time(raw) -> str`
  - `format_snapshot(number, day, snap) -> str`, `format_changes(number, day, changes) -> str`
  - `poll_interval(now, snap) -> timedelta`
  - `finish_reason(now, flight) -> str | None`
  - constants `UNITS_PER_CALL`, `MONTHLY_UNITS`, `WARN_AT`, `PAUSE_AT`, `FAILED`, `NO_FLIGHTS`

- [ ] **Step 1: Write `tests/samples.py`**

```python
"""Builders for AeroDataBox-shaped responses. Times are fixed around 2026-10-05."""


def leg(status="Expected", number="LH 123", dep_gate="A12", dep_terminal="1",
        dep_sched_utc="2026-10-05 08:15Z", dep_sched="2026-10-05 10:15+02:00",
        dep_est_utc=None, dep_est=None,
        arr_gate=None, arr_belt=None, arr_est_utc=None, arr_est=None):
    dep = {
        "airport": {"iata": "FRA"},
        "terminal": dep_terminal,
        "gate": dep_gate,
        "scheduledTime": {"utc": dep_sched_utc, "local": dep_sched},
    }
    if dep_est_utc:
        dep["revisedTime"] = {"utc": dep_est_utc, "local": dep_est}
    arr = {
        "airport": {"iata": "JFK"},
        "terminal": "4",
        "gate": arr_gate,
        "baggageBelt": arr_belt,
        "scheduledTime": {"utc": "2026-10-05 17:05Z", "local": "2026-10-05 13:05-04:00"},
    }
    if arr_est_utc:
        arr["revisedTime"] = {"utc": arr_est_utc, "local": arr_est}
    return {"number": number, "status": status, "departure": dep, "arrival": arr}


def payload(**kw):
    return [leg(**kw)]
```

- [ ] **Step 2: Write the failing tests in `tests/test_tracker.py`**

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v`
Expected: import error / `ModuleNotFoundError: No module named 'tracker'`.

- [ ] **Step 4: Write `app/tracker.py` (pure part)**

```python
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

def poll_interval(now, snap):
    """How long to wait after a poll made at `now`."""
    if snap["status"] == "Arrived":
        return timedelta(minutes=15)
    dep = _utc(snap["dep"]["est_utc"]) or _utc(snap["dep"]["sched_utc"])
    arr = _utc(snap["arr"]["est_utc"]) or _utc(snap["arr"]["sched_utc"])
    if dep is None:
        return timedelta(hours=6)
    until = dep - now
    if until > timedelta(hours=72):
        return timedelta(hours=48)
    if until > timedelta(hours=24):
        return timedelta(hours=12)
    if until > timedelta(hours=6):
        return timedelta(hours=6)
    if until > timedelta(hours=2):
        return timedelta(minutes=60)
    if until > timedelta(0) or snap["status"] in PRE_DEPARTURE:
        return timedelta(minutes=15)
    if arr is not None and now >= arr:
        return timedelta(minutes=15)
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v`
Expected: all tests PASS. If `test_real_response_parses` fails, the real shape differs from the documented one: fix `_side`/`parse_leg` and `samples.py` to match the fixture, then re-run.

- [ ] **Step 6: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/app/tracker.py kubernetes/apps/monitoring/flight-tracker/tests/
git commit -m "feat(flight-tracker): parsing, diffing and scheduling logic" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The `Tracker` — state, commands, polling, budget

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/tracker.py` (append)
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_tracker.py` (append before `if __name__`)

**Interfaces:**
- Consumes: everything from Task 2.
- Produces:
  - `class FetchError(Exception)`
  - `class Tracker(path, fetch, send, now=lambda: datetime.now(UTC))` where `fetch(number, day) -> list` (raises `FetchError`; `[]` means no data) and `send(text, buttons=None)` with `buttons` a list of `(label, callback_data)`.
  - `Tracker.handle_message(text)`, `Tracker.handle_callback(data)`, `Tracker.poll_due()`, `Tracker.set_offset(offset)`, attributes `.state`, `.lock`.

- [ ] **Step 1: Write the failing tests** (insert above `if __name__ == "__main__":`)

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v`
Expected: the new classes fail with `AttributeError: module 'tracker' has no attribute 'Tracker'` (or `FetchError`).

- [ ] **Step 3: Append the `Tracker` to `app/tracker.py`**

```python
# ---------- the tracker ----------

class FetchError(Exception):
    """The API call failed (HTTP error, timeout, unreadable body)."""


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
            raw = self.fetch(number, day)
        except FetchError:
            raw = None
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
                    flight["next_poll"] = (now + RETRY_AFTER_FAILURE).isoformat()
                    if not self.state["failing"]:
                        self.state["failing"] = True
                        self._save()
                        self.send(FAILED)
                    else:
                        self._save()
                    continue
                self._apply(flight, snap, now, notify=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v`
Expected: all tests PASS (Task 2's and Task 3's).

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/
git commit -m "feat(flight-tracker): tracker state, commands, polling and API budget" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Network edges, main loop, local smoke test

**Files:**
- Modify: `kubernetes/apps/monitoring/flight-tracker/app/tracker.py` (append)
- Modify: `kubernetes/apps/monitoring/flight-tracker/tests/test_tracker.py` (append before `if __name__`)

**Interfaces:**
- Consumes: `Tracker`, `FetchError`, `log`.
- Produces: `fetch_flight(number, day, key, timeout=20) -> list`, `class Telegram(token, chat_id)` with `.send(text, buttons=None)`, `.answer(callback_id)`, `.updates(offset)`, `dispatch(update, chat_id, tracker, answer)`, `run()`.

- [ ] **Step 1: Write the failing tests**

```python
import urllib.error  # noqa: E402  (add to the imports at the top of the file)
from unittest import mock  # noqa: E402


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

    def test_sends_a_custom_user_agent(self):
        # Cloudflare in front of RapidAPI answers 403 (code 1010) to Python's default one.
        self.call(return_value=fake_response(b"[]"))
        agent = self.request.get_header("User-agent")
        self.assertTrue(agent and not agent.startswith("Python-urllib"))

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
```

Put the two new imports (`import urllib.error`, `from unittest import mock`) with the other imports at the top of the file rather than inline.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v`
Expected: new classes fail with `AttributeError: ... 'fetch_flight'` / `'Telegram'` / `'dispatch'`.

- [ ] **Step 3: Append the edges and main loop to `app/tracker.py`**

```python
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
    except (OSError, ValueError) as e:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s kubernetes/apps/monitoring/flight-tracker/tests -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/
git commit -m "feat(flight-tracker): Telegram and AeroDataBox edges plus main loop" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Local smoke test against the real APIs**

This is the only end-to-end check before deploying, and it catches response-shape surprises while today's flights are still ahead. The bot token and chat id come from the gitignored local file; only one `getUpdates` consumer may run at a time, so stop this before the cluster pod starts.

```bash
S=/tmp/claude-1001/-home-void-repos-Homelab/2c547049-7cc8-4268-bf26-524eb3c6cd64/scratchpad
SEC=kubernetes/apps/monitoring/gotify-telegram/app/telegram-secret.yml
export TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN "$SEC" | cut -d'"' -f2)
export TELEGRAM_CHAT_ID=$(grep TELEGRAM_CHAT_ID "$SEC" | cut -d'"' -f2)
export AERODATABOX_KEY=$(cat "$S/aerodatabox.key")
export STATE_PATH="$S/smoke-state.json"
python3 kubernetes/apps/monitoring/flight-tracker/app/tracker.py
```

Run it in the background. Then ask the owner to send, from Telegram: `/track <FLIGHT> <DATE>`, `/list`, `/fetch`, `/untrack <FLIGHT>`. Expected: a snapshot reply, a list line, a fresh snapshot, and `Stopped tracking …`. Then stop the process and delete `$S/smoke-state.json`. If a reply is wrong or missing, fix it (with a test) before continuing.

---

### Task 5: Kubernetes manifests

**Files:**
- Create: `kubernetes/apps/monitoring/flight-tracker/ks.yml`
- Create: `kubernetes/apps/monitoring/flight-tracker/app/kustomization.yml`
- Create: `kubernetes/apps/monitoring/flight-tracker/app/pvc.yml`
- Create: `kubernetes/apps/monitoring/flight-tracker/app/deployment.yml`
- Create: `kubernetes/apps/monitoring/flight-tracker/app/aerodatabox-secret.yml` (gitignored, never committed)
- Create: `kubernetes/apps/monitoring/flight-tracker/app/aerodatabox-sealed.yml`
- Modify: `kubernetes/apps/monitoring/kustomization.yml`

**Interfaces:**
- Consumes: `telegram-secret` (created by the `gotify-telegram` Kustomization), env names `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `AERODATABOX_KEY`, `STATE_PATH` as read by `run()`.

- [ ] **Step 1: Pick the exact Python image tag**

Read the image-pinning rules in `design/decisions/images.md`, then list real tags:

```bash
curl -s "https://hub.docker.com/v2/repositories/library/python/tags?name=3.14&page_size=100" \
  | python3 -c "import sys,json,re; print('\n'.join(sorted(t['name'] for t in json.load(sys.stdin)['results'] if re.fullmatch(r'3\.14\.\d+-alpine\d+\.\d+', t['name']))))"
```

Use the highest `3.14.N-alpineX.Y` printed as `PYTHON_TAG` in Step 4. Do not use `3.14-alpine`.

- [ ] **Step 2: `ks.yml`**

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app flight-tracker
  namespace: flux-system
spec:
  targetNamespace: monitoring
  commonMetadata:
    labels:
      app.kubernetes.io/name: *app
  dependsOn:
    - name: sealed-secrets
    - name: gotify-telegram   # owns telegram-secret, which this app reuses
  path: ./kubernetes/apps/monitoring/flight-tracker/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: home-kubernetes
  wait: true
  interval: 30m
  timeout: 5m
```

- [ ] **Step 3: `app/kustomization.yml` and `app/pvc.yml`**

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./pvc.yml
  - ./deployment.yml
  - ./aerodatabox-sealed.yml
configMapGenerator:
  - name: flight-tracker-script
    namespace: monitoring   # must match the Deployment's namespace or the hashed name is not propagated
    files:
      - tracker.py
```

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: flight-tracker-data
  namespace: monitoring
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: nfs-client
  resources:
    requests:
      storage: 100Mi
```

- [ ] **Step 4: `app/deployment.yml`** (replace `PYTHON_TAG` with the tag from Step 1)

```yaml
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: flight-tracker
  namespace: monitoring
  annotations:
    reloader.stakater.com/auto: "true"
spec:
  replicas: 1
  strategy:
    type: Recreate   # one getUpdates consumer at a time; RWO PVC
  selector:
    matchLabels:
      app.kubernetes.io/name: flight-tracker
  template:
    metadata:
      labels:
        app.kubernetes.io/name: flight-tracker
    spec:
      restartPolicy: Always
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        runAsGroup: 65534
        seccompProfile:
          type: RuntimeDefault
      volumes:
        - name: script
          configMap:
            name: flight-tracker-script
        - name: data
          persistentVolumeClaim:
            claimName: flight-tracker-data
        - name: tmp
          emptyDir: {}
      containers:
        - name: tracker
          image: python:PYTHON_TAG
          command: ["python", "-u", "/scripts/tracker.py"]
          env:
            - name: STATE_PATH
              value: /data/state.json
            - name: PYTHONDONTWRITEBYTECODE
              value: "1"
          envFrom:
            - secretRef:
                name: telegram-secret
            - secretRef:
                name: aerodatabox-secret
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: script
              mountPath: /scripts
              readOnly: true
            - name: data
              mountPath: /data
            - name: tmp
              mountPath: /tmp
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 96Mi
```

Verify the tag was substituted: `! grep -n PYTHON_TAG kubernetes/apps/monitoring/flight-tracker/app/deployment.yml`

- [ ] **Step 5: Create and seal the API key secret**

```bash
S=/tmp/claude-1001/-home-void-repos-Homelab/2c547049-7cc8-4268-bf26-524eb3c6cd64/scratchpad
D=kubernetes/apps/monitoring/flight-tracker/app
KEY=$(cat "$S/aerodatabox.key")
cat > $D/aerodatabox-secret.yml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: aerodatabox-secret
  namespace: monitoring
stringData:
  AERODATABOX_KEY: "$KEY"
EOF
mise exec -- kubeseal --cert kubernetes/flux/pub-cert.pem --format yaml < $D/aerodatabox-secret.yml > $D/aerodatabox-sealed.yml
git check-ignore -v $D/aerodatabox-secret.yml
grep -c "$KEY" $D/aerodatabox-sealed.yml
```

Expected: `check-ignore` prints the `**/*secret.yml` rule; the `grep -c` prints `0` (the sealed file must not contain the plaintext key). If `check-ignore` prints nothing, stop: the plaintext file is not ignored.

- [ ] **Step 6: Wire into the namespace and render**

Add `  - ./flight-tracker/ks.yml` to the `resources:` list in `kubernetes/apps/monitoring/kustomization.yml`, then:

```bash
mise exec -- kubectl kustomize kubernetes/apps/monitoring/flight-tracker/app | grep -n -E "name: flight-tracker-script|kind:"
for f in kubernetes/apps/monitoring/flight-tracker/ks.yml kubernetes/apps/monitoring/flight-tracker/app/{pvc,deployment,aerodatabox-sealed}.yml kubernetes/apps/monitoring/kustomization.yml; do .agents/scripts/validate-manifests.sh "$f" && echo "ok $f"; done
```

Expected: the rendered ConfigMap name carries a hash suffix (`flight-tracker-script-<hash>`) and the Deployment volume references that same suffixed name; every file prints `ok`.

- [ ] **Step 7: Commit**

```bash
git add kubernetes/apps/monitoring/flight-tracker/ks.yml kubernetes/apps/monitoring/flight-tracker/app/ kubernetes/apps/monitoring/kustomization.yml
git status --short   # aerodatabox-secret.yml must NOT be listed
git commit -m "feat(flight-tracker): deploy to monitoring via Flux" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Docs, deploy, live check

**Files:**
- Create: `design/decisions/flight-tracker.md`
- Modify: `design/docs/services.md` (one line, Monitoring & Alerting section)
- Modify: `.claude/CLAUDE.md` (one routing row after the Gotify row)

- [ ] **Step 1: Write `design/decisions/flight-tracker.md`**

````markdown
# Flight tracker

**Read before editing:** `kubernetes/apps/monitoring/flight-tracker/`

## Current state

Single-user Telegram bot in `monitoring`. You send `/track LH123 2026-10-05`; it polls
AeroDataBox (RapidAPI free plan, 400 units/month) and messages gate, terminal, time and
status changes. `/list`, `/untrack`, and `/fetch` (tappable list of tracked flights, or
`/fetch LH123`) complete the interface. One stdlib-only Python file, delivered by
`configMapGenerator` `files:`; state is a JSON file on an `nfs-client` PVC.

It reuses the existing bot (`telegram-secret`, owned by the `gotify-telegram` Kustomization)
and answers only `TELEGRAM_CHAT_ID`.

## Rules

- **It is the only `getUpdates` consumer of the shared bot token.** `gotify-telegram` only
  sends. A second poller, or a webhook, on the same token makes Telegram answer `409` and
  the receiver loop will back off and log `getUpdates failed`.
- **Do not prune `gotify-telegram`** without sealing a separate `telegram-secret` for this
  app: `flight-tracker` `dependsOn` it for that Secret.
- **Polling spends the free quota.** The schedule (48 h → 60 min → 15 min → 120 min in
  flight) and the 80%/95% budget guard exist for that reason. The counter is a local
  estimate by calendar month, not RapidAPI's billing cycle; `/list` shows it. Raising
  polling frequency needs the arithmetic redone.
- **Time changes under 5 minutes are ignored** against a stored baseline, so drift still
  alerts once it passes 5 minutes from the last alerted time.
- **Never log request headers or Telegram/RapidAPI exception text** — both embed the
  credential. Log the exception type only.
- **Gate data is airport-dependent** and often appears only 1–3 h before departure; a
  missing gate is not a bug.
- **A failed API call is one message per streak** (`Failed to fetch API`); `/track` and
  `/fetch` always answer it. `/track` stores a flight only after a successful fetch.

## Verify

```bash
mise exec -- kubectl get deploy,pvc -n monitoring | grep flight-tracker
mise exec -- kubectl logs -n monitoring deploy/flight-tracker
```

Then send `/list` to the bot.
````

- [ ] **Step 2: Add the inventory line and routing row**

Read `design/docs/services.md`, then add one line under `## Monitoring & Alerting` matching its neighbours' format (name, what it is, no URL because nothing is exposed). In `.claude/CLAUDE.md`, add after the Gotify row:

```
| flight tracker, AeroDataBox, its Telegram commands   | design/decisions/flight-tracker.md    |
```

- [ ] **Step 3: Commit the docs**

```bash
git add design/decisions/flight-tracker.md design/docs/services.md .claude/CLAUDE.md
git commit -m "docs(flight-tracker): decision record, inventory line and routing row" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Ask before pushing**

Stop stale local runs (no `tracker.py` process may still hold `getUpdates`). Then ask the owner: "Ready to push to `main` so Flux deploys the tracker?" Do not push without an explicit yes.

- [ ] **Step 5: Push and watch Flux**

```bash
git push
mise exec -- flux reconcile source git home-kubernetes
mise exec -- flux reconcile kustomization cluster-apps --with-source
mise exec -- flux get kustomization flight-tracker
mise exec -- kubectl get pods -n monitoring -l app.kubernetes.io/name=flight-tracker
mise exec -- kubectl logs -n monitoring deploy/flight-tracker
```

Expected: the Kustomization is `Ready`, the pod is `Running`, and the log shows `flight-tracker started`. If the log shows `getUpdates failed: HTTPError`, another process holds the token: find and stop it.

- [ ] **Step 6: Live check**

Ask the owner to send: `/track <FLIGHT> <DATE>`, `/list`, `/fetch`, then `/untrack` a scratch flight. Confirm each reply. Then confirm the failure path once: send `/track ZZ9999 2026-10-05` (no such flight) and expect `Failed to fetch API`.

- [ ] **Step 7: Close out**

Tell the owner to regenerate the RapidAPI key (it appeared in the chat), and that after regenerating, the sealed secret needs re-sealing. Offer to do that as a separate change.
