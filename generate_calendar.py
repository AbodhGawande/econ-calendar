#!/usr/bin/env python3
"""Generate us-econ-ism-jolts.ics from the official ISM and BLS schedule pages.

Only dates literally published in the source tables are used — no projection.
If anything about a source looks wrong, this script exits nonzero so the
GitHub Actions job fails visibly instead of publishing a bad calendar.

Both ismworld.org and bls.gov refuse non-browser clients (ISM serves a captcha
wall, BLS's CDN returns 403 for datacenter IPs), so a direct fetch usually only
works from a residential browser. We respect that refusal: when the direct
fetch is walled, we read the newest Internet Archive (Wayback Machine) snapshot
of the SAME official page instead, and ask the archive to take a fresh capture
each run so snapshots stay days old, not months. If the newest usable snapshot
exceeds MAX_SNAPSHOT_AGE_DAYS, we fail loudly rather than trust stale dates.

For local testing without network access:
    python generate_calendar.py --ism-file ism.html --jolts-file jolts.html
"""

import argparse
import re
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ISM_URL = "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/"
JOLTS_URL = "https://www.bls.gov/schedule/news_release/jolts.htm"
OUTPUT_FILE = "us-econ-ism-jolts.ics"
MAX_SNAPSHOT_AGE_DAYS = 120

EASTERN = ZoneInfo("America/New_York")
RELEASE_TIME = time(10, 0)
EVENT_MINUTES = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def looks_walled(text):
    return "captcha" in text.lower() or "Access Denied" in text


WB_HEADERS = {"User-Agent": "econ-calendar/1.0 (github.com/AbodhGawande/econ-calendar)"}


