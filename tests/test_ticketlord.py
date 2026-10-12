import json
from pathlib import Path

import pytest

import copy

from ticketlord import cli, feed, notify
from ticketlord.scraper import parse

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = (FIXTURES / "listings.html").read_text()
FEED = json.loads((FIXTURES / "revivalhub-feed.json").read_text())
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


# ---------------------------------------------------------------- revivalhub feed


def feed_by_title():
    return {s.title: s for s in feed.to_screenings(FEED, include_past=True)}


def test_feed_maps_real_records():
    s = feed_by_title()["Portrait of a Lady on Fire"]
    assert s.id == "revivalhub:recPAnyOeKGFsNVQL"
    assert s.ticket_url == "https://www.egyptiantheatre.com/film/egyptian-portrait-of-a-lady-on-fire"
    assert s.event_url == "https://revivalhub.com/film/portrait-of-a-lady-on-fire-2019"
    assert s.venue  # resolved from the venue id
    assert s.when == "Mon Nov 2, 9:55pm"  # 05:55 UTC -> PST (DST ended Nov 1)
    assert "4K DCP" in s.details and "Celine Sciamma" in s.details


def test_feed_double_feature_and_presenter():
    titles = feed_by_title()
    assert "Dark Star + Dark Star" in titles
    assert any("presented by" in (s.details or "") for s in titles.values())


def test_feed_skips_past_screenings():
    assert feed.to_screenings(FEED) == [] or all(s.when for s in feed.to_screenings(FEED))
    old = copy.deepcopy(FEED)
    for x in old["screenings"]:
        x["utc_start"] = 1000
    assert feed.to_screenings(old) == []


def test_film_slug_matches_site_urls():
    assert feed.film_slug("Pan's Labyrinth", 2006) == "pan-s-labyrinth-2006"
    assert feed.film_slug("2001: A Space Odyssey", 1968) == "2001-a-space-odyssey-1968"


# ---------------------------------------------------------------- check command


def add_screening(data, rec_id, name, **extra):
    x = copy.deepcopy(data["screenings"][1])
    x.update({"id": rec_id, "films": [{"name": name, "year": 1979, "directors": "Andrei Tarkovsky"}], **extra})
    data["screenings"].append(x)


@pytest.fixture
def run(tmp_path, monkeypatch):
    sent = []
    data = copy.deepcopy(FEED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TICKETLORD_STATE_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("NTFY_TOPIC", "test")
    monkeypatch.setattr(feed, "fetch_screenings", lambda **k: feed.to_screenings(data, include_past=True))
    monkeypatch.setattr(notify.Ntfy, "send", lambda self, s, urgent=False: sent.append((s.title, urgent)))
    return lambda *a: cli.main(["check", *a]), sent, data


def test_first_run_seeds_then_notifies_only_new(run):
    check, sent, data = run
    assert check() == 0
    assert sent == []  # seeded silently
    add_screening(data, "recNEW1", "Stalker")
    assert check() == 0
    assert sent == [("Stalker", False)]
    assert check() == 0
    assert len(sent) == 1


def test_keywords_mark_urgent_and_filter(run, monkeypatch):
    check, sent, _ = run
    monkeypatch.setenv("TICKETLORD_KEYWORDS", "70mm, sciamma")
    monkeypatch.setenv("TICKETLORD_ONLY_KEYWORDS", "1")
    assert check("--notify-existing") == 0
    assert sorted(sent) == [("Aliens", True), ("Portrait of a Lady on Fire", True)]


def test_big_drop_is_batched_but_keywords_still_alert(run, monkeypatch):
    check, sent, data = run
    monkeypatch.setenv("TICKETLORD_KEYWORDS", "tarkovsky")
    check()
    for i in range(7):
        add_screening(data, f"recBULK{i}", f"Film {i}", films=[{"name": f"Film {i}", "year": 2000}])
    add_screening(data, "recNEW1", "Stalker")
    check()
    assert sent == [("Stalker", True), ("7 new screenings", False)]
    check()
    assert len(sent) == 2


def test_failed_delivery_is_retried(run, monkeypatch):
    check, sent, _ = run
    real_send_all = notify.send_all
    monkeypatch.setattr(notify, "send_all", lambda *a, **k: False)
    monkeypatch.setenv("TICKETLORD_BATCH_OVER", "100")
    check("--notify-existing")
    assert json.loads(Path("seen.json").read_text())["seen"] == {}
    monkeypatch.setattr(notify, "send_all", real_send_all)
    check()
    assert len(sent) == len(FEED["screenings"])


def test_zero_results_is_an_error(run):
    check, sent, data = run
    data["screenings"].clear()
    assert check() == 1


def test_other_sites_use_html_scraper(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "fetch", lambda url, render_js=False: FIXTURE)
    assert cli.main(["inspect", "https://example-cinema.com/"]) == 0
    assert "2001: A Space Odyssey" in capsys.readouterr().out


def test_unchanged_feed_leaves_state_file_untouched(run):
    check, _, _ = run
    check()
    before = Path("seen.json").read_text()
    check()
    assert Path("seen.json").read_text() == before


def test_keywords_file_adds_to_env(run, monkeypatch):
    check, sent, _ = run
    Path("keywords.txt").write_text("# my list\nSciamma\n\n70mm  # big film\n")
    monkeypatch.setenv("TICKETLORD_KEYWORDS", "q&a")
    assert cli.keywords() == ["70mm", "q&a", "sciamma"]
