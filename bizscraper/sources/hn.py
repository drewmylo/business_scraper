"""Hacker News via the free Algolia API (https://hn.algolia.com/api).

Two harvests:
- show_hn: every "Show HN" launch, paged backwards by date, then incrementally.
- revenue: stories and Ask HN threads that talk about revenue, plus every top-level
  comment in Ask HN threads where founders share what their projects earn.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterator

from .. import db
from ..http import PoliteSession
from ..text import domain_of, find_revenue, first_business_url, strip_html

log = logging.getLogger(__name__)
API = "https://hn.algolia.com/api/v1"
PAGE = 1000  # Algolia's maximum hits per request

REVENUE_QUERIES = [
    "MRR", "ARR", "per month revenue", "profitable side project", "bootstrapped revenue",
    "indie hacker revenue", "micro saas", "acquired my saas", "sold my saas", "ramen profitable",
]
# Ask HN threads where commenters list their businesses and revenue.
REVENUE_THREAD_QUERIES = [
    "side project making money", "side projects revenue", "what's your side project",
    "profitable one person", "solo founder revenue", "passive income project", "bootstrapped business",
    "small saas revenue", "how much does your side project make",
]
MIN_THREAD_COMMENTS = 40


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def story_row(hit: dict, source: str) -> dict:
    text = strip_html(hit.get("story_text"))
    title = hit.get("title") or ""
    rev = find_revenue(f"{title}\n{text}")
    url = hit.get("url") or first_business_url(hit.get("story_text"))
    name = title.split(":", 1)[1].strip() if title.lower().startswith("show hn:") else title
    name = name.split(" – ")[0].split(" - ")[0].strip()
    return {
        "source": source,
        "source_id": hit["objectID"],
        "name": name,
        "url": url,
        "domain": domain_of(url),
        "title": title,
        "description": text[:4000],
        "posted_at": _iso(hit["created_at_i"]),
        "popularity": hit.get("points"),
        "comments": hit.get("num_comments"),
        "revenue_monthly": rev.monthly if rev else None,
        "revenue_currency": rev.currency if rev else None,
        "revenue_text": rev.text if rev else None,
        "tags": [t for t in hit.get("_tags", []) if not t.startswith(("author_", "story_"))],
        "raw": {k: hit.get(k) for k in ("objectID", "title", "url", "author", "points", "num_comments", "created_at_i", "_tags")},
    }


def comment_row(comment: dict, thread: dict) -> dict | None:
    raw_html = comment.get("text") or ""
    text = strip_html(raw_html)
    url = first_business_url(raw_html)
    rev = find_revenue(text)
    if not url and not rev:
        return None
    first_line = text.split("\n", 1)[0][:200]
    return {
        "source": "hn_revenue_comment",
        "source_id": str(comment["id"]),
        "name": first_line,
        "url": url,
        "domain": domain_of(url),
        "title": thread.get("title"),
        "description": text[:4000],
        "posted_at": _iso(comment["created_at_i"]) if comment.get("created_at_i") else None,
        "popularity": comment.get("points"),
        "comments": len(comment.get("children") or []),
        "revenue_monthly": rev.monthly if rev else None,
        "revenue_currency": rev.currency if rev else None,
        "revenue_text": rev.text if rev else None,
        "tags": ["comment", f"thread_{thread['id']}"],
        "raw": {"id": comment["id"], "author": comment.get("author"), "thread": thread["id"], "text": raw_html[:8000]},
    }


class HackerNews:
    def __init__(self, session: PoliteSession | None = None):
        self.s = session or PoliteSession(min_interval=0.3)

    def _get(self, path: str, **params) -> dict:
        r = self.s.get(f"{API}/{path}", params=params)
        r.raise_for_status()
        return r.json()

    def by_date(self, tags: str, after: int = 0, before: int | None = None, query: str = "") -> Iterator[dict]:
        """All hits for a tag filter between two timestamps, newest first, past the 1,000-hit cap."""
        upper = before
        seen: set[str] = set()
        while True:
            filters = [f"created_at_i>{after}"]
            if upper is not None:
                filters.append(f"created_at_i<{upper}")
            data = self._get("search_by_date", tags=tags, query=query,
                             numericFilters=",".join(filters), hitsPerPage=PAGE)
            hits = data.get("hits", [])
            if not hits:
                return
            for h in hits:
                if h["objectID"] not in seen:
                    seen.add(h["objectID"])
                    yield h
            oldest = min(h["created_at_i"] for h in hits)
            if len(hits) < PAGE:
                return
            # Keep the oldest second in the next window so posts sharing it are not skipped
            # (the seen set drops the repeats). If that makes no progress, step past it.
            nxt = oldest + 1
            upper = oldest if nxt == upper else nxt

    def search(self, query: str, tags: str, pages: int = 1) -> Iterator[dict]:
        for page in range(pages):
            data = self._get("search", query=query, tags=tags, hitsPerPage=PAGE, page=page)
            yield from data.get("hits", [])
            if page + 1 >= data.get("nbPages", 0):
                return

    def item(self, item_id: int | str) -> dict:
        return self._get(f"items/{item_id}")


def sync_show_hn(conn, client: HackerNews | None = None, limit: int | None = None) -> int:
    """Every Show HN post. First run backfills all history; later runs fetch only new posts."""
    client = client or HackerNews()
    after = int(db.get_state(conn, "hn_show_max_ts") or 0)
    n, newest = 0, after
    batch = []
    for hit in client.by_date("show_hn", after=after):
        batch.append(story_row(hit, "hn_show"))
        newest = max(newest, hit["created_at_i"])
        if len(batch) >= 500:
            n += db.upsert_candidates(conn, batch)
            conn.commit()
            batch.clear()
            log.info("show_hn: %d posts so far", n)
        if limit and n + len(batch) >= limit:
            break
    n += db.upsert_candidates(conn, batch)
    # Only advance the watermark after a full pass, so an interrupted backfill resumes correctly.
    if not limit:
        db.set_state(conn, "hn_show_max_ts", newest)
    conn.commit()
    return n


def sync_revenue(conn, client: HackerNews | None = None, thread_limit: int | None = None) -> int:
    """Stories that mention revenue, and founders' comments in revenue-sharing Ask HN threads."""
    client = client or HackerNews()
    n = 0
    seen_threads: dict[str, dict] = {}
    for q in REVENUE_QUERIES:
        rows = [story_row(h, "hn_revenue_story") for h in client.search(q, tags="(story,ask_hn,show_hn)")]
        rows = [r for r in rows if r["revenue_monthly"]]
        n += db.upsert_candidates(conn, rows)
        conn.commit()
        log.info("revenue stories '%s': %d with figures", q, len(rows))
    for q in REVENUE_THREAD_QUERIES:
        for h in client.search(q, tags="ask_hn"):
            if (h.get("num_comments") or 0) >= MIN_THREAD_COMMENTS:
                seen_threads[h["objectID"]] = h
    threads = sorted(seen_threads.values(), key=lambda h: -(h.get("num_comments") or 0))
    if thread_limit:
        threads = threads[:thread_limit]
    done = set((db.get_state(conn, "hn_threads_done") or "").split(",")) - {""}
    for h in threads:
        tid = h["objectID"]
        if tid in done:
            continue
        tree = client.item(tid)
        rows = [r for c in tree.get("children", []) if (r := comment_row(c, tree))]
        n += db.upsert_candidates(conn, rows)
        done.add(tid)
        db.set_state(conn, "hn_threads_done", ",".join(sorted(done)))
        conn.commit()
        log.info("thread %s (%s): %d businesses", tid, (tree.get("title") or "")[:60], len(rows))
    return n
