"""
Sikka local runner.

    python run.py --test     one search on oncf-voyages.ma to check it works (5 seconds)
    python run.py            open the map and keep today's ONCF timetable in data/live.json
                             (first fetch takes about 30-40 minutes, then once a night)
    python run.py --demo     example data with made-up delays (no internet needed)

Politeness: one request every 5 seconds at most, one full fetch per day, and it
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


def write_feed(doc: dict, path: pathlib.Path = LIVE) -> None:
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


HOST = "www.oncf-voyages.ma"
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

    cert = x509.load_pem_x509_certificate(ssl.get_server_certificate((HOST, 443)).encode())
    extra = []
    for _ in range(3):  # follow up to 3 missing links
        try:
            aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        except x509.ExtensionNotFound:
            break
        urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
        if not urls:
            break
        data = urllib.request.urlopen(urls[0], timeout=20).read()
        try:
            cert = x509.load_der_x509_certificate(data)
        except ValueError:
            cert = x509.load_pem_x509_certificate(data)
        extra.append(cert.public_bytes(Encoding.PEM).decode())
        if cert.issuer == cert.subject:
            break
    if not extra:
        raise RuntimeError("could not find the missing certificate")
    BUNDLE.write_text(pathlib.Path(certifi.where()).read_text(encoding="utf-8") + "\n" + "\n".join(extra), encoding="utf-8")
    return str(BUNDLE)


def session():
    import requests
    import oncf_adapter as A
    _use_system_certificates()
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "fr-FR,fr;q=0.9"})
    if BUNDLE.exists():
        s.verify = str(BUNDLE)
    try:
        A.warm_up(s)
        return s
    except requests.exceptions.SSLError:
        pass
    say("Certificate check failed. Fetching the site's missing certificate (browsers do this automatically)...")
    if _truststore_on:
        import truststore
        truststore.extract_from_ssl()
    s.verify = _complete_chain_bundle()
    A.warm_up(s)  # raises again if it still fails
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


def live_loop(gap: float) -> None:
    while True:
        today = dt.date.today()
        if have_timetable(today):
            say("Today's timetable is already saved.")
        else:
            cached = DATA / f"timetable-{today.isoformat()}.json"
            if cached.exists():
                LIVE.write_bytes(cached.read_bytes())
                say("Loaded today's saved timetable.")
            elif not fetch_day(today, gap):
                say("Will try again in 1 hour.")
                time.sleep(3600)
                continue
        tomorrow = dt.datetime.combine(today + dt.timedelta(days=1), dt.time(0, 10))
        say(f"Next fetch: {tomorrow:%d %b %H:%M}.")
        time.sleep(max(60, (tomorrow - dt.datetime.now()).total_seconds()))


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
    a = ap.parse_args()
    if a.test:
        return run_test()
    serve(a.port, not a.no_browser)
    try:
        demo_loop() if a.demo else live_loop(max(3.0, a.gap))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
