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