def wb_get(url, desc, timeout=60, params=None):
    """GET against web.archive.org with retries — the archive has flaky spells
    (e.g. the Aug 2026 outage) where some requests time out and others work."""
    for attempt in range(1, 4):
        try:
            return requests.get(url, headers=WB_HEADERS, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            print(f"{desc}: attempt {attempt}/3 failed ({exc}); retrying in 60s...")
            time_mod.sleep(60)


def fetch(url, content_marker):
    """Fetch an official page: direct first, Wayback Machine snapshot fallback.

    content_marker is text that must appear in the real page — it tells a good
    page apart from a bot-block page or an error snapshot.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code == 200 and not looks_walled(resp.text) and content_marker in resp.text:
            print(f"Fetched {url} directly.")
            return resp.text
        print(f"Direct fetch of {url} refused (HTTP {resp.status_code}"
              f"{', bot wall' if looks_walled(resp.text) else ''}); using Wayback Machine.")
    except requests.RequestException as exc:
        print(f"Direct fetch of {url} failed ({exc}); using Wayback Machine.")

    # Ask the archive for a fresh capture (best-effort; no trailing slash —
    # the /save endpoint 404s on trailing-slash URLs).
    try:
        wb_get(f"https://web.archive.org/save/{url.rstrip('/')}", "save-page-now", timeout=150)
        time_mod.sleep(45)  # give the capture a chance to be indexed
    except requests.RequestException as exc:
        print(f"Save-page-now request failed ({exc}); using existing snapshots.")

    try:
        resp = wb_get("https://web.archive.org/cdx/search/cdx", "CDX lookup",
                      params={"url": url, "output": "json", "fl": "timestamp",
                              "filter": "statuscode:200", "limit": "-8"})
        resp.raise_for_status()
        rows = resp.json()
    except (requests.RequestException, ValueError) as exc:
        fail(f"Wayback CDX lookup for {url} failed: {exc}")
    timestamps = sorted((r[0] for r in rows[1:]), reverse=True)
    if not timestamps:
        fail(f"no Wayback snapshots exist for {url}")

    for ts in timestamps:
        try:
            snap = wb_get(f"https://web.archive.org/web/{ts}id_/{url}", f"snapshot {ts}")
        except requests.RequestException as exc:
            # Connection-level failure after retries: the archive itself is
            # down, so older snapshots (same host) won't fare better.
            fail(f"Wayback Machine unreachable while fetching snapshots of {url}: {exc}")
        if snap.status_code != 200 or looks_walled(snap.text) or content_marker not in snap.text:
            continue
        snap_date = datetime.strptime(ts[:8], "%Y%m%d").date()
        age = (date.today() - snap_date).days
        if age > MAX_SNAPSHOT_AGE_DAYS:
            fail(f"newest usable Wayback snapshot of {url} is {age} days old "
                 f"(limit {MAX_SNAPSHOT_AGE_DAYS}) — refusing to trust stale dates")
        print(f"Using Wayback snapshot of {url} from {snap_date} ({age} days old).")
        return snap.text
    fail(f"no usable Wayback snapshot of {url} in the last 8 captures")


def parse_month_year(text):
    """'January 2026' -> (2026, 1), else None."""
    m = re.match(r"^\s*([A-Za-z]+)\s+(\d{4})\s*$", text)
    if not m or m.group(1).lower() not in MONTHS:
        return None
    return int(m.group(2)), MONTHS[m.group(1).lower()]


def prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_label(year, month):
    return date(year, month, 1).strftime("%b %Y")


def parse_ism(html):
    """Return events from the ISM release-dates table.

    Each row names the RELEASE month ('September 2026') with the day of month
    for each report; the data covered is the previous month (verified against
    the page's own footnotes, e.g. the Sep 1 release covers Aug data).
    """
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for table in soup.find_all("table"):
        # Header must be exactly Month / Manufacturing / Services — pages nest
        # their schedule table inside layout tables, so match the inner one.
        header = table.find("tr")
        if header is None:
            continue
        head_cells = [c.get_text(" ", strip=True) for c in header.find_all(["th", "td"])]
        if (len(head_cells) < 3 or head_cells[0] != "Month"
                or "Manufacturing" not in head_cells[1] or "Services" not in head_cells[2]):
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < 3:
                continue
            label = cells[0].get_text(" ", strip=True)
            if "supply chain planning forecast" in label.lower():
                continue
            ym = parse_month_year(label)
            if ym is None:
                fail(f"ISM table has an unrecognized month row: {label!r}")
            year, month = ym
            ref_year, ref_month = prev_month(year, month)
            for cell, series, slug in ((cells[1], "ISM Manufacturing PMI", "ism-mfg"),
                                       (cells[2], "ISM Services PMI", "ism-svc")):
                day_text = re.sub(r"[*®]", "", cell.get_text(" ", strip=True)).strip()
                if not day_text.isdigit():
                    fail(f"ISM {series} cell for {label} is not a plain day number: {day_text!r}")
                try:
                    release = date(year, month, int(day_text))
                except ValueError:
                    fail(f"ISM {series} day {day_text} is invalid for {label}")
                events.append({
                    "title": f"{series} ({month_label(ref_year, ref_month)} data)",
                    "date": release,
                    "uid": f"{slug}-{release:%Y%m%d}@econ-cal",
                })
    if not events:
        fail("ISM page parsed but no release rows found — page layout may have changed")
    return events


def parse_jolts(html):
    """Return events from the BLS JOLTS schedule table."""
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for table in soup.find_all("table"):
        header = table.find("tr")
        if header is None:
            continue
        head_cells = [c.get_text(" ", strip=True) for c in header.find_all(["th", "td"])]
        if (len(head_cells) < 3 or head_cells[0] != "Reference Month"
                or head_cells[1] != "Release Date"):
            continue
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < 3:
                continue
            ref_text = cells[0].get_text(" ", strip=True)
            date_text = cells[1].get_text(" ", strip=True)
            time_text = cells[2].get_text(" ", strip=True)
            ym = parse_month_year(ref_text)
            if ym is None:
                fail(f"JOLTS table has an unrecognized reference month: {ref_text!r}")
            try:
                release = datetime.strptime(date_text.replace(".", ""), "%b %d, %Y").date()
            except ValueError:
                fail(f"JOLTS release date {date_text!r} did not parse")
            if time_text.upper().replace(" ", "") != "10:00AM":
                fail(f"JOLTS release time is {time_text!r}, expected 10:00 AM — check the source")
            events.append({
                "title": f"JOLTS Job Openings ({month_label(*ym)} data)",
                "date": release,
                "uid": f"jolts-{release:%Y%m%d}@econ-cal",
            })
    if not events:
        fail("JOLTS page parsed but no schedule rows found — page layout may have changed")
    return events


VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def build_ics(events):
    """Deterministic ICS text: same events in -> byte-identical file out."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//econ-calendar//ISM-JOLTS//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:US Econ — ISM & JOLTS",
        "X-WR-TIMEZONE:America/New_York",
    ]
    lines.extend(VTIMEZONE.split("\n"))
    for ev in sorted(events, key=lambda e: (e["date"], e["uid"])):
        start = datetime.combine(ev["date"], RELEASE_TIME)
        end = start + timedelta(minutes=EVENT_MINUTES)
        stamp = start.replace(tzinfo=EASTERN).astimezone(ZoneInfo("UTC"))
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{ev['uid']}",
            f"DTSTAMP:{stamp:%Y%m%dT%H%M%SZ}",
            f"DTSTART;TZID=America/New_York:{start:%Y%m%dT%H%M%S}",
            f"DTEND;TZID=America/New_York:{end:%Y%m%dT%H%M%S}",
            f"SUMMARY:{ev['title']}",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ev['title']}",
            "TRIGGER:-PT15M",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def validate(ics_bytes):
    """Parse the generated file back and check every invariant. Any failure
    exits nonzero so a broken file is never committed."""
    from icalendar import Calendar

    try:
        cal = Calendar.from_ical(ics_bytes)
    except Exception as exc:
        fail(f"generated ICS does not parse: {exc}")
    uids = []
    count = 0
    for comp in cal.walk("VEVENT"):
        count += 1
        dtstart = comp["DTSTART"].dt
        if dtstart.tzinfo is None or dtstart.astimezone(EASTERN).time() != RELEASE_TIME:
            fail(f"event {comp['UID']} is not at 10:00 America/New_York: {dtstart}")
        if dtstart.weekday() >= 5:
            fail(f"event {comp['UID']} falls on a weekend: {dtstart:%A %Y-%m-%d}")
        uids.append(str(comp["UID"]))
    if count == 0:
        fail("generated ICS has zero events")
    if len(uids) != len(set(uids)):
        fail("generated ICS has duplicate UIDs")
    print(f"Validation passed: {count} events, all 10:00 ET weekdays, UIDs unique.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ism-file", help="local HTML file instead of fetching the ISM page")
    ap.add_argument("--jolts-file", help="local HTML file instead of fetching the BLS page")
    args = ap.parse_args()

    ism_html = (open(args.ism_file, encoding="utf-8").read() if args.ism_file
                else fetch(ISM_URL, "Manufacturing PMI"))
    jolts_html = (open(args.jolts_file, encoding="utf-8").read() if args.jolts_file
                  else fetch(JOLTS_URL, "Reference Month"))

    events = parse_ism(ism_html) + parse_jolts(jolts_html)

    cutoff = date.today() - timedelta(days=30)
    kept = [e for e in events if e["date"] >= cutoff]
    dropped = len(events) - len(kept)
    for prefix in ("ism-mfg", "ism-svc", "jolts"):
        if not any(e["uid"].startswith(prefix) for e in kept):
            fail(f"no upcoming events for series {prefix} — refusing to publish a partial calendar")

    print(f"Parsed {len(events)} events ({dropped} older than 30 days dropped):")
    for ev in sorted(kept, key=lambda e: e["date"]):
        print(f"  {ev['date']:%Y-%m-%d} {ev['date']:%a} 10:00 ET  {ev['title']}")

    ics = build_ics(kept)
    validate(ics.encode("utf-8"))
    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    print(f"Wrote {OUTPUT_FILE} with {len(kept)} events.")


if __name__ == "__main__":
    main()
