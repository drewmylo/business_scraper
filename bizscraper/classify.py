"""Tag and group collected businesses with Claude.

For each candidate row Claude decides whether it is a real business or product, names it, and
records: what it does, who buys it (B2B/B2C), ANZSIC industry division, a short category name that
groups similar products, an AU search phrase, and whether the revenue figure is the business's own.

Rows are sent 20 at a time with a JSON schema, so every response parses. Already-classified rows
are skipped, so the command can be stopped and rerun.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Iterable

import anthropic

from . import db

log = logging.getLogger(__name__)

MODEL = os.environ.get("BIZSCRAPER_MODEL", "claude-opus-5-5")
BATCH = 20
DESC_CHARS = 1200

ANZSIC = {
    "A": "Agriculture, Forestry and Fishing", "B": "Mining", "C": "Manufacturing",
    "D": "Electricity, Gas, Water and Waste Services", "E": "Construction", "F": "Wholesale Trade",
    "G": "Retail Trade", "H": "Accommodation and Food Services", "I": "Transport, Postal and Warehousing",
    "J": "Information Media and Telecommunications", "K": "Financial and Insurance Services",
    "L": "Rental, Hiring and Real Estate Services", "M": "Professional, Scientific and Technical Services",
    "N": "Administrative and Support Services", "O": "Public Administration and Safety",
    "P": "Education and Training", "Q": "Health Care and Social Assistance", "R": "Arts and Recreation Services",
    "S": "Other Services", "X": "Consumers or no single industry",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "is_business": {"type": "boolean"},
                    "business_name": {"type": "string"},
                    "one_liner": {"type": "string"},
                    "buyer": {"type": "string", "enum": ["B2B", "B2C", "both", "unknown"]},
                    "customer_industry": {"type": "string", "enum": list(ANZSIC)},
                    "category": {"type": "string"},
                    "search_phrase": {"type": "string"},
                    "software_leveraged": {"type": "boolean"},
                    "revenue_is_own": {"type": "boolean"},
                    "revenue_monthly_usd": {"type": ["number", "null"]},
                    "country": {"type": "string"},
                },
                "required": ["id", "is_business", "business_name", "one_liner", "buyer", "customer_industry",
                             "category", "search_phrase", "software_leveraged", "revenue_is_own",
                             "revenue_monthly_usd", "country"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = f"""You classify small businesses and products found on Hacker News, Reddit and Product Hunt, for a \
founder in Australia looking for business models proven overseas that could work in Australia.

For each item, return one object with the same id:
- is_business: true if this is a product or business someone pays for or could pay for, of any kind: \
software, a trade or service business, a shop, a physical product, content. False for \
articles, open-source libraries with no paid offering, games made for fun, personal sites, questions, \
or comments that do not describe a specific business.
- business_name: the product or company name, or "" if none.
- one_liner: what it does and for whom, at most 15 words.
- buyer: who pays: B2B, B2C, both, or unknown.
- customer_industry: the ANZSIC division letter of the paying customer's industry (X if consumers or \
no single industry). Divisions: {"; ".join(f"{k} {v}" for k, v in ANZSIC.items())}.
- category: a short lowercase name for the market, 2 to 5 words, specific enough that competitors \
share it and general enough that more than one product could, e.g. "booking software for dog \
groomers", "invoicing for tradespeople", "ai headshot generator". Reuse a category from the list \
of existing categories when one fits.
- search_phrase: what an Australian buyer would type into Google to find this, 2 to 5 words.
- software_leveraged: true if software is the product or does most of the delivery.
- revenue_is_own: true only if the text states revenue earned by this item's own business, not \
another company's, a goal, a funding round, or a hypothetical.
- revenue_monthly_usd: that revenue per month in USD (convert ARR or yearly figures; EUR, GBP, AUD \
at rough current rates), or null if revenue_is_own is false.
- country: the business's country as an ISO 3166 alpha-2 code if stated or obvious, else "".
Use only what the text says or clearly implies; do not guess revenue."""

DDL = """
CREATE TABLE IF NOT EXISTS classifications (
    candidate_id        INTEGER PRIMARY KEY REFERENCES candidates(id),
    model               TEXT NOT NULL,
    classified_at       TEXT NOT NULL,
    is_business         INTEGER,
    business_name       TEXT,
    one_liner           TEXT,
    buyer               TEXT,
    customer_industry   TEXT,
    category            TEXT,
    search_phrase       TEXT,
    software_leveraged  INTEGER,
    revenue_is_own      INTEGER,
    revenue_monthly_usd REAL,
    country             TEXT
);
CREATE INDEX IF NOT EXISTS idx_class_category ON classifications(category);
"""


def ensure_schema(conn) -> None:
    conn.executescript(DDL)


def select_rows(conn, min_points: int = 200, limit: int | None = None) -> list:
    """Unclassified rows most likely to be real businesses: any with a revenue figure, every revenue
    thread comment, and Show HN / Product Hunt posts above a popularity threshold. Revenue first."""
    sql = """
        SELECT c.id, c.source, c.name, c.domain, c.title, c.description, c.revenue_text, c.popularity
        FROM candidates c LEFT JOIN classifications k ON k.candidate_id = c.id
        WHERE k.candidate_id IS NULL
          AND (c.revenue_monthly IS NOT NULL OR c.source IN ('hn_revenue_comment', 'reddit_comment')
               OR (c.source != 'reddit_post' AND c.popularity >= ?))
        ORDER BY (c.revenue_monthly IS NOT NULL) DESC, c.popularity DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (min_points,)).fetchall()


