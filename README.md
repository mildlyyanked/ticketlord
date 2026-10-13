# ticketlord

Watches [Revival Hub](https://revivalhub.com/) (LA repertory, indie and microcinema listings) and pushes
a notification to your phone whenever a new screening appears. Tapping the notification opens
the ticket page.

- **Notifications:** [ntfy](https://ntfy.sh) (free, no account needed), Pushover or Telegram. You can use more than one.
- **Keywords:** screenings that match your watchlist arrive as urgent alerts, which can break through Do Not Disturb.
  Keywords match the title, director, venue, format (`35mm`, `70mm`, `IB Technicolor`), category (`Q&A`, `Premiere`,
  `Double Feature`, `Midnight`) and notes, so `kubrick, 70mm, q&a` works. You can also mute everything that doesn't match.
- **Batching:** several showtimes of one film at one venue are one push; when a venue posts its whole month at once,
  the non-keyword ones arrive as one summary push, not 40.
- **No flood on day one:** the first run quietly records what's already listed. After that you only hear about new screenings.
- **Fails loudly:** if a check finds 0 screenings (site redesign, blocked request), it exits with an error
  instead of reporting nothing new.

## Where it looks

| Source | What | Why |
|---|---|---|
| **Revival Hub** | LA repertory, indie and microcinema listings | the broad net |
| **American Cinematheque** | Aero, Egyptian, Los Feliz, straight from its own calendar | catches announcements before Revival Hub lists them |
| **Academy Museum** | screenings and special events from its ticketing system | same |
| **AMC** | LA AMC showtimes (Revival Hub's AMC feed) | only screenings with a Q&A or guest alert |

All of these sites load their schedules as JSON, and ticketlord reads that same data: stable IDs, venue,
Pacific start time, formats, categories, directors and ticket link. The same screening coming from two
sources (a theater's site, then Revival Hub days later) alerts once: screenings are matched by ticket link and date.
A newly added source records its current listings silently on its first run, so turning one on doesn't flood you.
`TICKETLORD_SOURCES` picks which to use (default: all).

Only Revival Hub's **Los Angeles** region (LA County) is reported. Its feed also covers Orange County,
Ventura County and San Bernardino County; set `TICKETLORD_REGIONS` (e.g. `Los Angeles,Orange County`, or `all`).
Nationwide "Wide Release" events (Fathom anniversary re-releases, Ghibli Fest...) and AMC showtimes are skipped
unless they include a Q&A or guest; `TICKETLORD_WIDE_RELEASE` sets those words (`all` / `none` also work).

If a source breaks (site redesign), the others keep working and the run turns red so GitHub emails you.
**Actions → Check screenings → Run workflow → Only list what each source finds** shows what each one returns.

For any other site (`TICKETLORD_URL=...`), a generic HTML scraper reads schema.org Event data, ticket links
and links to event pages.

## Setup (5 minutes)

### 1. Set up your phone
Install **ntfy** ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
and subscribe to a topic with a hard-to-guess name, e.g. `ticketlord-7f3k2q9x`. Anyone who knows the name can read the topic.

### 2a. Run it for free on GitHub Actions (recommended)
This repo includes `.github/workflows/check.yml`. It runs twice an hour and commits `state/seen.json` back to the repo.

1. Repo **Settings → Secrets and variables → Actions → Secrets**: add `NTFY_TOPIC` (and/or the Pushover/Telegram secrets).
2. Optionally, list keywords one per line in `keywords.txt`, and/or under **Variables** add `TICKETLORD_KEYWORDS` (comma-separated). Both lists apply. Add `TICKETLORD_ONLY_KEYWORDS=1` to mute non-matches.
3. **Actions → Check screenings → Run workflow** once to seed. Tick **Also send a test push to my phone** to confirm notifications arrive. Scheduled runs take over from there.

> GitHub only runs scheduled workflows on the default branch, and it may delay or skip runs at busy times.
> Revival Hub publishes new listings in batches and its feed is cached for up to an hour, so faster checks gain little.

### 2b. Or run it on your own machine / a Raspberry Pi / a VPS
```bash
pip install .
cp .env.example .env        # fill in NTFY_TOPIC etc.
ticketlord test-notify      # your phone should buzz
ticketlord inspect          # see what the parser finds on the page
ticketlord watch -i 300     # check every ~5 minutes, forever
```
You can also run `ticketlord check` from cron.

## Commands
| command | what it does |
|---|---|
| `ticketlord check` | one pass: fetch, diff, notify, save state (`--dry-run`, `--notify-existing`) |
| `ticketlord watch -i SECONDS` | loop `check` forever, with jitter |
| `ticketlord inspect [URL] [--json]` | print upcoming screenings as parsed (no notifications, no state change) |
| `ticketlord test-notify [--urgent]` | send a test push |

## If it finds nothing
The check fails with "Parsed 0 screenings", and the GitHub Actions run turns red, which GitHub emails you about.
Run **Actions → Snapshot page (debugging) → Run workflow**. It saves the live page (raw and rendered), a screenshot and every
network request into `snapshots/`, which shows where the data comes from now.

## Auto-purchasing?
Not included. Revival Hub only aggregates listings: each "buy" link goes to a different theater's ticketing system
(Eventbrite, Veezi, Eventive, in-house sites...). Each one has its own checkout, seat picker, login and
anti-bot protection, and automating payment means storing your card details for the bot. The practical
speed-up is the one here: an urgent push for the films you care about that opens straight onto the purchase page.
For one theater you go to often, a Playwright checkout script for its specific ticketing vendor is doable as a follow-up.
