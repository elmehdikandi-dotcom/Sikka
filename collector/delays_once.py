"""
Check live delays once, add them to today's timetable in data/live.json, and exit.
GitHub Actions runs this every 15 minutes during the day (see .github/workflows/delays.yml).
The workflow sets TZ=Africa/Casablanca so times are Moroccan.
"""
import datetime as dt
import sys

import run


def main() -> None:
    if dt.datetime.now().hour < 5:
        run.say("Before 05:00: no trains to check.")
        return
    result = run.DelayWatcher(10, gap=4.0).run(dt.date.today())
    sys.exit(1 if result is False else 0)


if __name__ == "__main__":
    main()