def existing_categories(conn, n: int = 300) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT category FROM classifications WHERE is_business = 1 AND category != '' "
        "GROUP BY category ORDER BY COUNT(*) DESC, category LIMIT ?", (n,))]


def render(rows: Iterable) -> str:
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "source": r["source"], "name": r["name"], "domain": r["domain"],
            "title": r["title"], "text": (r["description"] or "")[:DESC_CHARS],
            "revenue_phrase_found": r["revenue_text"],
        })
    return json.dumps(items, ensure_ascii=False)


class Classifier:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = MODEL):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
        self._lock = threading.Lock()

    def classify(self, rows: list, categories: list[str]) -> list[dict] | None:
        cats = "\n".join(categories) if categories else "(none yet)"
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Existing categories:\n{cats}\n\nItems:\n{render(rows)}"}],
        )
        with self._lock:
            self.usage["requests"] += 1
            self.usage["input_tokens"] += response.usage.input_tokens + (response.usage.cache_read_input_tokens or 0)
            self.usage["output_tokens"] += response.usage.output_tokens
        if response.stop_reason in ("refusal", "max_tokens"):
            log.warning("batch skipped: stop_reason=%s", response.stop_reason)
            return None
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)["items"]


def save(conn, model: str, items: list[dict], valid_ids: set[int]) -> int:
    ts = db.now()
    n = 0
    for it in items:
        if it["id"] not in valid_ids:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO classifications VALUES "
            "(:id, :model, :ts, :is_business, :business_name, :one_liner, :buyer, :customer_industry, "
            ":category, :search_phrase, :software_leveraged, :revenue_is_own, :revenue_monthly_usd, :country)",
            {**it, "model": model, "ts": ts, "category": it["category"].strip().lower()},
        )
        n += 1
    conn.commit()
    return n


def run(conn, min_points: int = 200, limit: int | None = None, classifier: Classifier | None = None,
        workers: int = 4, max_minutes: float | None = None) -> int:
    """Classify in parallel batches. Stops cleanly after max_minutes so results are always saved;
    rows left over are picked up by the next run."""
    ensure_schema(conn)
    rows = select_rows(conn, min_points, limit)
    chunks = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    log.info("%d rows to classify in %d requests", len(rows), len(chunks))
    classifier = classifier or Classifier()
    deadline = time.monotonic() + max_minutes * 60 if max_minutes is not None else None
    done = 0
    pending: dict = {}
    queue = iter(chunks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def submit() -> bool:
            if deadline is not None and time.monotonic() > deadline:
                return False
            chunk = next(queue, None)
            if chunk is None:
                return False
            pending[pool.submit(classifier.classify, chunk, existing_categories(conn))] = chunk
            return True

        for _ in range(workers):
            submit()
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                chunk = pending.pop(fut)
                try:
                    items = fut.result()
                except anthropic.APIStatusError as e:
                    if e.status_code in (400, 401, 403, 404):
                        for f in pending:
                            f.cancel()
                        raise
                    log.warning("batch failed (%s), will retry next run", e.status_code)
                    items = None
                except anthropic.APIConnectionError:
                    log.warning("batch failed (connection), will retry next run")
                    items = None
                if items is not None:
                    done += save(conn, classifier.model, items, {r["id"] for r in chunk})
                u = classifier.usage
                log.info("classified %d/%d (requests %d, in %d tok, out %d tok)", done, len(rows),
                         u["requests"], u["input_tokens"], u["output_tokens"])
                submit()
    if deadline is not None and time.monotonic() > deadline:
        log.info("time budget reached; %d rows left for the next run", len(rows) - done)
    return done


def categories_report(conn, top: int = 50, min_items: int = 2) -> list:
    """Categories ranked by overseas proof: number of businesses and revenue they report."""
    ensure_schema(conn)
    return conn.execute("""
        SELECT category, COUNT(*) AS businesses,
               SUM(revenue_is_own) AS with_revenue,
               ROUND(MAX(revenue_monthly_usd)) AS max_mrr_usd,
               ROUND(SUM(COALESCE(revenue_monthly_usd, 0))) AS total_mrr_usd,
               GROUP_CONCAT(DISTINCT customer_industry) AS industries,
               MAX(search_phrase) AS search_phrase,
               SUM(buyer = 'B2B') AS b2b
        FROM classifications
        WHERE is_business = 1 AND software_leveraged = 1 AND category != ''
        GROUP BY category HAVING COUNT(*) >= ?
        ORDER BY with_revenue DESC, total_mrr_usd DESC, businesses DESC
        LIMIT ?""", (min_items, top)).fetchall()
