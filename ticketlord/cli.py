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
from datetime import datetime
from pathlib import Path

from urllib.parse import urlparse

from . import feed, notify, theaters
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


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


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


GUEST_WORDS = "q&a,intro with,introduced by,in person,in-person,special guest,conversation with,live appearance"


def filter_wide_releases(screenings: list[Screening]) -> list[Screening]:
    """Nationwide re-releases (Fathom, Ghibli Fest...) and AMC chain showtimes only count if they
    match these words, i.e. there's a Q&A or a guest.

    TICKETLORD_WIDE_RELEASE: comma-separated words, "all" to keep every one, "none" to drop them all.
    """
    setting = (os.environ.get("TICKETLORD_WIDE_RELEASE") or GUEST_WORDS).strip().lower()
    if setting == "all":
        return screenings
    words = [] if setting == "none" else [w.strip() for w in setting.split(",") if w.strip()]
    return [s for s in screenings if not s.wide_release or (words and matches_keywords(s, words))]


SOURCES = ("revivalhub", "amc", "cinematheque", "academy")
FAILED_SOURCES: list[str] = []


def get_screenings(url: str, debug_dir: str | None = None) -> list[Screening]:
    if urlparse(url).netloc.removeprefix("www.") == "revivalhub.com":
        # TICKETLORD_SOURCES: comma-separated subset of SOURCES (default: all of them).
        wanted_sources = [x.strip().lower() for x in (os.environ.get("TICKETLORD_SOURCES") or ",".join(SOURCES)).split(",")]
        # Revival Hub regions to keep; "all" for every region.
        wanted = (os.environ.get("TICKETLORD_REGIONS") or "Los Angeles").strip().lower()
        only = None if wanted == "all" else {r.strip() for r in wanted.split(",") if r.strip()}
        screenings: list[Screening] = []
        FAILED_SOURCES.clear()
        if "revivalhub" in wanted_sources:
            screenings += feed.fetch_screenings(include_amc="amc" in wanted_sources, only_regions=only)
        for name, fetcher in theaters.SOURCES.items():
            if name not in wanted_sources:
                continue
            try:
                found = fetcher()
            except Exception as e:  # one broken site shouldn't stop the others
                log.error("%s failed: %s", name, e)
                FAILED_SOURCES.append(name)
                continue
            if not found:
                log.error("%s returned 0 screenings", name)
                FAILED_SOURCES.append(name)
            screenings += found
        return filter_wide_releases(screenings)
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
        record_check(state, [], 0, 0, ["everything"])
        if not args.dry_run:
            maybe_send_digest(state, notifiers)
            state.save()
        return 1

    words = keywords()
    only_kw = env_flag("TICKETLORD_ONLY_KEYWORDS")
    # First run of a source (or of the whole app): remember everything it currently lists
    # instead of sending a flood.
    new_sources = {source_of(s) for s in screenings} - state.known_sources
    if new_sources and not args.notify_existing:
        log.info("first run for %s: recording current listings without notifying", ", ".join(sorted(new_sources)))

    def seeding(s: Screening) -> bool:
        return not args.notify_existing and source_of(s) in new_sources

    new = [s for s in screenings if state.is_new(s)]
    log.info("%d screenings listed, %d new", len(screenings), len(new))

    for s in screenings:
        if not state.is_new(s):
            state.mark(s)  # refresh last_seen

    to_send: list[tuple[Screening, bool]] = []
    for s in new:
        urgent = matches_keywords(s, words)
        if not seeding(s) and (urgent or not only_kw):
            to_send.append((s, urgent))
        else:
            state.mark(s)

    # Several showtimes of one film at one venue (a run, or an AMC Q&A on three nights) are one push.
    groups: dict[tuple, list[tuple[Screening, bool]]] = {}
    for s, urgent in to_send:
        groups.setdefault((source_of(s), s.title.lower(), s.venue), []).append((s, urgent))
    alerts = [([s for s, _ in g], any(u for _, u in g)) for g in groups.values()]

    # One push per film, unless a venue just dropped a whole calendar: then the non-keyword
    # ones are rolled into a single summary.
    batch_over = int(os.environ.get("TICKETLORD_BATCH_OVER") or 5)
    normal = [members for members, urgent in alerts if not urgent]
    batched = normal if len(normal) > batch_over else []
    sent = 0
    for members, urgent in alerts:
        if batched and members in batched:
            continue
        alert = combine(members)
        log.info("NEW%s: %s | %s | %s -> %s", " [keyword]" if urgent else "", alert.title, alert.when, alert.venue, alert.url)
        if args.dry_run or notify.send_all(notifiers, alert, urgent=urgent):
            for s in members:
                state.mark(s)  # on failure leave them unseen so the next run retries
            sent += 1
    if batched:
        summary = summarize([combine(m) for m in batched])
        log.info("NEW (batched): %s", summary.title)
        if args.dry_run or notify.send_all(notifiers, summary):
            for members in batched:
                for s in members:
                    state.mark(s)
            sent += 1
    state.known_sources |= new_sources
    record_check(state, screenings, sum(not seeding(s) for s in new), sent, FAILED_SOURCES)
    if not args.dry_run:
        maybe_send_digest(state, notifiers)
        state.save()
    log.info("sent %d notification(s)", sent)
    if FAILED_SOURCES:
        log.error("could not check: %s (see errors above)", ", ".join(FAILED_SOURCES))
        return 1
    return 0


