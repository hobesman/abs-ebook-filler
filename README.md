# abs-ebook-filler

Finds audiobooks in your **Audiobookshelf** library that don't have an ebook yet, searches **Shelfmark** for a
matching EPUB, lets you pick the right release, and saves it **into the audiobook's own folder** so ABS attaches
it to the same item (the "Read" button appears next to "Play").

- Web UI (main interface) + a CLI for dry runs and scripting, sharing the same core.
- EPUB only. You choose every download; nothing is grabbed automatically.
- Never overwrites a file; writes atomically; copies the audio files' owner onto the new ebook.

## How it works

1. Pages through every book library via the ABS API and keeps items that have audio files but no
   `ebookFile` and no supplementary ebook.
2. Cleans the title of series prefixes (see below) and searches Shelfmark's enabled release sources with
   `/api/releases?provider=manual&title=…&author=…` (works in both direct and universal search mode).
   If that finds no EPUB, it looks the book up via `/api/metadata/search` and searches releases for the
   closest match.
3. You pick a release → it's queued in Shelfmark → polled via `/api/status` → fetched with
   `/api/localdownload` → written as `<Title> - <Author>.epub` into the item folder.
4. Asks ABS to rescan that item (`POST /api/items/{id}/scan`).

### Title cleaning

Applied in order to the ABS title; a rule is skipped if it would leave nothing title-like:

| Rule | Example |
|---|---|
| Number in square brackets → text after it | `Mistborn [2] The Well of Ascension` → `The Well of Ascension` |
| Number + ` - ` → text after it | `Stormlight Archive 1 - The Way of Kings` → `The Way of Kings` |
| Number + `-` (no space) → text after it | `Expanse 3-Abaddon's Gate` → `Abaddon's Gate` |
| Standalone two-digit number → text after it | `Discworld 01 The Colour of Magic` → `The Colour of Magic` |

`(Unabridged)` and similar tags are removed. `Catch-22`, `1984`, `11/22/63` and `Fahrenheit 451` are left alone.
Titles with a genuine two-digit number (e.g. *The 39 Steps*) will be over-trimmed; just edit the query in the UI.

## Setup (Docker, same host as ABS + Shelfmark)

1. **Shelfmark API key** – set `SHELFMARK_API_KEY=<long random secret>` on the Shelfmark container and restart it.
2. **ABS token** – ABS → Settings → Users → your admin user → copy the API token.
3. On the server, in an empty folder, grab the two example files:
   ```bash
   curl -o docker-compose.yml https://raw.githubusercontent.com/hobesman/abs-ebook-filler/main/docker-compose.example.yml
   curl -o .env https://raw.githubusercontent.com/hobesman/abs-ebook-filler/main/.env.example
   ```
   Fill in `.env` (tokens, `WEB_PASSWORD`, `PATH_MAP`) and fix the library volume in `docker-compose.yml`.
   The prebuilt image `ghcr.io/hobesman/abs-ebook-filler:latest` (amd64 + arm64) is pulled automatically.

   **Updating:** `docker compose pull && docker compose up -d`
4. **PATH_MAP** – ABS reports folders by *its* container path (e.g. `/audiobooks/Author/Book`). Mount the same
   host folder into this container (e.g. at `/library`) read-write and set `PATH_MAP=/audiobooks:/library`.
   To check ABS's path: open any book in ABS → the folder path shown in its details.
5. Confirm connectivity and Shelfmark's JSON shapes:
   ```bash
   docker compose run --rm abs-ebook-filler probe --out /data/probe.json
   ```
6. Preview without writing anything:
   ```bash
   docker compose run --rm abs-ebook-filler dry-run --limit 5
   ```
7. Start the web UI:
   ```bash
   docker compose up -d
   ```
   Open `http://<server>:8090`, log in with `WEB_USER` / `WEB_PASSWORD`, click **Rescan library**.

## Using the web UI

- **Books** – your tracked audiobooks; the status dropdown defaults to *Missing* (no ebook yet) and can show
  queued, done, skipped, failed or all. The top bar (with the filters and pre-search) stays pinned while you
  scroll. Click a title to open its panel: Shelfmark is searched
  automatically with the cleaned title + author. Edit the query and search again if needed, then **Download** a
  release, or **Skip** the book (skipped books stay hidden until you un-skip them).
