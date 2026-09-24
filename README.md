# Chineur2000 · collecteur de logements Amsterdam + Lausanne

This experimental branch is rebuilding the original script as a private pipeline:

1. Open Facebook in a real, visible Chromium browser.
2. Reuse a locally saved login session to read configured housing groups.
3. Store recent posts in a deduplicated SQLite database.
4. Extract structured housing attributes and expose them in the private Chineur2000 web UI.

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

Edit `config/groups.json` and add each group name, URL, and its `amsterdam` or `lausanne`
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
to a stable Amsterdam or Lausanne neighborhood set. Every material field keeps a supporting quote and
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

Open `http://127.0.0.1:8000`. The French interface first asks for Amsterdam or Lausanne, then offers
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
- `facebookrooms-collect.timer`: optional randomized refresh every 30–40 minutes. Enable this only
  after a Facebook session and `OPENAI_API_KEY` have been installed on the VPS.

Deployment templates are stored in `deploy/`. Never commit the Caddy password hash, `.env`, browser
session or production database.

## Privacy boundary

This project is for a single user's private browsing interface. Do not publish session state or
raw private-group content. The later UI should minimize stored personal data and link back to the
original Facebook post.

## Legacy Amsterdam prototype

### Description
The original, superseded goal of this code was as follows:

1. Scrape pre-determined Facebook groups for room listings in Amsterdam (currently, Zoekt Kamer in Amsterdam Community).

2. Filter the rooms on the criteria you set, eg. price, location, size of the room

3. Send the matches to your email, daily. 

### About the code
This project heavily relies on the already existing Facebook scraper found here : [Facebook-scraper](https://github.com/kevinzg/facebook-scraper).

In the repository also resides a CSV file from the city of Amsterdam, containing all the adresses of Amsterdam. (Freely accessible from https://data.amsterdam.nl ).

### usage
First, make sure you install the Facebook scraper from Kevin's repo (listed above).

In the repository file, you shoud insert a `cookies.json` file, containing your Facebook cookies, in JSON format. See Kevin's page for more info

Set your criteria : locations of interest, (the easiest is to insert a list of postcodes), max price you are willing to pay, minimum room size, and Scrape away.

(Send to email function not implemented yet)

You should also probably have git LFS installed, as the large CSV file makes use of LFS (https://git-lfs.github.com/).

### Disclaimer
This is a non-serious project and is in a very imperfect state. Feel free to clone, fork, create pull requests.
