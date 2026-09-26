"""
Live delays, from the service the ONCF Voyages app uses (mobile.oncf.ma).
Written from the app capture of 26 Sep 2026.

  POST /mobile/DepartingTrains  {"codeGare": "200"}
      -> the next 10 departures from that station, each with its scheduled
         time (heureDepart) and its real expected time (realDepartureTime).

No login is needed for this call. We ask about ~24 stations spread over the
network, one every few seconds, every 10 minutes during the day, and match each
departure to a train in the day's timetable by (station, scheduled time).
"""
from __future__ import annotations

import datetime as dt
import time

MOBILE = "https://mobile.oncf.ma"
MOBILE_HOST = "mobile.oncf.ma"

# Stations whose departure boards we read (ONCF codes). Spread so that every
# running train has an upcoming stop among them.
BOARD_STATIONS = [
    "303", "325", "350", "250", "231", "229", "217", "206", "200", "191", "190", "183",
    "139", "120", "110", "57", "610", "167", "363", "380", "431", "451", "490", "664",
]


class Blocked(Exception):
    pass


def warm_up(session) -> None:
    """The app's own first call when it opens."""
    session.get(MOBILE + "/mobile//healthcheck", timeout=30)


def board(session, code: str) -> list[dict]:
    r = session.post(MOBILE + "/mobile/DepartingTrains", json={"codeGare": code}, timeout=30,
                     headers={"Accept": "application/json", "Content-Type": "application/json"})
    if r.status_code in (401, 403, 429):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise Blocked("answered with a web page instead of data")
    head = data.get("head") or {}
    if head.get("errorCode", 0) not in (0, None):
        return []
    return ((data.get("body") or {}).get("trains")) or []


def _min(hms: str | None) -> int | None:
    if not hms or len(hms) < 5:
        return None
    return int(hms[:2]) * 60 + int(hms[3:5])


def observe(session, gap: float = 4.0, log=print) -> list[dict]:
    """Read every board once. Returns one observation per train departure."""
    out = []
    for i, code in enumerate(BOARD_STATIONS):
        try:
            trains = board(session, code)
        except Blocked:
            raise
        except Exception as e:
            log(f"  board {code}: {type(e).__name__}: {e}")
            trains = []
        for t in trains:
            if str(t.get("codeGamme")) == "1000":  # Supratours coach
                continue
            sched, real = _min(t.get("heureDepart")), _min(t.get("realDepartureTime"))
            if sched is None:
                continue
            delay = 0 if real is None else (real - sched) % 1440
            if delay > 720:  # early, or a clock wrap: treat as on time
                delay = 0
            out.append({"num": str(t.get("numTrain")), "station": code, "sched": f"{sched // 60:02d}:{sched % 60:02d}",
                        "delay": delay, "final": str(t.get("codeGareFinale")), "gamme": str(t.get("codeGamme"))})
        if i < len(BOARD_STATIONS) - 1:
            time.sleep(gap)
    return out


def apply(doc: dict, obs: list[dict]) -> tuple[dict, int]:
    """Attach observed delays to timetable trains. Returns (new doc, matched count)."""
    trains = [dict(t) for t in doc.get("trains", [])]
    index: dict[tuple[str, str], list[dict]] = {}
    for t in trains:
        for s in t.get("stops", []):
            if s.get("dep"):
                index.setdefault((str(s.get("oncf")), s["dep"]), []).append(t)
    matched = set()
    for o in obs:
        cands = index.get((o["station"], o["sched"]), [])
        if len(cands) > 1:
            cands = [t for t in cands if str(t["stops"][-1].get("oncf")) == o["final"]] or cands[:1]
        if not cands:
            continue
        t = cands[0]
        t["delayMin"] = o["delay"]
        t["status"] = "delayed" if o["delay"] else "on time"
        t.setdefault("code", t.get("number"))
        t["number"] = o["num"]  # the number passengers see (e.g. 1001)
        matched.add(id(t))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    new = {**doc, "trains": trains, "realtime": True, "generatedAt": now, "delaysCheckedAt": now,
           "source": "oncf-voyages.ma + ONCF app delays"}
    return new, len(matched)
