"""Schedules read straight from theaters' own sites, so new screenings are caught as soon as
they're announced rather than when Revival Hub gets to them.

Both sites load their calendars from JSON endpoints, which is what we read here.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from .feed import TZ, film_slug, format_when
from .scraper import USER_AGENT, Screening, clean

log = logging.getLogger(__name__)

DAYS_AHEAD = 180


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_json(url: str, params: dict | None = None, timeout: int = 60):
    resp = requests.get(url, params=params, timeout=timeout,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _local_date(utc_ms: float) -> str:
    return datetime.fromtimestamp(utc_ms / 1000, TZ).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- American Cinematheque

AC_SITE = "https://www.americancinematheque.com"


def _ac_terms() -> dict[str, dict[int, str]]:
    data = _get_json(f"{AC_SITE}/wp-json/wp/v2/event/taxonomies")["taxonomies"]
    return {name: {t["term_id"]: t["name"] for t in tax.get("list", [])} for name, tax in data.items()}


def _ac_events(start: int, end: int) -> list[dict]:
    # The site's search index is named per year ("production_2026"); try this year's, then neighbours.
    year = _now().astimezone(TZ).year
    last_error: Exception | None = None
    for env in (year, year + 1, year - 1):
        try:
            data = _get_json(f"{AC_SITE}/wp-json/wp/v2/algolia_get_events",
                             {"environment": f"production_{env}", "startDate": start, "endDate": end})
        except (requests.RequestException, ValueError) as e:
            last_error = e
            continue
        if data.get("hits"):
            return data["hits"]
    if last_error:
        raise last_error
    return []


SMALL_WORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with", "vs", "from"}


def _title_case(title: str) -> str:
    """The site lists titles in ALL CAPS; make them readable ("PAN'S LABYRINTH" -> "Pan's Labyrinth")."""
    if title != title.upper():
        return title
    out: list[str] = []
    for i, word in enumerate(title.split(" ")):
        lower = word.lower()
        if re.fullmatch(r"[IVX]+|[A-Z0-9]*[&0-9][A-Z0-9&]*", word):  # II, Q&A, 4K, 35MM
            out.append(word)
        elif i and lower in SMALL_WORDS and not out[-1].endswith((":", "-", "—")):
            out.append(lower)
        else:
            out.append(lower[:1].upper() + lower[1:])
    return " ".join(out)


def _ac_when(date: str, start_time: str) -> str | None:
    try:
        dt = datetime.strptime(f"{date} {start_time.strip().upper()}", "%Y%m%d %I:%M %p")
    except ValueError:
        return None
    return dt.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def cinematheque_screenings() -> list[Screening]:
    terms = _ac_terms()
    now = int(_now().timestamp())
    hits = _ac_events(now - 86400, now + DAYS_AHEAD * 86400)
    out = []
    for h in hits:
        if h.get("post_type") not in (None, "event") or not h.get("url"):
            continue
        names = lambda tax: [terms.get(tax, {}).get(i) for i in h.get(tax) or [] if terms.get(tax, {}).get(i)]
        locations = names("event_location")
        if locations and all(loc in ("Virtual",) for loc in locations):
            continue
        date = h.get("event_start_date") or ""
        if len(date) != 8 or date < _now().astimezone(TZ).strftime("%Y%m%d"):
            continue  # past
        excerpt = clean(html.unescape(re.sub(r"<[^>]+>", " ", h.get("event_card_excerpt") or "")))
        types = [t for t in names("event_type") if t not in ("Film", "Single Feature")]
        details = " · ".join(filter(None, [
            ", ".join(names("event_format")),
            ", ".join(types),
            "Members only" if "Member Only" in names("event_access_type") else "",
            excerpt,
        ]))
        out.append(Screening(
            id=f"cinematheque:{h.get('id')}",
            title=_title_case(clean(html.unescape(h.get("title") or ""))),
            url=h["url"],
            ticket_url=h["url"],
            event_url=h["url"],
            when=_ac_when(date, h.get("event_start_time") or ""),
            date=f"{date[:4]}-{date[4:6]}-{date[6:]}",
            venue=", ".join(locations) or None,
            details=details or None,
            source="cinematheque",
        ))
    return out


# ---------------------------------------------------------------- Academy Museum

ACADEMY_API = "https://tickets.academymuseum.org/cached_api/events/available"
ACADEMY_SITE = "https://www.academymuseum.org/en/programs/detail/"
ACADEMY_CATEGORIES = [
    "Film Screening", "Film Screening: Matinee", "Film Screening: Double Feature", "Special Event", "Program",
]


def academy_screenings() -> list[Screening]:
    since = (_now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = _get_json(ACADEMY_API, {
        "event_session.start_datetime._gte": since,
        "category._in": ",".join(ACADEMY_CATEGORIES),
        "_embed": "event_session,venue",
        "_sort": "event_session.start_datetime",
    })
    templates = {t["id"]: t for t in (data.get("event_template") or {}).get("_data", [])}
    venues = {v["id"]: v.get("name") for v in (data.get("venue") or {}).get("_data", [])}
    out = []
    for sess in (data.get("event_session") or {}).get("_data", []):
        t = templates.get(sess.get("event_template_id"))
        if not t or t.get("category") not in ACADEMY_CATEGORIES:
            continue
        try:
            start = datetime.fromisoformat(sess["start_datetime"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        ms = start.timestamp() * 1000
        name = clean(t.get("name"))
        link = f"{ACADEMY_SITE}{film_slug(name, None)}-{t['id']}"
        room = venues.get(t.get("venue_id"))
        details = " · ".join(filter(None, [
            t.get("category"), clean(t.get("subtitle")), clean(t.get("summary")),
            clean(re.sub(r"<[^>]+>", " ", t.get("description") or ""))[:300],
        ]))
        out.append(Screening(
            id=f"academy:{sess['id']}",
            title=name,
            url=link,
            ticket_url=link,
            event_url=link,
            when=format_when(ms),
            date=_local_date(ms),
            venue="Academy Museum" + (f" – {room}" if room else ""),
            details=details or None,
            sold_out=bool(sess.get("sold_out")),
            source="academy",
        ))
    return out


SOURCES = {
    "cinematheque": cinematheque_screenings,
    "academy": academy_screenings,
}
