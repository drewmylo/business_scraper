# business_scraper

Collects small overseas businesses that publicly show revenue, plus Australian market data, as the
first stage of a method for finding businesses that work in the US and Europe but are missing or
underserved in Australia.

## Sources (free)

| Command | Source | Key needed | What it collects |
| --- | --- | --- | --- |
| `bizscraper sync hn-show` | Hacker News (Algolia API) | none | Every Show HN launch since 2007, then only new ones |
| `bizscraper sync hn-revenue` | Hacker News (Algolia API) | none | Stories that state revenue, and founders' comments in Ask HN "what does your side project make" threads |
| `bizscraper sync producthunt` | Product Hunt API v2 | `PRODUCTHUNT_TOKEN` | Launches with tagline, topics, votes |
| `bizscraper sync abs` | ABS Data API, Australian Industry (8155) | none | Employment, sales, profit by ANZSIC industry, to size AU customer pools |
| `bizscraper abn "name"` | ABN Lookup | `ABN_LOOKUP_GUID` | Australian businesses trading under a name |

`bizscraper sync all` runs every source and skips ones whose key is missing.

Revenue figures are pulled from text with patterns like "$10k MRR", "$3,500/month" and "$120k ARR",
normalised to per month and capped at $1M/month. They are a rough signal: a post can quote someone
else's revenue. The next stage (LLM classification) will clean this up.

## Running locally

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # add keys if you have them
.venv/bin/bizscraper sync all
.venv/bin/bizscraper stats
.venv/bin/bizscraper export --revenue-only --out data/candidates_with_revenue.csv
```

The first `hn-show` run backfills about 215,000 posts and takes about 4 minutes. Data goes to
`data/bizscraper.db` (SQLite). `bizscraper reparse` re-runs revenue extraction after the patterns change.

## Weekly automation

`.github/workflows/sync.yml` runs every Monday morning AEST (or on demand from the Actions tab). It
restores the database from the `data` release, syncs everything, and uploads the database and CSV
exports back to that release. Add `PRODUCTHUNT_TOKEN` and `ABN_LOOKUP_GUID` as repository secrets
to include those sources.

## Getting the free keys

- Product Hunt: create an application at https://www.producthunt.com/v2/oauth/applications and copy
  its developer token. The API is free for non-commercial use.
- ABN Lookup: register at https://abr.business.gov.au/Tools/WebServicesAgreement; the GUID arrives by email.

## Tables

- `candidates`: one row per overseas business signal (source, name, domain, revenue, raw payload)
- `au_industry`: ABS industry measures by year
- `au_entities`: ABN Lookup results by query
- `runs`, `sync_state`: run log and incremental sync watermarks
