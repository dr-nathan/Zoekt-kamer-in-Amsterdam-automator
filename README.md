# Amsterdam Facebook housing collector

This experimental branch is rebuilding the original script as a private pipeline:

1. Open Facebook in a real, visible Chromium browser.
2. Reuse a locally saved login session to read configured housing groups.
3. Store recent posts in a deduplicated SQLite database.
4. Later, extract structured housing attributes and expose them in a private web UI.

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

Edit `config/groups.json` and add each group name and URL. This file is intentionally ignored.

## First login

```bash
fb-housing login
```

A browser opens. Sign in manually, wait for the Facebook home feed, then return to the terminal
and press Enter. Never commit `.state/` or share it: the saved browser state can access the
Facebook account.

## Collect a small sample

```bash
fb-housing collect --max-posts 50
```

The collector stays visible by default so failures are understandable. Results are stored in
`data/listings.db`. Start with one group and a small post limit; Facebook can restrict accounts
or IP addresses when it detects automated collection. The collector enforces a minimum two-second
scroll pause, backs off when scrolling produces no new posts, and stops if Facebook displays a
login checkpoint or temporary-block warning.

## Extract filterable listings

```bash
export OPENAI_API_KEY="your-api-key"
fb-housing extract
```

The extractor sends each pending post to the OpenAI Responses API with Structured Outputs and
writes the validated result to the normalized `listings` table. It extracts price, size, location,
availability, registration, contract, furnishing, applicant requirements, amenities, a short
summary, and concise Particularities labels. Every material field keeps a supporting quote and
confidence score. Raw captured posts remain unchanged.

The default model is `gpt-5-nano`. Override it with `OPENAI_MODEL` or `--model`. Results are cached
by the post text, extraction version, and model, so unchanged posts do not incur another API call.
Use `--force` to deliberately rebuild them or `--limit 5` for a small trial.

Private-group post text is sent to the configured OpenAI API project during extraction. API
response storage is disabled (`store=False`), but do not run extraction if that data transfer is
incompatible with your privacy requirements.

Run the unit tests with:

```bash
python -m unittest discover -s tests
```

## Privacy boundary

This project is for a single user's private browsing interface. Do not publish session state or
raw private-group content. The later UI should minimize stored personal data and link back to the
original Facebook post.

## Legacy prototype

### Description
The goal of this code is as follows:

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
