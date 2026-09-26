"""
ONCF adapter, written from the capture recorded on 26 Sep 2026.

What oncf-voyages.ma offers (from that capture):
  - POST /api/availability : the ticket search. For one origin/destination
    and a start time it returns about 11 journeys. Each journey lists its
    train segments: train number, product range, and departure/arrival times
    at the two ends of the segment. No intermediate stops, no delays.
  - GET /cache/stations    : every station with its code and coordinates.
  - The website has no live traffic / delay endpoint ("Info trafic" is only
    in the mobile app).

How we rebuild a full timetable from that:
  For every station along each corridor we search "this station -> each end
  of the corridor". A train that stops at a station shows up with its time
  there. Collecting all those sightings gives each train's list of stops
  and times for the day.
"""
from __future__ import annotations

import datetime as dt
import time

BASE = "https://www.oncf-voyages.ma"
AVAILABILITY = BASE + "/api/availability"

# ONCF station code -> name. Only stations drawn on the Sikka map.
STATIONS = {
    "303": "Tanger Ville", "317": "Asilah", "325": "Ksar El Kebir", "337": "Souk El Arbaa",
    "339": "Mechra Bel Ksiri", "350": "Sidi Kacem", "264": "Sidi Slimane", "259": "Sidi Yahya El Gharb",
    "250": "Kenitra", "238": "Sale Tabriquet", "237": "Sale", "231": "Rabat Ville", "229": "Rabat Agdal",
    "228": "Rabat Riad", "227": "Temara", "223": "Skhirat", "221": "Bouznika", "217": "Mohammedia",
    "213": "Ain Sebaa", "200": "Casa Voyageurs", "206": "Casa Port", "193": "Mers Sultan",
    "191": "Casa Oasis", "192": "Facultes", "194": "Ennassim", "187": "Bouskoura",
    "190": "Aeroport Mohammed V", "183": "Berrechid", "140": "Sidi El Aidi", "139": "Settat",
    "120": "Benguerir", "110": "Marrakech", "77": "Youssoufia", "57": "Safi", "625": "Azemmour",
    "610": "El Jadida", "155": "Ras El Ain", "159": "Sidi Hajjaj", "167": "Khouribga", "173": "Oued Zem",
    "362": "Meknes Al Amir", "363": "Meknes", "365": "Sebaa Aioun", "367": "Ain Taoujdate", "380": "Fes",
    "423": "Oued Amlil", "431": "Taza", "441": "Guercif", "451": "Taourirt", "459": "El Aioun",
    "490": "Oujda", "666": "Nador Sud", "664": "Nador Ville", "662": "Beni Nsar",
}

# Service corridors, in running order (ONCF codes).
CORRIDORS = [
    ["303", "250"],                                                        # Al Boraq LGV
    ["303", "317", "337", "325", "339", "350"],                            # Tanger conventional line
    ["206", "200", "213", "217", "221", "223", "227", "228", "229", "231", "237", "238", "250"],  # Casa - Kenitra
    ["250", "259", "264", "350", "362", "363", "365", "367", "380"],       # Kenitra - Fes
    ["380", "423", "431", "441", "451", "459", "490"],                     # Fes - Oujda
    ["451", "666", "664", "662"],                                          # Taourirt - Nador
    ["200", "193", "191", "192", "194", "187", "183", "140", "139", "120", "110"],  # Casa - Marrakech
    ["206", "200", "193", "191", "192", "194", "187", "190"],              # Airport shuttle
    ["200", "191", "625", "610"],                                          # El Jadida
    ["120", "77", "57"],                                                   # Safi
    ["200", "183", "155", "159", "167", "173"],                            # Oued Zem
]

GAMME = {"10002": "boraq", "48": "tnr", "58": "atlas"}
PRODUCT = {"boraq": "Al Boraq", "atlas": "Al Atlas", "tnr": "TNR", "airport": "Navette aeroport",
           "regional": "Al Atlas (regional)", "night": "Train de nuit"}
REGIONAL_ENDS = {"610", "57", "173", "167", "625", "77"}


class Blocked(Exception):
    """The site refused us (403/429 or a non-JSON page). Stop, don't retry."""


def queries() -> list[tuple[str, str]]:
    seen, out = set(), []
    for c in CORRIDORS:
        for s in c:
            for end in (c[0], c[-1]):
                if s != end and (s, end) not in seen:
                    seen.add((s, end))
                    out.append((s, end))
    return out


def warm_up(session) -> None:
    """Open the home page once, like a browser would, to get its cookies."""
    session.get(BASE + "/", timeout=30)


def search(session, frm: str, to: str, when: dt.datetime) -> dict:
    body = {
        "codeGareDepart": frm, "codeGareArrivee": to, "codeNiveauConfort": 2,
        "dateDepartAller": when.strftime("%Y-%m-%dT%H:%M:00+00:00"),
        "dateDepartAllerMax": None, "dateDepartRetour": None, "dateDepartRetourMax": None,
        "isTrainDirect": None, "isPreviousTrainAller": None, "isTarifReduit": True,
        "adulte": 1, "kids": 0,
        "listVoyageur": [{"numeroClient": None, "codeTarif": None, "codeProfilDemographique": "3", "dateNaissance": None}],
        "booking": True, "isEntreprise": False, "token": "", "numeroContract": "", "codeTiers": "",
        "iTravel": False, "isActive": False,
    }
    r = session.post(AVAILABILITY, json=body, timeout=40,
                     headers={"Referer": BASE + "/", "Content-Type": "application/json"})
    if r.status_code in (401, 403, 429):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        raise Blocked("the site answered with a web page instead of data (maybe a captcha)")


