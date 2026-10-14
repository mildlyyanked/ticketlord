"""Cross-checks that "0 new" is real. Run on GitHub (Actions → Diagnose).

1. American Cinematheque: events whose card image was uploaded recently (≈ newly announced),
   and whether ticketlord already knows them.
2. Revival Hub: which data file the live site loads, and whether its rendered calendar shows
   screenings that aren't in the feed ticketlord reads.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ticketlord import feed, theaters  # noqa: E402
from ticketlord.state import State  # noqa: E402

state = State("state/seen.json")
UA = theaters.USER_AGENT


def section(title):
    print(f"\n===== {title}")


# ---------------------------------------------------------------- American Cinematheque
section("American Cinematheque: recently created events")
now = int(datetime.now(timezone.utc).timestamp())
hits = theaters._ac_events(now - 86400, now + 365 * 86400)
print(f"{len(hits)} events in the calendar from yesterday on; latest date "
      f"{max((h.get('event_start_date') or '') for h in hits)}")
cutoff = datetime.now() - timedelta(days=7)
recent = []
for h in hits:
    img = h.get("event_card_image") or {}
    created = img.get("date") if isinstance(img, dict) else None
    try:
        created_dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S") if created else None
    except ValueError:
        created_dt = None
    if created_dt and created_dt >= cutoff:
        recent.append((created_dt, h))
print(f"{len(recent)} with an image uploaded in the last 7 days:")
for created_dt, h in sorted(recent, key=lambda x: x[0]):
    known = f"cinematheque:{h.get('id')}" in state.seen
    print(f"  {created_dt:%a %b %d %H:%M}  {h.get('event_start_date')}  {h.get('title')[:60]:60}  "
          f"{'already known' if known else 'NOT IN STATE'}")
unknown = [h for h in hits if f"cinematheque:{h.get('id')}" not in state.seen]
print(f"events ticketlord has never seen: {len(unknown)} "
      "(non-screenings like panels and past events are expected here)")
for h in unknown[:20]:
    print(f"  {h.get('event_start_date')}  {h.get('title')[:70]}")

# ---------------------------------------------------------------- Revival Hub
section("Revival Hub: which data file the site loads")
html = requests.get(feed.SITE, headers={"User-Agent": UA}, timeout=30).text
for link in sorted(set(re.findall(r'(?:href|src)="([^"]+\.json[^"]*)"', html))):
    print("  ", link)
resp = requests.head(feed.FEED_URL, timeout=30)
print("feed last-modified:", resp.headers.get("last-modified"))
data = feed.load_feed()
feed_titles = {f["name"].strip().lower() for s in data["screenings"] for f in s.get("films") or [] if f.get("name")}
print(f"feed: {len(data['screenings'])} screenings, {len(feed_titles)} distinct titles")

section("Revival Hub: live calendar vs feed")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright not installed; skipping")
    sys.exit(0)
seen_on_site = set()
requests_made = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(user_agent=UA)
    page.on("request", lambda r: requests_made.append(r.url) if r.resource_type in ("fetch", "xhr") else None)
    for path in ("", "calendar", "films"):
        page.goto(feed.SITE + path, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        for _ in range(6):
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(800)
        for a in page.query_selector_all("a[href^='/film/']"):
            t = (a.inner_text() or "").strip().lower()
            if t and len(t) < 120:
                seen_on_site.add(t)
    browser.close()
print("data requests made by the site:")
for u in sorted(set(requests_made)):
    if "google" not in u and "analytics" not in u:
        print("  ", u[:160])
missing = sorted(t for t in seen_on_site if t not in feed_titles)
print(f"{len(seen_on_site)} film titles linked on the site; {len(missing)} not in the feed:")
for t in missing[:40]:
    print("  ", t)
