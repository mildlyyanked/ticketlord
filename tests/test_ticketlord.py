import json
from pathlib import Path

import pytest

import copy

from ticketlord import cli, feed, notify
from ticketlord.scraper import Screening, parse

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
    x.update({"id": rec_id, "films": [{"name": name, "year": 1979, "directors": "Andrei Tarkovsky"}],
              "ticket_urls": [f"https://tickets.example.com/{rec_id}"], **extra})
    data["screenings"].append(x)


@pytest.fixture
def run(tmp_path, monkeypatch):
    sent = []
    data = copy.deepcopy(FEED)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TICKETLORD_STATE_FILE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("NTFY_TOPIC", "test")
    monkeypatch.setenv("TICKETLORD_SOURCES", "revivalhub")
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


def test_only_la_region_by_default(run, monkeypatch):
    frida = next(v for v in FEED["venues"] if v["name"] == "Frida Cinema")
    data = copy.deepcopy(FEED)
    add_screening(data, "recOC1", "Stalker", venue=frida["id"])
    la = {s.title for s in feed.to_screenings(data, include_past=True, only_regions={"los angeles"})}
    assert "Stalker" not in la and "Portrait of a Lady on Fire" in la
    oc = {s.title for s in feed.to_screenings(data, include_past=True, only_regions={"orange county"})}
    assert oc == {"Stalker"}


def test_region_falls_back_to_nearest_center():
    regions = feed.Regions(FEED)
    assert regions.of({"lat": 33.75, "lng": -117.87}) == "Orange County"  # Santa Ana, no region set
    assert regions.of({"lat": 34.05, "lng": -118.25}) == "Los Angeles"
    assert regions.of({}) is None  # Wide Release: no location, kept


def test_regions_env(monkeypatch):
    monkeypatch.setenv("TICKETLORD_SOURCES", "revivalhub")
    calls = []
    monkeypatch.setattr(feed, "fetch_screenings", lambda **k: calls.append(k["only_regions"]) or [])
    cli.get_screenings("https://revivalhub.com/")
    monkeypatch.setenv("TICKETLORD_REGIONS", "Los Angeles, Orange County")
    cli.get_screenings("https://revivalhub.com/")
    monkeypatch.setenv("TICKETLORD_REGIONS", "all")
    cli.get_screenings("https://revivalhub.com/")
    assert calls == [{"los angeles"}, {"los angeles", "orange county"}, None]


def test_wide_releases_need_a_q_and_a(run, monkeypatch):
    check, sent, data = run
    wide = next(v for v in FEED["venues"] if v.get("wide_release"))
    check()
    add_screening(data, "recWR1", "Spirited Away", venue=wide["id"], categories=["Anniversary"])
    add_screening(data, "recWR2", "Pan's Labyrinth", venue=wide["id"], categories=["Q&A / Discussion"])
    add_screening(data, "recLOCAL", "Stalker", categories=["Anniversary"])
    data["organizations"].append({"id": "recFATHOM", "name": "Fathom Events"})
    add_screening(data, "recFATHOM1", "It's a Wonderful Life", organizations=["recFATHOM"])
    check()
    assert sorted(t for t, _ in sent) == ["Pan's Labyrinth", "Stalker"]


def test_wide_release_setting(monkeypatch):
    wide = Screening(title="Spirited Away", url="x", wide_release=True)
    local = Screening(title="Stalker", url="y")
    assert cli.filter_wide_releases([wide, local]) == [local]
    monkeypatch.setenv("TICKETLORD_WIDE_RELEASE", "all")
    assert cli.filter_wide_releases([wide, local]) == [wide, local]
    monkeypatch.setenv("TICKETLORD_WIDE_RELEASE", "ghibli,spirited")
    assert cli.filter_wide_releases([wide, local]) == [wide, local]
    monkeypatch.setenv("TICKETLORD_WIDE_RELEASE", "none")
    assert cli.filter_wide_releases([wide, local]) == [local]


