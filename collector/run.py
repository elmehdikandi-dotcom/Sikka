"""
Sikka local runner.

    python run.py --test     one search on oncf-voyages.ma to check it works (5 seconds)
    python run.py            open the map and keep today's ONCF timetable in data/live.json
                             (first fetch takes about 30-40 minutes, then once a night)
    python run.py --refetch  fetch again now, even if today's timetable is already saved
    python run.py --every 6  also refetch every 6 hours during the day (minimum 3)
    python run.py --test-delays   read one live departure board (Casa Voyageurs)
    python run.py --delays 15     check delays every 15 min instead of 10 (0 = off)
    python run.py --demo     example data with made-up delays (no internet needed)

Politeness: one request every 5 seconds at most, one full timetable fetch per day,
delays: 24 departure boards every 10 minutes (one every 4 s) from 05:00. It
stops at once if the site refuses (403/429 or a captcha page).
"""
import argparse
import datetime as dt
import functools
import http.server
import json
import os
import pathlib
import random
import sys
import threading
import time
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = ROOT / "data"
LIVE = DATA / "live.json"
sys.path.insert(0, str(HERE))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/153.0.0.0 Safari/537.36")


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def write_feed(doc: dict, path: pathlib.Path | None = None) -> None:
    path = path or LIVE
    DATA.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve(port: int, open_browser: bool) -> None:
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        sys.exit(f"Port {port} is busy. Close the other Sikka window, or run: python run.py --port 8001")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    say(f"Map is at http://localhost:{port}/  (keep this window open; Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(f"http://localhost:{port}/")


HOSTS = ("www.oncf-voyages.ma", "mobile.oncf.ma")
BUNDLE = HERE / "ca-bundle.pem"
_truststore_on = False


def _use_system_certificates() -> None:
    """Check certificates the way Windows/macOS do (handles work-laptop and antivirus certificates)."""
    global _truststore_on
    try:
        import truststore
        truststore.inject_into_ssl()
        _truststore_on = True
    except Exception:
        pass


def _load_certs(data: bytes) -> list:
    """Read a downloaded certificate in any of the usual formats:
    single certificate (DER or PEM) or a bundle (PKCS#7 .p7c/.p7b, DER or PEM)."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs7
    loaders = (
        lambda d: [x509.load_der_x509_certificate(d)],
        lambda d: x509.load_pem_x509_certificates(d),
        lambda d: pkcs7.load_der_pkcs7_certificates(d),
        lambda d: pkcs7.load_pem_pkcs7_certificates(d),
    )
    for load in loaders:
        try:
            certs = load(data)
            if certs:
                return list(certs)
        except Exception:
            continue
    return []


def _complete_chain_bundle() -> str:
    """oncf-voyages.ma doesn't send its intermediate certificate. Browsers fetch it
    automatically from the address written inside the site's certificate (AIA);
    we do the same, add it to the normal trusted list, and keep full verification on."""
    import ssl
    import urllib.request
    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

    extra, problems = [], []
    for host in HOSTS:
        try:
            cert = x509.load_pem_x509_certificate(ssl.get_server_certificate((host, 443)).encode())
        except Exception as e:
            problems.append(f"{host}: {e}")
            continue
        _follow_chain(cert, extra, problems)
    if not extra:
        raise RuntimeError("could not fetch the missing certificate. " + " | ".join(problems))
    BUNDLE.write_text(pathlib.Path(certifi.where()).read_text(encoding="utf-8") + "\n" + "\n".join(extra), encoding="utf-8")
    return str(BUNDLE)


def _follow_chain(cert, extra: list, problems: list) -> None:
    import urllib.request
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
    for _ in range(4):  # follow up to 4 missing links
        if cert.issuer == cert.subject:
            break
        try:
            aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
        issuer = None
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                data = urllib.request.urlopen(req, timeout=20).read()
            except Exception as e:
                problems.append(f"{url}: {e}")
                continue
            certs = _load_certs(data)
            if not certs:
                problems.append(f"{url}: unreadable ({len(data)} bytes, starts {data[:12]!r})")
                continue
            for c in certs:
                extra.append(c.public_bytes(Encoding.PEM).decode())
            issuer = next((c for c in certs if c.subject == cert.issuer), certs[0])
            break
        if issuer is None:
            break
        cert = issuer


def session(warm=None):
    """A web session that passes the certificate check. `warm` is the first call
    to make (the website's home page by default, or the app service's health check)."""
    import requests
    import oncf_adapter as A
    warm = warm or A.warm_up
    _use_system_certificates()
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "fr-FR,fr;q=0.9"})
    if BUNDLE.exists():
        s.verify = str(BUNDLE)
    try:
        warm(s)
        return s
    except requests.exceptions.SSLError:
        pass
    say("Certificate check failed. Fetching the site's missing certificate (browsers do this automatically)...")
    if _truststore_on:
        import truststore
        truststore.extract_from_ssl()
    s.verify = _complete_chain_bundle()
    warm(s)  # raises again if it still fails
    say("Certificate fixed. Verification stays on.")
    return s


# ------------------------------------------------------------------ modes
def run_test() -> None:
    import oncf_adapter as A
    say("Asking oncf-voyages.ma for trains Casa Voyageurs -> Rabat Agdal...")
    try:
        segs = A.test(session(), log=print)
    except A.Blocked as e:
        sys.exit(f"The site refused the request ({e}). Stop here and tell Claude.")
    except Exception as e:
        sys.exit(f"It didn't work: {type(e).__name__}: {e}\nSend this message to Claude.")
    if segs:
        say(f"It works: {len(segs)} train segments received. Next step: python run.py")
    else:
        say("The site answered but listed no trains. Try again during the day, or tell Claude.")


def fetch_day(day: dt.date, gap: float) -> bool:
    import oncf_adapter as A
    say(f"Fetching the timetable for {day:%A %d %B} from oncf-voyages.ma.")
    say(f"About {len(A.queries())} searches, one every {gap:.0f} s. This takes a while; the map already works meanwhile.")
    try:
        trains = A.build_timetable(session(), day, gap=gap, log=print)
    except A.Blocked as e:
        say(f"The site refused us ({e}). Stopping so we stay polite. Tell Claude what happened.")
        return False
    except Exception as e:
        say(f"Fetch failed: {type(e).__name__}: {e}")
        return False
    if not trains:
        say("No trains found. Nothing written.")
        return False
    doc = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "serviceDate": day.isoformat(), "source": "oncf-voyages.ma", "realtime": False,
           "example": False, "trains": trains}
    write_feed(doc, DATA / f"timetable-{day.isoformat()}.json")
    write_feed(doc)
    say(f"Done: {len(trains)} trains saved. The map picks them up within a minute.")
    return True


def have_timetable(day: dt.date) -> bool:
    try:
        return json.loads(LIVE.read_text(encoding="utf-8")).get("serviceDate") == day.isoformat()
    except Exception:
        return False


class DelayWatcher:
    """Every few minutes, read ONCF departure boards and add delays to today's timetable."""

    def __init__(self, every_min: float, gap: float):
        self.every = every_min * 60
        self.gap = gap
        self.sess = None
        self.off = every_min <= 0
        self.last = 0.0

    def due(self) -> bool:
        return not self.off and time.time() - self.last >= self.every and 5 <= dt.datetime.now().hour

    def run(self, day: dt.date) -> bool | None:
        """True: delays saved. False: failed. None: no timetable for today yet."""
        import delays as D
        self.last = time.time()
        base_path = DATA / f"timetable-{day.isoformat()}.json"
        try:
            live = json.loads(LIVE.read_text(encoding="utf-8"))
        except Exception:
            live = {}
        if live.get("serviceDate") == day.isoformat() and live.get("trains"):
            base = live                      # keeps delays seen earlier today
        elif base_path.exists():
            base = json.loads(base_path.read_text(encoding="utf-8"))
        else:
            say("No timetable for today yet, so no delays to attach.")
            return None
        try:
            if self.sess is None:
                self.sess = session(D.warm_up)
            obs = D.observe(self.sess, gap=self.gap, log=print)
        except D.Blocked as e:
            say(f"The ONCF app service refused us ({e}). Delays switched off for this run; times still work.")
            self.off = True
            return False
        except Exception as e:
            say(f"Delay check failed: {type(e).__name__}: {e}")
            self.sess = None
            return False
        doc, n = D.apply(base, obs)
        write_feed(doc)
        late = sum(1 for t in doc["trains"] if (t.get("delayMin") or 0) >= 5)
        say(f"Delays updated: {n} trains matched, {late} running 5+ min late.")
        return True

    def sleep_until(self, when: dt.datetime, day: dt.date) -> None:
        while dt.datetime.now() < when:
            if self.due():
                self.run(day)
            left = (when - dt.datetime.now()).total_seconds()
            nap = left if self.off else min(left, max(5.0, self.every - (time.time() - self.last)))
            time.sleep(max(1.0, nap))


def live_loop(gap: float, refetch: bool = False, every_h: float = 0, delay_min: float = 10) -> None:
    force = refetch
    watcher = DelayWatcher(delay_min, gap=4.0)
    failures = 0
    while True:
        today = dt.date.today()
        cached = DATA / f"timetable-{today.isoformat()}.json"
        if not force and have_timetable(today):
            say("Today's timetable is already saved.")
        elif not force and cached.exists():
            LIVE.write_bytes(cached.read_bytes())
            say("Loaded today's saved timetable.")
        elif not fetch_day(today, gap):
            failures += 1
            wait = 5 if failures <= 3 else 60
            say(f"Will try again in {wait} minutes.")
            watcher.sleep_until(dt.datetime.now() + dt.timedelta(minutes=wait), today)
            continue
        failures = 0
        watcher.last = 0.0  # check delays right away
        midnight = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(0, 10))
        nxt = midnight
        if every_h:
            nxt = min(midnight, dt.datetime.now() + dt.timedelta(hours=every_h))
        say(f"Next timetable fetch: {nxt:%d %b %H:%M}." + ("" if watcher.off else f" Delays every {delay_min:g} min (05:00-24:00)."))
        watcher.sleep_until(nxt, today)
        force = bool(every_h) and nxt < midnight