SOURCE_NAMES = {"revivalhub": "Revival Hub", "cinematheque": "American Cinematheque",
                "academy": "Academy Museum", "amc": "AMC"}


def record_check(state: State, screenings: list[Screening], new: int, sent: int, failed: list[str]) -> None:
    """Counters for the daily check-in, kept in the state file between runs."""
    st = state.stats
    st["checks"] = st.get("checks", 0) + 1
    st["new"] = st.get("new", 0) + new
    st["alerts"] = st.get("alerts", 0) + sent
    for name in failed:
        st.setdefault("failures", {})[name] = st.get("failures", {}).get(name, 0) + 1
    st["last_check"] = int(time.time())
    if screenings:
        counts: dict[str, int] = {}
        for s in screenings:
            counts[source_of(s)] = counts.get(source_of(s), 0) + 1
        st["tracked"] = counts


def digest_message(st: dict) -> Screening:
    checks, new, alerts = st.get("checks", 0), st.get("new", 0), st.get("alerts", 0)
    last = datetime.fromtimestamp(st.get("last_check", time.time()), feed.TZ)
    lines = [
        f"{checks} check{'s' * (checks != 1)} since the last check-in, latest at {last.strftime('%-I:%M%p').lower()}",
        f"{new} new screening{'s' * (new != 1)} · {alerts} alert{'s' * (alerts != 1)} sent",
    ]
    tracked = st.get("tracked") or {}
    if tracked:
        lines.append(f"Watching {sum(tracked.values()):,} upcoming: " + " · ".join(
            f"{SOURCE_NAMES.get(k, k)} {v:,}" for k, v in sorted(tracked.items(), key=lambda kv: -kv[1])))
    for name, n in sorted((st.get("failures") or {}).items()):
        lines.append(f"⚠️ {SOURCE_NAMES.get(name, name)} failed {n} of {checks} checks")
    title = "ticketlord · " + ("needs a look" if st.get("failures") else "all quiet" if not new else "daily check-in")
    return Screening(id="digest", title=title, url=feed.SITE, details="\n".join(lines))


def _now() -> datetime:
    return datetime.now(feed.TZ)


def maybe_send_digest(state: State, notifiers: list[notify.Notifier]) -> None:
    """Once a day, with the first check after TICKETLORD_DIGEST_HOUR (Pacific, default 19 = 7pm)."""
    setting = (os.environ.get("TICKETLORD_DIGEST_HOUR") or "19").strip().lower()
    if setting in ("off", "none", "no") or not notifiers:
        return
    now = _now()
    today = now.strftime("%Y-%m-%d")
    if state.stats.get("last_digest") == today or now.hour < int(setting):
        return
    message = digest_message(state.stats)
    log.info("daily check-in: %s", message.details.replace("\n", " | "))
    if notify.send_all(notifiers, message, quiet=True):
        state.stats = {"last_digest": today, "tracked": state.stats.get("tracked", {})}


def source_of(s: Screening) -> str:
    return s.id.split(":", 1)[0]


def combine(members: list[Screening]) -> Screening:
    """One alert for several showtimes of the same film at the same venue."""
    first = members[0]
    if len(members) == 1:
        return first
    times = ", ".join(s.when for s in members if s.when)
    details = f"{len(members)} showtimes: {times}" + (f"\n{first.details}" if first.details else "")
    return Screening(id=first.id, title=first.title, url=first.url, ticket_url=first.ticket_url,
                     event_url=first.event_url, when=f"{first.when} (+{len(members) - 1} more)" if first.when else None,
                     venue=first.venue, details=details, sold_out=all(s.sold_out for s in members),
                     source=first.source)


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
    counts = {}
    for s in screenings:
        counts[s.source] = counts.get(s.source, 0) + 1
    print(f"Found {len(screenings)} upcoming screenings: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
          + (f" (failed: {', '.join(FAILED_SOURCES)})" if FAILED_SOURCES else "") + "\n", file=sys.stderr)
    if args.json:
        print(json.dumps([s.to_dict() for s in screenings], indent=2))
    else:
        for s in screenings:
            print(f"• {s.title}  [{s.source}]{'  (sold out)' if s.sold_out else ''}")
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
    ok = notify.send_all(notifiers, s, urgent=args.urgent)
    for n in notifiers:
        if isinstance(n, notify.Ntfy):
            diagnose_ntfy(n)
    return 0 if ok else 1


def diagnose_ntfy(n: notify.Ntfy) -> None:
    """Show what the ntfy server holds for the topic, to tell 'not sent' apart from 'phone not subscribed'."""
    t = n.topic
    print(f"ntfy server: {n.server}")
    print(f"topic: {len(t)} characters, starts with {t[:2]!r}, "
          f"{'has' if t != t.lower() else 'no'} capital letters, "
          f"{'HAS' if any(c.isspace() for c in os.environ.get('NTFY_TOPIC', '')) else 'no'} spaces/newlines "
          f"(topics are case-sensitive: the app must match exactly)")
    try:
        messages = n.recent()
    except Exception as e:
        print(f"could not read the topic back from ntfy: {e}")
        return
    print(f"ntfy is holding {len(messages)} message(s) for this topic from the last 24h:")
    for m in messages:
        when = datetime.fromtimestamp(m.get("time", 0), feed.TZ).strftime("%a %-I:%M%p")
        print(f"  - {when}: {m.get('title') or m.get('message', '')[:60]}")
    print("If messages are listed here but not on your phone, the app is subscribed to a different topic "
          "or server: open https://ntfy.sh/<your topic> in your phone's browser to see them directly.")


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
