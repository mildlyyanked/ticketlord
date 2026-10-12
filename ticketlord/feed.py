"""Revival Hub's own listings feed.

revivalhub.com is a JavaScript app; the screenings aren't in its HTML. The page loads them
from a public JSON export of the site's Airtable base, which is what we read here.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

from .scraper import USER_AGENT, Screening, clean

log = logging.getLogger(__name__)

SITE = "https://revivalhub.com/"
FEED_URL = "https://storage.googleapis.com/revival-hub-ab2a8.firebasestorage.app/airtable-uploads/data-import.json"
# AMC's showtimes, which the site merges in. Mostly first-run chain showtimes, so off by default.
AMC_FEED_URL = "https://firebasestorage.googleapis.com/v0/b/revival-hub-ab2a8.firebasestorage.app/o/airtable-uploads%2Famc-import.json?alt=media"
TZ = ZoneInfo("America/Los_Angeles")


def _get_json(url: str, timeout: int = 60):
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def discover_feed_url(site: str = SITE) -> str | None:
    """The homepage preloads the feed; use that if the hardcoded URL ever moves."""
    html = requests.get(site, headers={"User-Agent": USER_AGENT}, timeout=30).text
    m = re.search(r'href="([^"]*data-import[^"]*\.json[^"]*)"', html)
    return urljoin(site, m.group(1)) if m else None


def load_feed(url: str = FEED_URL) -> dict:
    try:
        return _get_json(url)
    except (requests.RequestException, ValueError) as e:
        log.warning("feed %s failed (%s); looking for its new location on the homepage", url, e)
        found = discover_feed_url()
        if not found or found == url:
            raise
        return _get_json(found)


def film_slug(name: str, year) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug}-{year}" if year else slug


def format_when(utc_ms: int | None) -> str | None:
    if not utc_ms:
        return None
    dt = datetime.fromtimestamp(int(utc_ms) / 1000, TZ)
    return dt.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def to_screenings(data: dict, include_past: bool = False, source: str = "revivalhub") -> list[Screening]:
    venues = {v["id"]: v for v in data.get("venues", []) if isinstance(v, dict) and "id" in v}
    orgs = {o["id"]: o for o in data.get("organizations", []) if isinstance(o, dict) and "id" in o}
    now_ms = time.time() * 1000
    out = []
    for s in data.get("screenings", []):
        films = [f for f in s.get("films") or [] if f.get("name")]
        if not films:
            continue
        start = s.get("utc_start")
        if start and not include_past and int(start) < now_ms:
            continue
        title = " + ".join(clean(f["name"]) for f in films)
        tickets = [u for u in s.get("ticket_urls") or [] if u] or ([s["purchaseUrl"]] if s.get("purchaseUrl") else [])
        main = films[0]
        year = main.get("year")
        event_url = urljoin(SITE, f"film/{film_slug(main['name'], year)}") if year else None
        venue = venues.get(s.get("venue"), {}).get("name")
        presenters = [orgs[o]["name"] for o in s.get("organizations") or [] if o in orgs]
        directors = [clean(f.get("directors")) for f in films if f.get("directors")]
        details = " · ".join(filter(None, [
            ", ".join(s.get("formats") or []),
            ", ".join(s.get("categories") or []),
            ("dir. " + "; ".join(directors)) if directors else "",
            ("presented by " + ", ".join(presenters)) if presenters else "",
            clean(s.get("additionalInfo")),
        ]))
        out.append((int(start or 0), Screening(
            id=f"{source}:{s['id']}",
            title=title,
            url=tickets[0] if tickets else (event_url or SITE),
            ticket_url=tickets[0] if tickets else None,
            event_url=event_url,
            when=format_when(start),
            venue=venue,
            details=details or None,
            sold_out=bool(s.get("isSoldOut")),
            source=source,
        )))
    out.sort(key=lambda pair: pair[0])
    return [s for _, s in out]


def fetch_screenings(include_amc: bool = False, include_past: bool = False) -> list[Screening]:
    screenings = to_screenings(load_feed(), include_past=include_past)
    if include_amc:
        try:
            screenings += to_screenings(_get_json(AMC_FEED_URL), include_past=include_past, source="amc")
        except (requests.RequestException, ValueError) as e:
            log.warning("AMC feed failed: %s", e)
    return screenings