- **Source toggles** – above the results, one button per source/indexer (e.g. *Direct Download*,
  *MyAnonamouse*) with its result count. Click to hide/show that source; the choice is saved on the server
  and applies to every book, rapid mode and the CLI. Hidden sources are filtered before the top-N cut, so the
  next-best results from enabled sources fill the list. Toggling doesn't re-run the search.
- **Pre-search** – "Pre-search the next [100] missing books" (above the list) searches books ahead of time,
  top of the list first, skipping ones that already have results. It runs one search at a time with a short
  pause (`PRESEARCH_DELAY`), pauses while you're searching, and waits out Anna's Archive rate-limit cooldowns
  and retries. Books with results ready get a ⚡ and open instantly; results are kept `SEARCH_CACHE_HOURS`
  (24 h). Every normal search is saved the same way. Start it, go do something else, then work through the
  list in rapid mode. Also available as `abs-ebook-filler presearch --count 100` (e.g. nightly from cron).
- **Rapid mode** (toggle on the Books page, remembered per browser) – after **Download** or **Skip** the
  panel jumps straight to the next missing/failed book in the list and searches it. The next book's search is
  started in the background while you look at the current one, so results are usually ready when you arrive.
- **Auto-download 100s** (toggle next to Rapid mode, remembered per browser) – when a book's results include a
  release scored exactly 100 from an enabled (not hidden) source, it's queued automatically after a
  one-second notice. Lower scores, hidden sources, or the toggle off → it waits for you as usual. Only applies
  to books still marked missing (never re-queues a failed book's release). With rapid mode on, runs of perfect
  matches go through hands-free and stop at the first book that needs you; combine with pre-search so the
  results are already there.
- Click any cover to see it full size (click again or press Esc to close).
- **Activity** – live progress for queued downloads, recent successes and failures. A failed download offers
  **Retry** (same release), **Search again** (unmatch and jump to that book's search to pick a different
  release) and **Unmatch** (forget the release and mark the book missing again on the Books page). The same Retry /
  Unmatch buttons appear in a failed book's panel, above its search results.
- **Settings** – current config (secrets hidden) and a connection test.

Downloads run in the background one at a time (`WORKER_CONCURRENCY`), so you can queue several books and keep
browsing. Interrupted downloads resume when the container restarts. State lives in `./data/state.db`.

## CLI

```
abs-ebook-filler scan [--library ID]
abs-ebook-filler dry-run [--limit 5]
abs-ebook-filler probe [--title "..."] [--author "..."] [--out file.json]
abs-ebook-filler run [--item ID] [--limit N] [--retry-skipped]   # interactive terminal picker
abs-ebook-filler presearch [--count 100] [--no-rescan]                  # search ahead; results shown with ⚡ in the UI
abs-ebook-filler web
```

Via Docker: `docker compose run --rm abs-ebook-filler <command>`.

## Security

`SHELFMARK_API_KEY` and `ABS_TOKEN` are admin credentials. The web UI is protected by HTTP basic auth;
put it behind HTTPS (your reverse proxy) if it's reachable beyond your LAN.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Troubleshooting

- **"Audiobook folder not found"** – `PATH_MAP` or the library volume is wrong.
- **Probe shows `shelfmark_manual_releases` failing with 503** – every release source errored (e.g. Anna's
  Archive unreachable / bypass failing); check Shelfmark's own logs.
- **"Some sources failed … annas-archive.gl is rate-limited (429)"** – Anna's Archive throttles bursts of
  searches. Results from the other sources are still shown; wait out the cooldown Shelfmark mentions and click
  **Search again**. The tool runs only one release search at a time (`SEARCH_CONCURRENCY=1`) and skips its
  metadata fallback when a source has failed, so it doesn't add to the throttling.
- **The same book shows up once even though it's in two ABS libraries** – intended; overlapping libraries are
  de-duplicated by folder. Set `ABS_LIBRARY_IDS` to just your top-level library if you prefer.
- **Ebook saved but ABS doesn't show "Read"** – run a library scan in ABS; check the file owner/permissions.
- **Shelfmark refuses the download (403)** – the request policy for that source is set to REQUEST/BLOCKED.
