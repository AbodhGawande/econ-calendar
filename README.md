# econ-calendar

Self-updating Apple Calendar feed for **ISM Manufacturing PMI**, **ISM Services PMI**,
and **BLS JOLTS** release dates. All events are 10:00–10:30 AM Eastern with a
15-minute display alarm.

## Subscribe

```
https://raw.githubusercontent.com/AbodhGawande/econ-calendar/main/us-econ-ism-jolts.ics
```

In Apple Calendar: File → New Calendar Subscription → paste the URL.
The URL never changes; events update in place (stable UIDs).

## Sources (official only)

- ISM release calendar: <https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/>
- BLS JOLTS schedule: <https://www.bls.gov/schedule/news_release/jolts.htm>

Only dates literally published in those tables are used — nothing is projected
from business-day rules. When a source publishes its next year, the new dates
appear here automatically.

Both sites refuse non-browser clients (captcha wall / CDN 403), so when a
direct fetch is refused the generator reads the newest [Wayback Machine](https://web.archive.org)
snapshot of the same official page instead, and requests a fresh capture each
run so snapshots stay days old. If the newest usable snapshot is more than
120 days old the job fails loudly instead of trusting stale dates.

## How it updates

A GitHub Actions cron job ([update-calendar.yml](.github/workflows/update-calendar.yml))
re-runs [generate_calendar.py](generate_calendar.py) every Monday 12:00 UTC and
commits `us-econ-ism-jolts.ics` only when its content changed. The generated file
is validated (parses, events > 0, all 10:00 ET, weekdays only, unique UIDs)
before any commit; on any fetch/parse/validation failure the job fails visibly
and commits nothing.

Manual run:

```
gh workflow run update-calendar.yml
```
