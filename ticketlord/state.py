"""Remembers which screenings have already been announced."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .scraper import normalize_url

# Forget screenings not seen on the page for this long so the file doesn't grow forever.
EXPIRE_SECONDS = 60 * 60 * 24 * 90


class State:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.seen: dict[str, dict] = {}
        self.known_sources: set[str] = set()
        self.stats: dict = {}  # counters for the daily check-in, since the last one was sent
        self.exists = self.path.exists()
        if self.exists:
            data = json.loads(self.path.read_text() or "{}")
            self.seen = data.get("seen", {})
            self.stats = data.get("stats", {})
            # Older state files have no "sources" list; work it out from the ids.
            self.known_sources = set(data.get("sources") or
                                     (k.split(":", 1)[0] for k in self.seen if not k.startswith("url:")))

    @staticmethod
    def keys(s) -> list[str]:
        """A screening's own id, plus "ticket link + date" so the same screening reported by two
        sources (a theater's site, then Revival Hub days later) only alerts once."""
        keys = [s.id]
        if s.ticket_url and s.date:
            keys.append(f"url:{normalize_url(s.ticket_url)}|{s.date}")
        return keys

    def is_new(self, s) -> bool:
        return not any(k in self.seen for k in self.keys(s))

    def mark(self, s) -> None:
        # Day resolution: the file (and the bot's commit) only changes when something new shows up,
        # or once a day, instead of on every run.
        now = int(time.time()) // 86400 * 86400
        for key in self.keys(s):
            entry = self.seen.setdefault(key, {"title": s.title, "first_seen": now})
            entry["last_seen"] = now


    def save(self) -> None:
        cutoff = time.time() - EXPIRE_SECONDS
        self.seen = {k: v for k, v in self.seen.items() if v.get("last_seen", 0) >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sources": sorted(self.known_sources), "stats": self.stats, "seen": self.seen}, indent=1, sort_keys=True))
        tmp.replace(self.path)
