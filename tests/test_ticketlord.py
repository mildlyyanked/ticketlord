import json
from pathlib import Path

import pytest

from ticketlord import cli, notify
from ticketlord.scraper import parse

FIXTURE = (Path(__file__).parent / "fixtures" / "listings.html").read_text()
BASE = "https://revivalhub.com/"


def by_title():
    return {s.title: s for s in parse(FIXTURE, BASE)}


def test_finds_all_screenings_once():
    assert sorted(by_title()) == ["2001: A Space Odyssey", "Cure", "Mulholland Drive (35mm)", "The Brood"]


def test_json_ld_event():
    s = by_title()["Mulholland Drive (35mm)"]
    assert s.source == "json-ld"
    assert s.venue == "New Beverly Cinema"
    assert s.url.startswith("https://tickets.example-theater.com/")
    assert s.event_url == "https://revivalhub.com/event/mulholland-drive"


def test_ticket_link_card():
    s = by_title()["2001: A Space Odyssey"]
    assert s.ticket_url == "https://www.eventbrite.com/e/2001-70mm-123"
    assert s.venue == "Aero Theatre"
    assert s.when == "2026-10-21T19:30"


def test_sold_out_and_event_page_not_duplicated():
    s = by_title()["Cure"]
    assert s.sold_out
    assert s.ticket_url == "https://vidiots.veezi.com/purchase/999"
    assert s.event_url == "https://revivalhub.com/event/cure"


def test_event_link_without_tickets():
    s = by_title()["The Brood"]
    assert s.url == "https://revivalhub.com/screening/the-brood"
    assert s.venue == "Brain Dead Studios"


def test_ids_ignore_tracking_params():
    a = parse(FIXTURE, BASE)
    b = parse(FIXTURE.replace("utm_source=hub", "utm_source=other"), BASE)
    assert {s.id for s in a} == {s.id for s in b}


@pytest.fixture
def run(tmp_path, monkeypatch):
    sent = []
    page = {"html": FIXTURE}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TICKETLORD_STATE_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("NTFY_TOPIC", "test")
    monkeypatch.setattr(cli, "fetch", lambda url, render_js=False: page["html"])
    monkeypatch.setattr(notify.Ntfy, "send", lambda self, s, urgent=False: sent.append((s.title, urgent)))
    return lambda *a: cli.main(["check", *a]), sent, page


def test_first_run_seeds_then_notifies_only_new(run):
    check, sent, page = run
    assert check() == 0
    assert sent == []  # seeded silently
    page["html"] = FIXTURE.replace("</section>", """
      <div class="screening"><h3>Stalker</h3>
      <a href="https://tickets.example-theater.com/stalker">Get Tickets</a></div></section>""")
    assert check() == 0
    assert sent == [("Stalker", False)]
    assert check() == 0
    assert len(sent) == 1


def test_keywords_mark_urgent_and_filter(run, monkeypatch):
    check, sent, _ = run
    monkeypatch.setenv("TICKETLORD_KEYWORDS", "kubrick, aero")
    monkeypatch.setenv("TICKETLORD_ONLY_KEYWORDS", "1")
    assert check("--notify-existing") == 0
    assert sent == [("2001: A Space Odyssey", True)]


def test_failed_delivery_is_retried(run, monkeypatch):
    check, sent, _ = run
    real_send_all = notify.send_all
    monkeypatch.setattr(notify, "send_all", lambda *a, **k: False)
    check("--notify-existing")
    assert json.loads(Path("seen.json").read_text())["seen"] == {}
    monkeypatch.setattr(notify, "send_all", real_send_all)
    check()
    assert len(sent) == 4


def test_zero_results_is_an_error(run):
    check, sent, page = run
    page["html"] = "<html><body>maintenance</body></html>"
    assert check() == 1