def _t(s: str) -> dt.datetime:
    # The site labels times +00:00 but they are Moroccan wall-clock times.
    return dt.datetime.fromisoformat(s[:19])


def segments(payload: dict) -> list[dict]:
    """All train segments found in one availability answer."""
    out = []
    body = (payload or {}).get("body") or {}
    for journey in (body.get("departurePath") or []):
        for sg in journey.get("listSegments") or []:
            gamme = str(sg.get("codeGamme") or "")
            if gamme == "1000":  # Supratours coach, not a train
                continue
            out.append({
                "train": str(sg.get("codeTrainAutoCar")),
                "gamme": gamme,
                "classif": sg.get("codeClassification") or "",
                "from": str(sg.get("codeGareDepart")), "dep": _t(sg["dateHeureDepart"]),
                "to": str(sg.get("codeGareArrivee")), "arr": _t(sg["dateHeureArrivee"]),
            })
    return out


def collect(session, day: dt.date, start: dt.datetime, gap: float, log=print, stop_early=None) -> list[dict]:
    """Run every query for `day`, page by page. Returns all segments seen."""
    found = []
    qs = queries()
    for i, (frm, to) in enumerate(qs, 1):
        when, pages = start, 0
        while pages < 8:
            payload = search(session, frm, to, when)
            pages += 1
            segs = segments(payload)
            found.extend(segs)
            deps = [_t(j["dateTimeDepart"]) for j in ((payload.get("body") or {}).get("departurePath") or [])]
            time.sleep(gap)
            if not deps or max(deps) <= when or max(deps).date() > day:
                break
            when = max(deps) + dt.timedelta(minutes=1)
        log(f"  [{i:3d}/{len(qs)}] {STATIONS[frm]} -> {STATIONS[to]}: {pages} page(s), {len(found)} segments so far")
        if stop_early and stop_early():
            break
    return found


def classify(gamme: str, stops: list[dict]) -> str:
    kind = GAMME.get(gamme, "atlas")
    codes = {s["oncf"] for s in stops}
    if "190" in codes and kind in ("tnr", "atlas"):
        return "airport"
    if kind == "atlas":
        dep = stops[0]["_t"]
        hours = (stops[-1]["_t"] - dep).total_seconds() / 3600
        if hours >= 6 and (dep.hour >= 19 or dep.hour < 2):
            return "night"
        if stops[0]["oncf"] in REGIONAL_ENDS or stops[-1]["oncf"] in REGIONAL_ENDS:
            return "regional"
    return kind


def assemble(segs: list[dict], day: dt.date) -> list[dict]:
    """Turn segment sightings into trains with ordered stops."""
    trains: dict[str, dict] = {}
    for s in segs:
        if s["from"] not in STATIONS or s["to"] not in STATIONS:
            pass  # still useful: we keep the ends we know
        t = trains.setdefault(s["train"], {"gamme": s["gamme"], "classif": s["classif"], "st": {}})
        a = t["st"].setdefault(s["from"], {})
        a["dep"] = min(a.get("dep", s["dep"]), s["dep"])
        b = t["st"].setdefault(s["to"], {})
        b["arr"] = min(b.get("arr", s["arr"]), s["arr"])
    out = []
    for num, t in trains.items():
        stops = []
        for code, v in t["st"].items():
            arr, dep = v.get("arr"), v.get("dep")
            if arr and dep and arr > dep:
                arr = dep
            stops.append({"oncf": code, "arr": arr, "dep": dep, "_t": dep or arr})
        stops.sort(key=lambda x: x["_t"])
        stops = [x for x in stops if x["oncf"] in STATIONS]
        if len(stops) < 2 or stops[0]["_t"].date() != day:
            continue
        kind = classify(t["gamme"], stops)
        out.append({
            "number": num,
            "product": PRODUCT[kind],
            "type": kind,
            "stops": [{
                "oncf": x["oncf"], "station": STATIONS[x["oncf"]],
                "arr": x["arr"].strftime("%H:%M") if (x["arr"] and i) else None,
                "dep": x["dep"].strftime("%H:%M") if (x["dep"] and i < len(stops) - 1) else None,
            } for i, x in enumerate(stops)],
        })
    out.sort(key=lambda t: t["stops"][0]["dep"] or "")
    return out


def _usable(payload: dict) -> bool:
    head = (payload or {}).get("head") or {}
    return head.get("errorCode", 0) == 0 and bool(((payload or {}).get("body") or {}).get("departurePath"))


def build_timetable(session, day: dt.date, gap: float = 5.0, log=print) -> list[dict]:
    """Whole-day timetable for `day`. Tries from 00:01 so trains already running
    are included; if the site refuses past times, starts from now instead."""
    warm_up(session)
    start = dt.datetime.combine(day, dt.time(0, 1))
    now = dt.datetime.now()
    if start < now:
        try:
            ok = _usable(search(session, "200", "229", start))
        except Blocked:
            raise
        except Exception:
            ok = False
        time.sleep(gap)
        if not ok:
            log("  The site only shows upcoming trains, so trains that left earlier today are missing.")
            start = now.replace(second=0, microsecond=0)
    segs = collect(session, day, start, gap, log)
    return assemble(segs, day)


def test(session, log=print) -> list[dict]:
    """One search (Casa Voyageurs -> Rabat Agdal from now) to check access."""
    warm_up(session)
    segs = segments(search(session, "200", "229", dt.datetime.now()))
    for s in segs[:12]:
        log(f"  train {s['train']:>5}  {STATIONS.get(s['from'], s['from'])} {s['dep']:%H:%M} -> "
            f"{STATIONS.get(s['to'], s['to'])} {s['arr']:%H:%M}")
    return segs
