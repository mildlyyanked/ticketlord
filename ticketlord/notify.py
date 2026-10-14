"""Phone push notifications: ntfy, Pushover and Telegram."""

from __future__ import annotations

import json
import logging
import os

import requests

from .scraper import Screening

log = logging.getLogger(__name__)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def format_message(s: Screening, quiet: bool = False) -> tuple[str, str]:
    if quiet:  # status message: no film emoji, no ticket link
        return s.title, s.details or ""
    title = f"🎬 {s.title}"
    if s.sold_out:
        title += " (sold out)"
    lines = [" · ".join(x for x in (s.when, s.venue) if x)]
    if s.details:
        lines.append(s.details[:600])
    lines.append(s.url)
    lines = [x for x in lines if x]
    return title, "\n".join(lines)


class Notifier:
    name = "base"

    def send(self, s: Screening, urgent: bool = False, quiet: bool = False) -> None:
        """urgent: keyword match, break through Do Not Disturb. quiet: status message, no sound."""
        raise NotImplementedError


class Ntfy(Notifier):
    name = "ntfy"

    def __init__(self, topic: str, server: str, token: str = ""):
        self.topic, self.server, self.token = topic, server.rstrip("/"), token

    def send(self, s: Screening, urgent: bool = False, quiet: bool = False) -> None:
        title, body = format_message(s, quiet)
        payload = {
            "topic": self.topic,
            "title": title,
            "message": body,
            "click": s.url,
            "priority": 5 if urgent else 2 if quiet else 3,
            "tags": ["white_check_mark"] if quiet else ["ticket"] + (["rotating_light"] if urgent else []),
            "actions": [] if quiet else [{"action": "view", "label": "Buy tickets", "url": s.url, "clear": True}],
        }
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = requests.post(self.server, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        log.debug("ntfy accepted message %s", resp.json().get("id"))

    def recent(self, since: str = "24h") -> list[dict]:
        """Messages the ntfy server is holding for this topic (it keeps them ~12h or more)."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        resp = requests.get(f"{self.server}/{self.topic}/json", params={"poll": "1", "since": since},
                            headers=headers, timeout=15)
        resp.raise_for_status()
        events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
        return [e for e in events if e.get("event") == "message"]


class Pushover(Notifier):
    name = "pushover"

    def __init__(self, user: str, token: str):
        self.user, self.token = user, token

    def send(self, s: Screening, urgent: bool = False, quiet: bool = False) -> None:
        title, body = format_message(s, quiet)
        requests.post("https://api.pushover.net/1/messages.json", data={
            "token": self.token, "user": self.user, "title": title, "message": body,
            "url": s.url, "url_title": "Open" if quiet else "Buy tickets", "priority": 1 if urgent else -1 if quiet else 0,
        }, timeout=15).raise_for_status()


class Telegram(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token, self.chat_id = bot_token, chat_id

    def send(self, s: Screening, urgent: bool = False, quiet: bool = False) -> None:
        title, body = format_message(s, quiet)
        requests.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", json={
            "chat_id": self.chat_id,
            "text": f"{'🚨 ' if urgent else ''}{title}\n{body}",
            "disable_notification": quiet,
            **({} if quiet else {"reply_markup": {"inline_keyboard": [[{"text": "🎟 Buy tickets", "url": s.url}]]}}),
        }, timeout=15).raise_for_status()


def from_env() -> list[Notifier]:
    out: list[Notifier] = []
    if _env("NTFY_TOPIC"):
        out.append(Ntfy(_env("NTFY_TOPIC"), _env("NTFY_SERVER") or "https://ntfy.sh", _env("NTFY_TOKEN")))
    if _env("PUSHOVER_USER_KEY") and _env("PUSHOVER_APP_TOKEN"):
        out.append(Pushover(_env("PUSHOVER_USER_KEY"), _env("PUSHOVER_APP_TOKEN")))
    if _env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"):
        out.append(Telegram(_env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")))
    return out


def send_all(notifiers: list[Notifier], s: Screening, urgent: bool = False, quiet: bool = False) -> bool:
    """Returns True if at least one channel delivered."""
    ok = False
    for n in notifiers:
        try:
            n.send(s, urgent=urgent, quiet=quiet)
            log.info("%s: delivered %r", n.name, s.title)
            ok = True
        except requests.RequestException as e:
            log.error("%s notification failed for %r: %s", n.name, s.title, e)
    return ok
