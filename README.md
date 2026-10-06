<a href="https://gitviewsmap.onrender.com/dr-nathan">
  <img src="https://gitviewsmap.onrender.com/badge/dr-nathan.svg" alt="GitViewsMap Visitor Badge" />
</a>

# Chineur2000

<p align="center">
  <img src="src/fb_automator/web_assets/static/cities/amsterdam-icon.png" alt="Amsterdam" width="150">
  <img src="src/fb_automator/web_assets/static/cities/lausanne-icon.png" alt="Lausanne" width="150">
  <img src="src/fb_automator/web_assets/static/cities/bern-icon.png" alt="Bern" width="150">
</p>

**A searchable housing feed built from the places where the best listings often appear first: local Facebook groups.**

Housing groups contain great rooms and apartments, but discovery is fragmented and the posts are
unstructured. Chineur2000 collects recent listings, turns their free-form text into consistent data,
removes duplicates and presents everything in a simple city-based website.

The current feed covers **Amsterdam, Lausanne and Bern**.

## What it does

- Collects recent housing posts from configured Facebook groups with Playwright.
- Extracts rent, size, location, availability, registration rules, furnishing, amenities and other
  useful attributes with structured LLM output.
- Normalizes locations into city-specific neighborhoods while retaining nearby municipalities.
- Detects exact copies, cross-posts and similar reposts across groups.
- Saves listing images locally and selects the most useful cover image.
- Tracks reactions and comments so listings can be ranked by activity.
- Offers city, neighborhood, price, size and particularity filters in a responsive French interface.
- Automatically removes listings older than two weeks from the visible feed.
- Sends personalized daily e-mail or Telegram digests for saved searches.
- Caches text extraction and image review results so unchanged posts do not consume additional API
  calls.

## How it works

```text
Facebook housing groups
          │
          ▼
 Playwright collector ──────► SQLite + local images
          │                         │
          ▼                         ▼
 Structured LLM extraction ─► deduplication and ranking
                                    │
                                    ▼
                         FastAPI/Jinja web interface
                                    │
                                    ├──► searchable city feeds
                                    └──► e-mail and Telegram alerts
```

Raw posts and extracted listings live in the same SQLite database as separate layers. The original
capture remains available for reprocessing, while normalized listing records power the filters and
alerts. Extraction and image review are content-addressed, making repeated collection economical.

## Product highlights

### Multi-city discovery

Each city has its own groups, currency and neighborhood vocabulary. The landing page opens into a
dedicated Amsterdam, Lausanne or Bern feed with filters tailored to that location.

### LLM-native extraction

Housing posts vary widely in language and format. Instead of relying on a growing collection of
regular expressions, Chineur2000 uses validated structured output to interpret Dutch, French,
German and English listings consistently.

### Fresh, deduplicated results

The collector keeps engagement counts current, combines reposts and limits the website to recent
offers. The source Facebook link remains attached to every listing for the definitive context.

### Personalized alerts

Saved searches support multiple neighborhoods and particularities. Subscribers receive one concise
daily digest only when new matching listings are available, with self-service management and
unsubscribe links.

## Technology

- **Python** for collection, extraction and application logic
- **Playwright** for browser-based Facebook collection
- **OpenAI Responses API** with Pydantic structured outputs
- **FastAPI + Jinja** for the server-rendered website
- **SQLite** for raw posts, normalized listings, subscriptions and operational data
- **Resend + Telegram Bot API** for daily notifications
- **Caddy + systemd** for the self-hosted production deployment

## Run locally

Requirements: Python 3.11+ and a Chromium-compatible Playwright installation.

```bash
python -m pip install -e .
python -m playwright install chromium
cp config/groups.example.json config/groups.json
```

Authenticate Facebook interactively, collect a small sample, extract its attributes and start the
website:

```bash
fb-housing login
fb-housing collect --max-posts 50
fb-housing extract
fb-housing serve
```

The interface is then available at `http://127.0.0.1:8000`. Runtime configuration and optional
provider settings are documented in [`.env.example`](.env.example), while the group format is shown
in [`config/groups.example.json`](config/groups.example.json).

Run the test suite with:

```bash
python -m unittest discover -s tests
```

## Project structure

```text
src/fb_automator/
├── collector.py          Facebook collection and scrolling
├── extractor.py          Structured listing extraction
├── image_reviewer.py     Listing-cover selection
├── storage.py            SQLite schema and persistence
├── notifications.py      Saved searches and daily digests
├── visitor_tracking.py   Lightweight site analytics
├── web.py                FastAPI application
└── web_assets/           Templates, styles and city artwork

config/                   Example group configuration
deploy/                   Self-hosting templates
tests/                    Unit and browser-assisted tests
```

## Status

Chineur2000 is an actively developed personal project and learning playground for browser
automation, structured extraction and self-hosted product development. It is not affiliated with or
endorsed by Facebook or Meta. Anyone adapting the collector should respect group privacy, local law
and the rules of the platforms they access.


