# Chineur2000 · collecteur de logements Amsterdam + Lausanne + Bern

Chineur2000 is a private housing-listing pipeline that:

1. Opens Facebook in a real, visible Chromium browser.
2. Reuses a locally saved login session to read configured housing groups.
3. Stores recent posts in a deduplicated SQLite database.
4. Extracts structured housing attributes and exposes them in the private Chineur2000 web UI.

The collector does not store a Facebook password. Browser session data stays under `.state/`,
which is excluded from Git. The database and collected posts stay under `data/`, also excluded
from Git.

## Setup

```bash
conda activate fb
python -m pip install -e .
python -m playwright install chromium
```

Create the private group configuration:

```bash
cp config/groups.example.json config/groups.json
```

Edit `config/groups.json` and add each group name, URL, and its `amsterdam`, `lausanne`, or `bern`
city identifier. This file is intentionally ignored.

## First login

```bash
fb-housing login
```

A browser opens. Sign in manually, wait for the Facebook home feed, then return to the terminal
and press Enter. Never commit `.state/` or share it: the saved browser state can access the
Facebook account.

To move the authenticated session to a private server without copying the entire browser profile:

```bash
fb-housing export-session
```

This creates `.state/facebook-profile/storage-state.json`. It contains active Facebook cookies and
must be transferred only over SSH, kept mode `0600`, and never committed. The collector imports it
into the destination browser profile and refreshes it after successful collections.

## Collect a small sample

```bash
fb-housing collect --max-posts 50
```

The collector stays visible by default so failures are understandable. Results are stored in
`data/listings.db`. Start with one group and a small post limit; Facebook can restrict accounts
or IP addresses when it detects automated collection. The collector enforces a minimum two-second
scroll pause, backs off when scrolling produces no new posts, and stops if Facebook displays a
login checkpoint or temporary-block warning.

Each collection also captures the visible reaction and comment counts. `raw_posts` keeps the latest
known counts, while `engagement_snapshots` keeps dated observations so popularity can be graphed or
ranked later. Missing Facebook UI counters are stored as unknown rather than assumed to be zero.

Up to three listing photos per post are downloaded into `data/images/`. SQLite keeps their source
URL, ordering and local path in `post_images`; the image bytes are deliberately kept out of the
database. The private website serves these local copies, so its visitors do not depend on expiring
Facebook CDN URLs or make direct image requests to Facebook.

## Extract filterable listings

```bash
export OPENAI_API_KEY="your-api-key"
fb-housing extract
```

The extractor sends each pending post to the OpenAI Responses API with Structured Outputs and
writes the validated result to the normalized `listings` table. It extracts monthly rent and currency, size, location,
availability, registration, contract, furnishing, applicant requirements, amenities, a short
French summary, and concise French Particularities labels. Locations are normalized by the model
to a stable Amsterdam, Lausanne, or Bern neighborhood set. Every material field keeps a supporting quote and
confidence score. Raw captured posts remain unchanged.

The default model is `gpt-5.4-mini`. Override it with `OPENAI_MODEL` or `--model`. Results are cached
by the post text, extraction version, and model, so unchanged posts do not incur another API call.
Use `--force` to deliberately rebuild them or `--limit 5` for a small trial. Extraction uses four
concurrent API requests by default; adjust this with `--workers` if needed.
Re-collecting a post only refreshes its timestamps and engagement counts; it does not invalidate the
LLM result unless the post text changes. A stable prompt-cache key also lets eligible API requests
reuse the common extraction instructions.

After extraction, the same command reviews saved photos with `gpt-5.4-mini` at low image detail and
chooses a useful housing photo instead of a portrait, screenshot or unrelated image. Image reviews
are cached by the image bytes and model. Override the model with `OPENAI_VISION_MODEL` or
`--vision-model`, or use `--skip-image-review` when only text extraction is wanted.

Private-group post text is sent to the configured OpenAI API project during extraction. API
response storage is disabled (`store=False`), but do not run extraction if that data transfer is
incompatible with your privacy requirements.

Run the unit tests with:

```bash
python -m unittest discover -s tests
```

## Browse the listings

Start the private web interface after collecting and extracting posts:

```bash
fb-housing serve
```

Open `http://127.0.0.1:8000`. The French interface first asks for Amsterdam, Lausanne, or Bern, then offers
city-specific neighborhood and currency filters, maximum rent, minimum room size, registration and
extracted particularities. Results can be sorted by discovery time, price or Facebook engagement.
Listings disappear from the website 14 days after first collection without being deleted from SQLite.
The website never shows raw post text and links back to Facebook for the original context.

Override the defaults when needed:

```bash
fb-housing serve --database /path/to/listings.db --host 0.0.0.0 --port 8000
```

Do not expose the app publicly before adding access control; private-group summaries are still
private information.

## VPS layout

The production layout keeps deployable code separate from private runtime state:

- `/home/nathan/facebookrooms`: Git checkout, virtual environment and private `.env` file.
- `/home/nathan/facebookrooms-data`: SQLite database and downloaded listing photos.
- `facebookrooms.service`: web application on localhost port 8000.
- Caddy: HTTPS, password protection and reverse proxy for `facebookrooms.nl`.
- `facebookrooms-collect.timer`: optional randomized refresh every two hours, with up to 20 minutes
  of jitter. Enable this only
  after a Facebook session and `OPENAI_API_KEY` have been installed on the VPS.
- `facebookrooms-digest.timer`: personalized digests at 09:00 in the `Europe/Amsterdam` timezone.

## Daily e-mail and Telegram alerts

The current result filters can be saved from a city page. E-mail subscriptions use double opt-in;
Telegram subscriptions open a bot deep link and become active only after the user presses Start.
Each verified destination receives at most one combined message per day and never receives the same
listing twice in the same digest. Days without new matching listings stay quiet.

Configure providers interactively on the VPS:

```bash
cd /home/nathan/facebookrooms
bash deploy/configure-notifications.sh
```

The script asks for a separate administrator password, optional Resend credentials, and optional
Telegram BotFather credentials. It generates the application and Telegram webhook secrets, updates
the private `.env`, installs the 09:00 timer, configures the Telegram webhook when possible, and
reloads Caddy. It can safely be rerun to add a provider later without rotating existing application
or webhook secrets. For e-mail, verify `facebookrooms.nl` (or a sending subdomain) in Resend and register
`https://facebookrooms.nl/webhooks/resend` as the webhook endpoint.

Subscription verification, management, unsubscribe, and provider webhook routes bypass the shared
website password because they carry signed, single-purpose tokens or verified webhook secrets.
The `/admin` dashboard has its own Caddy password and is additionally inaccessible to requests that
do not pass through the protected Caddy admin route.

The dashboard shows masked recipients, saved-search counts, provider readiness, delivery results,
collection/extraction job history, listing counts, free disk space, and public-page traffic. Traffic
records contain the timestamp, full client IP, requested page, referrer and a compact browser label.
Each public IP is geolocated once through `ipwho.is`, then served from the local SQLite cache. Static
assets, health checks, errors and admin requests are not counted. Visit history is retained without an
automatic expiry. The dashboard never displays raw private Facebook post text or provider secrets.

Deployment templates are stored in `deploy/`. Never commit the Caddy password hash, `.env`, browser
session or production database.

## Privacy boundary

This project is for a small private browsing interface. Do not publish session state or raw
private-group content. The traffic dashboard intentionally stores IP addresses indefinitely and sends
new public IPs to `ipwho.is` for approximate geolocation. If the site is opened to a wider audience,
add an appropriate privacy notice and revisit retention before inviting visitors. Listing cards link
back to the original Facebook post.
