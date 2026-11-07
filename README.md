# business_scraper

Collects small overseas businesses that publicly show revenue, plus Australian market data, as the
first stage of a method for finding businesses that work in the US and Europe but are missing or
underserved in Australia.

## Sources (free)

| Command | Source | Key needed | What it collects |
| --- | --- | --- | --- |
| `bizscraper sync hn-show` | Hacker News (Algolia API) | none | Every Show HN launch since 2007, then only new ones |
| `bizscraper sync hn-revenue` | Hacker News (Algolia API) | none | Stories that state revenue, and founders' comments in Ask HN "what does your side project make" threads |
| `bizscraper sync reddit` | Reddit Data API | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Owners describing their business and revenue in r/sweatystartup, r/smallbusiness, r/Entrepreneur, trade and e-commerce subreddits |
| `bizscraper sync producthunt` | Product Hunt API v2 | `PRODUCTHUNT_TOKEN` | Launches with tagline, topics, votes |
| `bizscraper sync abs` | ABS Data API, Australian Industry (8155) | none | Employment, sales, profit by ANZSIC industry, to size AU customer pools |
| `bizscraper abn "name"` | ABN Lookup | `ABN_LOOKUP_GUID` | Australian businesses trading under a name |

`bizscraper sync all` runs every source and skips ones whose key is missing.

Revenue figures are pulled from text with patterns like "$10k MRR", "$3,500/month" and "$120k ARR",
normalised to per month and capped at $1M/month. They are a rough signal: a post can quote someone
else's revenue. The next stage (LLM classification) will clean this up.

## Classifying with Claude

`bizscraper classify` sends rows to Claude (default model `claude-opus-5-5`, override with
`BIZSCRAPER_MODEL`) 20 at a time and stores, per row: whether it is a real business, its name, a
one-line description, buyer type, the customer's ANZSIC division, a market category, an Australian
search phrase, and whether the revenue figure is the business's own (with a cleaned USD/month figure).

It picks rows with a revenue figure, every revenue-thread comment, and Show HN / Product Hunt posts
with at least `--min-points` (default 200). Today that is about 8,000 rows, roughly 400 requests;
expect around $30 to $50 for the first pass on Opus 5.5 (an estimate, not measured), then a few
dollars a week for new rows. Try `--limit 100` first to check quality and cost. Requests opt into
server-side refusal fallbacks (`fallbacks: "default"`), so a declined batch is retried on another model.

`bizscraper categories` then ranks markets by overseas proof: how many businesses share the category,
how many report their own revenue, and the largest and total monthly revenue. These are the
categories to take into the Australian checks.

## Merging categories into markets

`bizscraper markets --map` sends the category names (not the rows) to Claude in chunks of 250 and
maps each to one of a few hundred markets, typed as software, service, ecommerce or physical product,
content, marketplace, or community. `bizscraper markets` then ranks markets by how many businesses
report their own revenue; `--type service` narrows to one kind. Cheap: about 15 requests for the
current 3,300 categories.

## Running locally

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # add keys if you have them, incl. ANTHROPIC_API_KEY
.venv/bin/bizscraper sync all
.venv/bin/bizscraper stats
.venv/bin/bizscraper export --revenue-only --out data/candidates_with_revenue.csv
.venv/bin/bizscraper classify --limit 100   # trial run
.venv/bin/bizscraper categories
```

The first `hn-show` run backfills about 215,000 posts and takes about 4 minutes. Data goes to
`data/bizscraper.db` (SQLite). `bizscraper reparse` re-runs revenue extraction after the patterns change.

## Weekly automation

`.github/workflows/sync.yml` runs every Monday morning AEST (or on demand from the Actions tab). It
restores the database from the `data` release, syncs everything, and uploads the database and CSV
exports back to that release. Add `PRODUCTHUNT_TOKEN`, `ABN_LOOKUP_GUID`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` and `ANTHROPIC_API_KEY` as
repository secrets to include those sources and classification.

## Getting the free keys

- Product Hunt: create an application at https://www.producthunt.com/v2/oauth/applications and copy
  its developer token. The API is free for non-commercial use.
- ABN Lookup: register at https://abr.business.gov.au/Tools/WebServicesAgreement; the GUID arrives by email.

## Tables

- `candidates`: one row per overseas business signal (source, name, domain, revenue, raw payload)
- `au_industry`: ABS industry measures by year
- `au_entities`: ABN Lookup results by query
- `classifications`: Claude's tags per candidate
- `market_map`: category to market
- `runs`, `sync_state`: run log and incremental sync watermarks
