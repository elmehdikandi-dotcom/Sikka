"""
Fetch today's ONCF timetable once, save it in data/, and exit.
GitHub Actions runs this every night (see .github/workflows/update.yml).
The workflow sets TZ=Africa/Casablanca so "today" is the Moroccan date.
"""
import datetime as dt
import sys

import run


def main() -> None:
    ok = run.fetch_day(dt.date.today(), gap=5.0)
    # keep the last 7 daily copies so the project stays small
    for old in sorted(run.DATA.glob("timetable-*.json"))[:-7]:
        old.unlink()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
