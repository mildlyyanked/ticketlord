"""Fetch a listings page and extract screenings from it.

The parser does not depend on one exact page layout. It tries, in order:

1. schema.org ``Event`` objects in JSON-LD (common with WordPress event plugins)
2. ticket links in the HTML ("Tickets", "Buy", or links to known ticketing hosts),
   each paired with the closest title in the surrounding markup
3. links that look like event detail pages (``/event/...``, ``/screening/...``, ...)

Results from every strategy are merged and de-duplicated, so a page that changes
layout keeps working as long as it still links to tickets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Ticketing platforms used by LA rep houses and microcinemas (and in general).
TICKET_HOSTS = (
    "eventbrite.", "eventive.org", "veezi.com", "ticketing.", "tickets.",
    "showclix.com", "dice.fm", "tixr.com", "ticketmaster.", "axs.com",
    "universe.com", "squadup.com", "ticketleap.com", "brownpapertickets.com",
    "fandango.com", "atomtickets.com", "agiletix", "ticketweb.", "etix.com",
    "seetickets.", "posh.vip", "simpletix.com", "purplepass.com", "boxofficetickets",
    "ticketsource.", "ticketbud.com", "tix.com", "showtix4u.com", "cinematheque",
    "goelevent", "filmbot", "savoysystems", "gofobo.com", "stellartickets.com",
)
TICKET_TEXT_RE = re.compile(r"\b(tix|tickets?|buy|purchase|rsvp|book( now)?|get tickets|showtimes?)\b", re.I)
EVENT_PATH_RE = re.compile(r"/(events?|screenings?|films?|movies?|shows?|showtimes?|program|calendar/\w)", re.I)
GENERIC_TITLES = re.compile(
    r"^(tix|tickets?|buy( tickets)?|purchase|rsvp|book( now)?|get tickets|more( info)?|info|"
    r"details|learn more|read more|here|link|showtimes?|sold out)$",
    re.I,
)
DATE_RE = re.compile(
    r"\b((mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(,?\s+\d{4})?"
    r"(.{0,12}?\d{1,2}(:\d{2})?\s*(am|pm))?",
    re.I,
)
SOLD_OUT_RE = re.compile(r"\bsold[\s-]?out\b", re.I)
HEADING_SELECTOR = "h1, h2, h3, h4, h5, h6, [class*=title], [class*=Title], [class*=name], strong, b"
VENUE_SELECTOR = "[class*=venue], [class*=Venue], [class*=theater], [class*=theatre], [class*=location], [class*=cinema]"
MAX_CARD_TEXT = 400


@dataclass
class Screening:
    title: str
    url: str  # best link to send: ticket link if we have one, else the event page
    ticket_url: str | None = None
    event_url: str | None = None
    when: str | None = None
    date: str | None = None  # local date, YYYY-MM-DD
    venue: str | None = None
    details: str | None = None
    sold_out: bool = False
    wide_release: bool = False  # nationwide event (Fathom etc.), not a local screening
    source: str = "html"
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = make_id(self)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", False)}


def make_id(s: Screening) -> str:
    # A ticket/event URL is the most stable identity; fall back to title + date + venue.
    key = s.ticket_url or s.event_url or f"{s.title}|{s.when}|{s.venue}"
    key = normalize_url(key) if key.startswith("http") else key.lower()
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def normalize_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    # Strip common tracking params so the same link isn't seen as new.
    parsed = urlparse(url)
    parsed = parsed._replace(scheme="https", netloc=parsed.netloc.lower().removeprefix("www."))
    if parsed.query:
        kept = [p for p in parsed.query.split("&") if not p.lower().startswith(("utm_", "fbclid", "gclid", "aff"))]
        parsed = parsed._replace(query="&".join(kept))
    return parsed.geturl().rstrip("/")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------- fetching


def fetch(url: str, render_js: bool = False, timeout: int = 30) -> str:
    if url.startswith("file://"):  # a saved copy of the page, for debugging
        return open(url[len("file://"):], encoding="utf-8", errors="replace").read()
    if render_js:
        return _fetch_with_browser(url, timeout)
    resp = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def _fetch_with_browser(url: str, timeout: int) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - optional dependency
        raise SystemExit(
            "TICKETLORD_RENDER_JS=1 needs Playwright: pip install '.[js]' && playwright install chromium"
        ) from e
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        html = page.content()
        browser.close()
        return html


# ---------------------------------------------------------------- parsing


def parse(html: str, base_url: str) -> list[Screening]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, Screening] = {}
    seen_urls: set[str] = set()
    for strategy in (_from_json_ld, _from_ticket_links, _from_event_links):
        for s in strategy(soup, base_url):
            # An event-page link already covered by a ticket link is the same screening.
            # (Two ticket links sharing one event page are different showtimes, so keep both.)
            dup_page = s.source == "event-link" and normalize_url(s.event_url) in seen_urls
            if not s.title or s.id in found or dup_page:
                continue
            found[s.id] = s
            seen_urls |= {normalize_url(u) for u in (s.ticket_url, s.event_url) if u}
    return list(found.values())


def _from_json_ld(soup: BeautifulSoup, base_url: str) -> list[Screening]:
    out = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk(data):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if not any(isinstance(t, str) and t.endswith("Event") for t in types):
                continue
            offers = obj.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            ticket = offers.get("url") if isinstance(offers, dict) else None
            event_url = obj.get("url")
            location = obj.get("location") or {}
            if isinstance(location, list):
                location = location[0] if location else {}
            venue = location.get("name") if isinstance(location, dict) else location
            ticket = urljoin(base_url, ticket) if ticket else None
            event_url = urljoin(base_url, event_url) if event_url else None
            availability = str(offers.get("availability", "")) if isinstance(offers, dict) else ""
            out.append(Screening(
                title=clean(obj.get("name")),
                url=ticket or event_url or base_url,
                ticket_url=ticket,
                event_url=event_url,
                when=obj.get("startDate"),
                venue=clean(venue) if isinstance(venue, str) else None,
                details=clean(obj.get("description"))[:MAX_CARD_TEXT] or None,
                sold_out="SoldOut" in availability,
                source="json-ld",
            ))
    return out


def _walk(data):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _walk(v)
    elif isinstance(data, list):
        for v in data:
            yield from _walk(v)


def _is_ticket_link(a: Tag, href: str, base_host: str) -> bool:
    host = urlparse(href).netloc.lower()
    if any(h in host for h in TICKET_HOSTS):
        return True
    if TICKET_TEXT_RE.search(clean(a.get_text())) or TICKET_TEXT_RE.search(a.get("title", "") or ""):
        return True
    # e.g. https://theater.com/tickets/123
    return host != base_host and "ticket" in href.lower()


def _links(soup: BeautifulSoup, base_url: str):
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        yield a, urljoin(base_url, href)


def _card_for(a: Tag, is_ticket) -> Tag:
    """Smallest ancestor that looks like one listing: has a title, holds few ticket links."""
    best = a.parent
    node = a.parent
    for _ in range(8):
        if node is None or node.name in ("body", "html", "[document]"):
            break
        ticket_count = sum(1 for x in node.find_all("a", href=True) if is_ticket(x))
        if ticket_count > 3:
            break
        best = node
        if node.select_one(HEADING_SELECTOR) is not None and len(clean(node.get_text())) > 15:
            break
        node = node.parent
    return best


def _title_from_card(card: Tag, link: Tag) -> str:
    for el in card.select(HEADING_SELECTOR):
        t = clean(el.get_text())
        if t and not GENERIC_TITLES.match(t) and len(t) < 200:
            return t
    for a in card.find_all("a"):
        t = clean(a.get_text())
        if t and not GENERIC_TITLES.match(t) and not TICKET_TEXT_RE.fullmatch(t):
            return t
    t = clean(link.get_text())
    return "" if GENERIC_TITLES.match(t) else t


def _card_meta(card: Tag) -> tuple[str | None, str | None, str, bool]:
    text = clean(card.get_text(" "))
    when = None
    time_el = card.find("time")
    if time_el:
        when = time_el.get("datetime") or clean(time_el.get_text())
    if not when:
        m = DATE_RE.search(text)
        when = m.group(0) if m else None
    venue_el = card.select_one(VENUE_SELECTOR)
    venue = clean(venue_el.get_text()) if venue_el else None
    return when, venue, text[:MAX_CARD_TEXT], bool(SOLD_OUT_RE.search(text))


def _from_ticket_links(soup: BeautifulSoup, base_url: str) -> list[Screening]:
    base_host = urlparse(base_url).netloc.lower()

    def is_ticket(a: Tag) -> bool:
        href = urljoin(base_url, a.get("href", ""))
        return href.startswith("http") and _is_ticket_link(a, href, base_host)

    out = []
    for a, href in _links(soup, base_url):
        if not _is_ticket_link(a, href, base_host):
            continue
        card = _card_for(a, is_ticket)
        title = _title_from_card(card, a)
        if not title:
            continue
        event_link = next(
            (urljoin(base_url, x["href"]) for x in card.find_all("a", href=True)
             if x is not a and EVENT_PATH_RE.search(x["href"])),
            None,
        )
        when, venue, details, sold_out = _card_meta(card)
        out.append(Screening(
            title=title, url=href, ticket_url=href, event_url=event_link,
            when=when, venue=venue, details=details, sold_out=sold_out, source="ticket-link",
        ))
    return out


def _from_event_links(soup: BeautifulSoup, base_url: str) -> list[Screening]:
    base_host = urlparse(base_url).netloc.lower()
    out = []
    for a, href in _links(soup, base_url):
        parsed = urlparse(href)
        if parsed.netloc.lower() != base_host or not EVENT_PATH_RE.search(parsed.path):
            continue
        # Skip index pages like /events or /calendar themselves.
        if len([p for p in parsed.path.split("/") if p]) < 2:
            continue
        title = clean(a.get_text())
        card = _card_for(a, lambda x: False)
        if not title or GENERIC_TITLES.match(title):
            title = _title_from_card(card, a)
        if not title:
            continue
        when, venue, details, sold_out = _card_meta(card)
        out.append(Screening(
            title=title, url=href, event_url=href,
            when=when, venue=venue, details=details, sold_out=sold_out, source="event-link",
        ))
    return out