def run_test_delays() -> None:
    import delays as D
    say("Asking the ONCF app service for departures from Casa Voyageurs...")
    try:
        s = session(D.warm_up)
        trains = D.board(s, "200")
    except D.Blocked as e:
        sys.exit(f"The ONCF app service refused the request ({e}). Stop here and tell Claude.")
    except Exception as e:
        sys.exit(f"It didn't work: {type(e).__name__}: {e}\nSend this message to Claude.")
    for t in trains:
        sched, real = (t.get("heureDepart") or "")[:5], (t.get("realDepartureTime") or "")[:5]
        flag = "" if sched == real else f"  (expected {real})"
        print(f"  {sched}  train {t.get('numTrain'):>6}  to station {t.get('codeGareFinale')}{flag}")
    say(f"It works: {len(trains)} departures received." if trains else "Answered, but no departures listed right now.")


def demo_loop() -> None:
    base = json.loads((DATA / "indicative.json").read_text(encoding="utf-8"))["trains"]
    rng, delays = random.Random(), {}
    while True:
        trains = []
        for t in base:
            d = delays.get(t["number"])
            d = (0 if rng.random() < 0.72 else rng.choice([2, 4, 6, 9, 13, 18, 25, 40])) if d is None \
                else max(0, d + rng.choice([-2, -1, 0, 0, 0, 1, 2, 3]))
            delays[t["number"]] = d
            status = "cancelled" if rng.random() < 0.01 else ("delayed" if d else "on time")
            trains.append({**t, "delayMin": d, "status": status})
        write_feed({"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "source": "Sikka demo (example data)", "example": True, "realtime": True, "trains": trains})
        say(f"Example feed updated ({len(trains)} trains).")
        time.sleep(60)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="one search to check access")
    ap.add_argument("--demo", action="store_true", help="example data, no internet")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--gap", type=float, default=5.0, help="seconds between two requests")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--test-delays", action="store_true", help="one departure board from the ONCF app service")
    ap.add_argument("--delays", type=float, default=10, metavar="MINUTES",
                    help="check live delays every MINUTES minutes (default 10, minimum 5; 0 = off)")
    ap.add_argument("--refetch", action="store_true", help="fetch again now, even if today's timetable is saved")
    ap.add_argument("--every", type=float, default=0, metavar="HOURS",
                    help="also refetch during the day every HOURS hours (minimum 3)")
    a = ap.parse_args()
    if a.test:
        return run_test()
    if a.test_delays:
        return run_test_delays()
    if 0 < a.delays < 5:
        say("--delays is at least 5 minutes. Using 5.")
        a.delays = 5
    if a.every and a.every < 3:
        say("--every is at least 3 hours: each fetch is ~350 searches on ONCF and takes ~40 min. Using 3.")
        a.every = 3
    serve(a.port, not a.no_browser)
    try:
        demo_loop() if a.demo else live_loop(max(3.0, a.gap), a.refetch, a.every, a.delays)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
