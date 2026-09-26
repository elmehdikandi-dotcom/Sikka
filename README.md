# Sikka

Live map of Morocco's trains, placed on the track from ONCF's official timetable, with live delays.

- Every night at 00:05 (Morocco), a GitHub Action fetches the day's timetable from oncf-voyages.ma
  (one request every 5 seconds; about 40 minutes).
- Every 15 minutes from 05:00, another Action reads 24 station departure boards from the service the
  ONCF Voyages app uses (one request every 4 seconds, no login) and adds each train's delay.
- Both stop if ONCF refuses. Personal hobby project; positions are estimates from times, not GPS.
