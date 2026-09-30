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