def test_new_source_is_seeded_silently_and_dupes_across_sources_alert_once(run, monkeypatch):
    check, sent, data = run
    check()
    # A second source shows up with two listings: recorded silently on its first run.
    extra = []
    monkeypatch.setattr(feed, "fetch_screenings", lambda **k: feed.to_screenings(data, include_past=True) + extra)
    extra.append(Screening(id="cinematheque:1", title="Old", url="https://a.test/old", ticket_url="https://a.test/old", date="2026-11-01"))
    check()
    assert sent == []
    # Later the theater's site lists something new: one alert...
    extra.append(Screening(id="cinematheque:2", title="Stalker", url="https://www.a.test/stalker/", ticket_url="https://www.a.test/stalker/", date="2026-11-02"))
    check()
    assert sent == [("Stalker", False)]
    # ...and none when Revival Hub picks up the same screening days later. (Its copied start time is
    # Nov 3 05:55 UTC, i.e. Nov 2 in LA; Revival Hub's own "date" field says Nov 3.)
    add_screening(data, "recLATER", "Stalker", ticket_urls=["https://a.test/stalker?utm_source=revivalhub"])
    check()
    assert sent == [("Stalker", False)]


# ---------------------------------------------------------------- theaters' own sites

from datetime import datetime, timezone

from ticketlord import theaters

AC = json.loads((FIXTURES / "cinematheque.json").read_text())
ACADEMY = json.loads((FIXTURES / "academy.json").read_text())


@pytest.fixture
def sites(monkeypatch):
    monkeypatch.setattr(theaters, "_now", lambda: datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc))

    def fake_get(url, params=None, timeout=60):
        if url.endswith("/event/taxonomies"):
            return {"taxonomies": AC["taxonomies"]}
        if url.endswith("/algolia_get_events"):
            assert params["environment"] == "production_2026"
            return {"hits": AC["hits"]}
        if url == theaters.ACADEMY_API:
            return ACADEMY
        raise AssertionError(url)

    monkeypatch.setattr(theaters, "_get_json", fake_get)


def test_cinematheque_records(sites):
    by = {s.title: s for s in theaters.cinematheque_screenings()}
    s = by["Mulholland Drive"]
    assert s.id == "cinematheque:5570875"
    assert s.url == "https://www.americancinematheque.com/now-showing/mulholland-drive-10-16-2026/"
    assert s.when == "Fri Oct 16, 1:00pm" and s.date == "2026-10-16"
    assert s.venue == "Los Feliz Theatre"
    assert "Pan's Labyrinth" not in by  # Oct 2: already past
    assert "Premiere" in by["Kwaidan"].details  # event types become searchable details
    assert theaters._title_case("PAN'S LABYRINTH") == "Pan's Labyrinth"
    assert theaters._title_case("THE GOOD, THE BAD AND THE UGLY") == "The Good, the Bad and the Ugly"
    assert theaters._title_case("NIGHT OF THE LIVING DEAD: A Q&A") == "Night of the Living Dead: A Q&A"  # event types become searchable details


def test_academy_records(sites):
    found = theaters.academy_screenings()
    names = {s.title for s in found}
    assert "Calm Morning: Stop-motion Stories of Family" not in names  # education programs skipped
    s = next(s for s in found if s.title.startswith("Arrival"))
    assert s.url.startswith("https://www.academymuseum.org/en/programs/detail/arrival-a-conversation-with-denis-villeneuve")
    assert s.venue.startswith("Academy Museum")
    assert s.date and s.when


def test_all_sources_combined_and_failures_reported(sites, run, monkeypatch):
    check, sent, data = run
    monkeypatch.setenv("TICKETLORD_SOURCES", "revivalhub,cinematheque,academy")
    calls = []
    monkeypatch.setitem(theaters.SOURCES, "cinematheque", lambda: calls.append("ac") or theaters.cinematheque_screenings())

    def broken():
        raise ValueError("site changed")

    monkeypatch.setitem(theaters.SOURCES, "academy", broken)
    assert check() == 1  # academy broke: run marked failed...
    state = json.loads(Path("seen.json").read_text())
    assert set(state["sources"]) == {"revivalhub", "cinematheque"}  # ...but the others were recorded
    assert any(k.startswith("cinematheque:") for k in state["seen"])
    assert calls == ["ac"] and sent == []
