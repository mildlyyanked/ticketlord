# ticketlord

Watches [Revival Hub](https://revivalhub.com/) (LA repertory, indie and microcinema listings) and pushes
a notification to your phone whenever a new screening appears. Tapping the notification opens
the ticket page.

- **Notifications:** [ntfy](https://ntfy.sh) (free, no account needed), Pushover or Telegram. You can use more than one.
- **Keywords:** screenings that match your watchlist arrive as urgent alerts, which can break through Do Not Disturb.
  Keywords match the title, director, venue, format (`35mm`, `70mm`, `IB Technicolor`), category (`Q&A`, `Premiere`,
  `Double Feature`, `Midnight`) and notes, so `kubrick, 70mm, q&a` works. You can also mute everything that doesn't match.
- **Batching:** when a venue posts its whole month at once, the non-keyword screenings arrive as one summary push, not 40.
- **No flood on day one:** the first run quietly records what's already listed. After that you only hear about new screenings.
- **Fails loudly:** if a check finds 0 screenings (site redesign, blocked request), it exits with an error
  instead of reporting nothing new.

## How it finds screenings

revivalhub.com is a JavaScript app, so its screenings aren't in the page HTML. The page loads them from a public
JSON export of the site's database (`data-import.json` on Google Cloud Storage), and ticketlord reads that same feed.
Each screening comes with a stable ID, its films (with year and director), venue, start time, formats, categories,
presenting organization and ticket URL. If the feed ever moves, ticketlord finds its new location on the homepage.
Set `TICKETLORD_INCLUDE_AMC=1` to also include the AMC showtimes the site merges in.

For any other site (`TICKETLORD_URL=...`), a generic HTML scraper reads schema.org Event data, ticket links
(Eventbrite, Veezi, Eventive, DICE, Tixr, ...) and links to event pages.

## Setup (5 minutes)

### 1. Set up your phone
Install **ntfy** ([iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
and subscribe to a topic with a hard-to-guess name, e.g. `ticketlord-7f3k2q9x`. Anyone who knows the name can read the topic.

### 2a. Run it for free on GitHub Actions (recommended)
This repo includes `.github/workflows/check.yml`. It runs every 10 minutes and commits `state/seen.json` back to the repo.

1. Repo **Settings → Secrets and variables → Actions → Secrets**: add `NTFY_TOPIC` (and/or the Pushover/Telegram secrets).
2. Optionally, under **Variables**, add `TICKETLORD_KEYWORDS` (comma-separated) and `TICKETLORD_ONLY_KEYWORDS=1`.
3. **Actions → Check screenings → Run workflow** once to seed. Scheduled runs take over from there.

> GitHub only runs scheduled workflows on the default branch, and it may delay them by a few minutes at busy times.
> If you need faster alerts, use 2b.

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
