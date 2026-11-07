"""Merge the classifier's fine-grained categories into a few hundred comparable markets.

Categories are free text, so near-duplicates ("dog grooming booking app", "booking software for dog
groomers") split the evidence. This pass sends the category names (not the rows) to Claude in chunks
of 250, with the markets chosen so far, and stores category -> market in `market_map`.
"""
from __future__ import annotations

import json
import logging

import anthropic

from . import db
from .classify import MODEL, ensure_schema as ensure_classify_schema

log = logging.getLogger(__name__)
CHUNK = 250

TYPES = ["software", "service", "ecommerce or physical product", "content or media", "marketplace",
         "community or education", "unclear"]

SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "market": {"type": "string"},
            "market_type": {"type": "string", "enum": TYPES},
        },
        "required": ["category", "market", "market_type"],
        "additionalProperties": False,
    }}},
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = """You group small-business categories into markets, for a founder in Australia comparing \
which business models are proven overseas. Any kind of business counts: software, services, retail, \
physical products, content, marketplaces.

For each category, return the market it belongs to:
- A market is a group of businesses that sell a similar thing to a similar buyer, so their results \
can be compared. Name it in lowercase, 2 to 6 words, buyer included where it matters, e.g. \
"booking software for pet services", "ad-supported niche content sites", "shopify merchant apps", \
"mobile car detailing", "subscription coffee".
- Reuse an existing market name exactly when one fits. Create a new one only when none fits.
- Aim for a few hundred markets overall: merge near-duplicates and wording variants, but keep \
different buyers or industries apart (scheduling for clinics is not scheduling for salons).
- Use "unclear" (market and market_type) when the category is too vague to place, e.g. \
"unspecified saas".
- market_type: what kind of business the market is.
Return every category exactly as given."""

DDL = """
CREATE TABLE IF NOT EXISTS market_map (
    category    TEXT PRIMARY KEY,
    market      TEXT NOT NULL,
    market_type TEXT NOT NULL,
    model       TEXT NOT NULL,
    mapped_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_map_market ON market_map(market);
"""


def ensure_schema(conn) -> None:
    ensure_classify_schema(conn)
    conn.executescript(DDL)


def unmapped_categories(conn) -> list[str]:
    return [r[0] for r in conn.execute("""
        SELECT k.category FROM classifications k LEFT JOIN market_map m ON m.category = k.category
        WHERE k.is_business = 1 AND k.category != '' AND m.category IS NULL
        GROUP BY k.category ORDER BY COUNT(*) DESC, k.category""")]


def existing_markets(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT market FROM market_map WHERE market != 'unclear' GROUP BY market ORDER BY COUNT(*) DESC, market")]


def map_chunk(client, model: str, categories: list[str], markets: list[str]) -> list[dict] | None:
    known = "\n".join(markets) if markets else "(none yet)"
    # Long output (250 mappings), so stream: the SDK refuses non-streaming calls this large.
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Existing markets:\n{known}\n\nCategories:\n{json.dumps(categories)}"}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason in ("refusal", "max_tokens"):
        log.warning("chunk skipped: stop_reason=%s", response.stop_reason)
        return None
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["items"]


def run(conn, client=None, model: str = MODEL) -> int:
    ensure_schema(conn)
    client = client or anthropic.Anthropic()
    todo = unmapped_categories(conn)
    log.info("%d categories to map in %d requests", len(todo), -(-len(todo) // CHUNK))
    n = 0
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        items = map_chunk(client, model, chunk, existing_markets(conn))
        if not items:
            continue
        wanted, ts = set(chunk), db.now()
        for it in items:
            if it["category"] in wanted:
                conn.execute("INSERT OR REPLACE INTO market_map VALUES (?, ?, ?, ?, ?)",
                             (it["category"], it["market"].strip().lower(), it["market_type"], model, ts))
                n += 1
        conn.commit()
        log.info("mapped %d/%d categories into %d markets", n, len(todo), len(existing_markets(conn)))
    return n


def report(conn, top: int = 60, min_items: int = 3, market_type: str | None = None) -> list:
    """Markets ranked by overseas proof: businesses reporting their own revenue, then total revenue."""
    ensure_schema(conn)
    where, params = "", [min_items, top]
    if market_type:
        where, params = "AND m.market_type = ?", [market_type, min_items, top]
    return conn.execute(f"""
        SELECT m.market, m.market_type, COUNT(*) AS businesses,
               SUM(k.revenue_is_own) AS with_revenue,
               ROUND(MAX(k.revenue_monthly_usd)) AS max_mrr_usd,
               ROUND(SUM(COALESCE(k.revenue_monthly_usd, 0))) AS total_mrr_usd,
               ROUND(100.0 * SUM(k.buyer IN ('B2B', 'both')) / COUNT(*)) AS b2b_pct,
               GROUP_CONCAT(DISTINCT k.customer_industry) AS industries,
               (SELECT GROUP_CONCAT(name, '; ') FROM (
                   SELECT k2.business_name AS name FROM classifications k2
                   JOIN market_map m2 ON m2.category = k2.category
                   WHERE m2.market = m.market AND k2.is_business = 1 AND k2.business_name != ''
                   ORDER BY k2.revenue_monthly_usd DESC NULLS LAST LIMIT 4)) AS examples
        FROM classifications k JOIN market_map m ON m.category = k.category
        WHERE k.is_business = 1 AND m.market != 'unclear' {where}
        GROUP BY m.market HAVING COUNT(*) >= ?
        ORDER BY with_revenue DESC, total_mrr_usd DESC, businesses DESC
        LIMIT ?""", params).fetchall()
