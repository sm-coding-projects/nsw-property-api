"""Download NSW Valuer General PSI bulk files into a local cache directory.

Yearly archives: https://www.valuergeneral.nsw.gov.au/__psi/yearly/YYYY.zip
Weekly files:    https://www.valuergeneral.nsw.gov.au/__psi/weekly/YYYYMMDD.zip
"""

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

YEARLY_URL = "https://www.valuergeneral.nsw.gov.au/__psi/yearly/{year}.zip"
WEEKLY_URL = "https://www.valuergeneral.nsw.gov.au/__psi/weekly/{yyyymmdd}.zip"
USER_AGENT = "nsw-property-sales-backfill/1.0 (private research; Python requests)"

FIRST_YEAR = 1990
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 5


def weekly_mondays(year: int, today: date) -> list[date]:
    """All Mondays in `year` up to and including `today`."""
    d = date(year, 1, 1)
    d += timedelta(days=(7 - d.weekday()) % 7)  # first Monday of the year
    mondays = []
    while d.year == year and d <= today:
        mondays.append(d)
        d += timedelta(days=7)
    return mondays


def download_one(url: str, dest: Path, session: requests.Session) -> str:
    """Fetch url into dest. Returns 'cached' | 'downloaded' | 'missing'.

    Raises on persistent non-404 failure. 404 returns 'missing' — normal for
    weekly files not yet published.
    """
    if dest.exists() and dest.stat().st_size > 0:
        return "cached"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, timeout=120)
            if resp.status_code == 404:
                return "missing"
            resp.raise_for_status()
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(resp.content)
            tmp.rename(dest)
            return "downloaded"
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            wait = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
            log.warning("attempt %d/%d failed for %s (%s); retrying in %ds",
                        attempt, MAX_ATTEMPTS, url, exc, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def download_all(raw_dir: Path, today: date | None = None) -> dict:
    """Download yearly ZIPs 1990..last-complete-year and current-year weeklies.

    Returns {"files": [Path, ...], "missing": [url, ...]} — missing entries are
    404s (normal for recent weeklies, worth flagging for yearly files).
    """
    today = today or date.today()
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    targets: list[tuple[str, Path]] = []
    for year in range(FIRST_YEAR, today.year):
        targets.append((YEARLY_URL.format(year=year), raw_dir / f"{year}.zip"))
    for monday in weekly_mondays(today.year, today):
        yyyymmdd = monday.strftime("%Y%m%d")
        targets.append((WEEKLY_URL.format(yyyymmdd=yyyymmdd), raw_dir / f"{yyyymmdd}.zip"))

    files, missing = [], []
    for url, dest in targets:
        status = download_one(url, dest, session)
        if status == "missing":
            log.info("404 (not published): %s", url)
            missing.append(url)
        else:
            log.info("%s: %s", status, dest.name)
            files.append(dest)
    return {"files": files, "missing": missing}
