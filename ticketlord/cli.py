"""Command line entry point.

    ticketlord check          fetch once, notify about new screenings
    ticketlord watch -i 600   keep checking every 10 minutes
    ticketlord inspect        show what the parser finds (no notifications, no state change)
    ticketlord test-notify    send a test push to every configured channel
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

from urllib.parse import urlparse

from . import feed, notify
from .scraper import Screening, fetch, parse
from .state import State

log = logging.getLogger("ticketlord")


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def keywords() -> list[str]:
    """TICKETLORD_KEYWORDS (comma-separated) plus keywords.txt (one per line, # for comments)."""
    words = os.environ.get("TICKETLORD_KEYWORDS", "").split(",")
    path = Path(os.environ.get("TICKETLORD_KEYWORDS_FILE") or "keywords.txt")
    if path.exists():
        words += [line.split("#", 1)[0] for line in path.read_text().splitlines()]
    return sorted({w.strip().lower() for w in words if w.strip()})


def matches_keywords(s: Screening, words: list[str]) -> bool:
    haystack = " ".join(filter(None, (s.title, s.venue, s.details))).lower()
    return any(w in haystack for w in words)


def get_screenings(url: str, debug_dir: str | None = None) -> list[Screening]:
    if urlparse(url).netloc.removeprefix("www.") == "revivalhub.com":
        return feed.fetch_screenings(include_amc=env_flag("TICKETLORD_INCLUDE_AMC"))
    # Any other site: generic HTML scraping.
    html = fetch(url, render_js=env_flag("TICKETLORD_RENDER_JS"))
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        Path(debug_dir, "page.html").write_text(html)
    # A saved file's relative links belong to the real site.
    base = (os.environ.get("TICKETLORD_URL") or "https://revivalhub.com/") if url.startswith("file://") else url
    return parse(html, base)


def check(args) -> int:
    url = (os.environ.get("TICKETLORD_URL") or "https://revivalhub.com/")
    state = State(os.environ.get("TICKETLORD_STATE_FILE") or "state/seen.json")
    notifiers = notify.from_env()
    if not notifiers and not args.dry_run:
        log.error("No notification channel configured (set NTFY_TOPIC, PUSHOVER_* or TELEGRAM_*).")
        return 2

    screenings = get_screenings(url)
    if not screenings:
        # Most likely the site changed layout or blocked us; don't silently report "nothing new".
        log.error("Parsed 0 screenings from %s. Run `ticketlord inspect` to debug.", url)
        return 1

    words = keywords()
    only_kw = env_flag("TICKETLORD_ONLY_KEYWORDS")
    # First ever run: remember everything currently listed instead of sending a flood.
    seeding = not state.exists and not args.notify_existing
    new = [s for s in screenings if state.is_new(s.id)]
    log.info("%d screenings on page, %d new%s", len(screenings), len(new), " (first run: seeding)" if seeding else "")

    for s in screenings:
        if not state.is_new(s.id):
            state.mark(s.id, s.title)  # refresh last_seen

    to_send: list[tuple[Screening, bool]] = []
    for s in new:
        urgent = matches_keywords(s, words)
        if not seeding and (urgent or not only_kw):
            to_send.append((s, urgent))
        else:
            state.mark(s.id, s.title)

    # One push per screening, unless a venue just dropped a whole calendar: then the
    # non-keyword ones are rolled into a single summary.
    batch_over = int(os.environ.get("TICKETLORD_BATCH_OVER") or 5)
    normal = [s for s, urgent in to_send if not urgent]
    batched = normal if len(normal) > batch_over else []
    batched_ids = {s.id for s in batched}
    sent = 0
    for s, urgent in to_send:
        if s.id in batched_ids:
            continue
        log.info("NEW%s: %s | %s | %s -> %s", " [keyword]" if urgent else "", s.title, s.when, s.venue, s.url)
        if args.dry_run or notify.send_all(notifiers, s, urgent=urgent):
            state.mark(s.id, s.title)  # on failure leave it unseen so the next run retries
            sent += 1
    if batched:
        summary = summarize(batched)
        log.info("NEW (batched): %s", summary.title)
        if args.dry_run or notify.send_all(notifiers, summary):
            for s in batched:
                state.mark(s.id, s.title)
            sent += 1
    if not args.dry_run:
        state.save()
    log.info("sent %d notification(s)", sent)
    return 0


def summarize(screenings: list[Screening]) -> Screening:
    lines = [f"{s.title} ({', '.join(filter(None, (s.when, s.venue)))})" for s in screenings[:15]]
    if len(screenings) > 15:
        lines.append(f"...and {len(screenings) - 15} more")
    return Screening(id="summary", title=f"{len(screenings)} new screenings", url=feed.SITE, details="\n".join(lines))


def watch(args) -> int:
    while True:
        try:
            check(args)
        except Exception:  # keep the loop alive through network blips
            log.exception("check failed")
        # Jitter so we don't hit the site on an exact schedule.
        time.sleep(args.interval + random.uniform(0, args.interval * 0.1))


def inspect(args) -> int:
    url = args.url or (os.environ.get("TICKETLORD_URL") or "https://revivalhub.com/")
    screenings = get_screenings(url, debug_dir="debug")
    print(f"Found {len(screenings)} upcoming screenings on {url}\n", file=sys.stderr)
    if args.json:
        print(json.dumps([s.to_dict() for s in screenings], indent=2))
    else:
        for s in screenings:
            print(f"• {s.title}  [{s.source}]")
            for label, val in (("when", s.when), ("venue", s.venue), ("info", s.details), ("tickets", s.url)):
                if val:
                    print(f"    {label}: {val}")
    return 0 if screenings else 1


def test_notify(args) -> int:
    notifiers = notify.from_env()
    if not notifiers:
        log.error("No notification channel configured.")
        return 2
    s = Screening(title="ticketlord test", url=(os.environ.get("TICKETLORD_URL") or "https://revivalhub.com/"),
                  when="Tonight 7:30pm", venue="Your phone")
    return 0 if notify.send_all(notifiers, s, urgent=args.urgent) else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="ticketlord", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, fn in (("check", check), ("watch", watch)):
        sp = sub.add_parser(name)
        sp.add_argument("--dry-run", action="store_true", help="log what would be sent; don't notify or save state")
        sp.add_argument("--notify-existing", action="store_true", help="on the first run, notify about everything already listed")
        if name == "watch":
            sp.add_argument("-i", "--interval", type=int, default=600, help="seconds between checks (default 600)")
        sp.set_defaults(fn=fn)

    sp = sub.add_parser("inspect")
    sp.add_argument("url", nargs="?")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=inspect)

    sp = sub.add_parser("test-notify")
    sp.add_argument("--urgent", action="store_true")
    sp.set_defaults(fn=test_notify)

    args = p.parse_args(argv)
    return args.fn(args)
