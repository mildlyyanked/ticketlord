"""Remembers which screenings have already been announced."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Forget screenings not seen on the page for this long so the file doesn't grow forever.
EXPIRE_SECONDS = 60 * 60 * 24 * 90


class State:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.seen: dict[str, dict] = {}
        self.exists = self.path.exists()
        if self.exists:
            self.seen = json.loads(self.path.read_text() or "{}").get("seen", {})

    def is_new(self, screening_id: str) -> bool:
        return screening_id not in self.seen

    def mark(self, screening_id: str, title: str) -> None:
        # Day resolution: the file (and the bot's commit) only changes when something new shows up,
        # or once a day, instead of on every 10-minute run.
        now = int(time.time()) // 86400 * 86400
        entry = self.seen.setdefault(screening_id, {"title": title, "first_seen": now})
        entry["last_seen"] = now

    def save(self) -> None:
        cutoff = time.time() - EXPIRE_SECONDS
        self.seen = {k: v for k, v in self.seen.items() if v.get("last_seen", 0) >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": self.seen}, indent=1, sort_keys=True))
        tmp.replace(self.path)
