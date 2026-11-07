"""Reddit via the official Data API (free for non-commercial use, needs an app).

Create a "script" app at https://www.reddit.com/prefs/apps and set REDDIT_CLIENT_ID and
REDDIT_CLIENT_SECRET. Collects posts and top-level comments where owners describe their business and
revenue, from subreddits where non-tech small businesses (trades, services, retail, e-commerce) post.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .. import db
from ..http import USER_AGENT, PoliteSession
from ..text import domain_of, find_revenue, first_business_url

log = logging.getLogger(__name__)
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"

SUBREDDITS = ["sweatystartup", "smallbusiness", "Entrepreneur", "EntrepreneurRideAlong", "ecommerce",
              "SideProject", "microsaas", "SaaS", "Flipping", "AmazonFBA", "Etsy", "lawncare",
              "pressurewashing", "AutoDetailing", "foodtrucks", "Bookkeeping", "restaurantowners"]
QUERIES = ["revenue", "per month", "profit", "how much do you make", "my business makes",
           "first year in business", "\"a month\" business", "MRR"]
MIN_THREAD_COMMENTS = 30


class RedditError(RuntimeError):
    pass


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def post_row(p: dict) -> dict:
    text = p.get("selftext") or ""
    rev = find_revenue(f"{p.get('title', '')}\n{text}")
    url = p.get("url") if not p.get("is_self") else first_business_url(p.get("selftext_html") or "")
    return {
        "source": "reddit_post", "source_id": p["name"], "name": p.get("title", "")[:200], "url": url,
        "domain": domain_of(url), "title": p.get("title"), "description": text[:4000],
        "posted_at": _iso(p.get("created_utc")), "popularity": p.get("score"), "comments": p.get("num_comments"),
        "revenue_monthly": rev.monthly if rev else None, "revenue_currency": rev.currency if rev else None,
        "revenue_text": rev.text if rev else None, "tags": [f"r/{p.get('subreddit')}"],
        "raw": {k: p.get(k) for k in ("name", "subreddit", "title", "permalink", "score", "num_comments", "created_utc")},
    }


def comment_row(c: dict, thread: dict) -> dict | None:
    text = c.get("body") or ""
    rev = find_revenue(text)
    url = first_business_url(c.get("body_html") or "")
    if not rev and not url:
        return None
    return {
        "source": "reddit_comment", "source_id": c["name"], "name": text.split("\n", 1)[0][:200], "url": url,
        "domain": domain_of(url), "title": thread.get("title"), "description": text[:4000],
        "posted_at": _iso(c.get("created_utc")), "popularity": c.get("score"), "comments": None,
        "revenue_monthly": rev.monthly if rev else None, "revenue_currency": rev.currency if rev else None,
        "revenue_text": rev.text if rev else None, "tags": [f"r/{thread.get('subreddit')}", "comment"],
        "raw": {"name": c["name"], "thread": thread.get("name"), "permalink": c.get("permalink")},
    }


class Reddit:
    def __init__(self, client_id: str, client_secret: str, session: PoliteSession | None = None):
        self.s = session or PoliteSession(min_interval=1.0)  # free tier: 100 requests a minute
        r = self.s.post(TOKEN_URL, auth=(client_id, client_secret), data={"grant_type": "client_credentials"},
                        headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        token = r.json().get("access_token")
        if not token:
            raise RedditError(f"no access token: {r.text[:200]}")
        self.s.headers["Authorization"] = f"bearer {token}"

    def get(self, path: str, **params):
        r = self.s.get(f"{API}{path}", params={"raw_json": 1, **params})
        r.raise_for_status()
        return r.json()

    def search(self, sub: str, query: str, pages: int = 3):
        after = None
        for _ in range(pages):
            data = self.get(f"/r/{sub}/search", q=query, restrict_sr=1, sort="top", t="all", limit=100, after=after)["data"]
            yield from (c["data"] for c in data["children"])
            after = data.get("after")
            if not after:
                return

    def top_comments(self, post_id: str) -> list[dict]:
        listing = self.get(f"/comments/{post_id}", limit=500, depth=1, sort="top")
        return [c["data"] for c in listing[1]["data"]["children"] if c["kind"] == "t1"]


def sync(conn, client: Reddit | None = None, thread_limit: int | None = None) -> int:
    if client is None:
        cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if not (cid and secret):
            raise RedditError("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to collect from Reddit")
        client = Reddit(cid, secret)
    n, threads = 0, {}
    for sub in SUBREDDITS:
        for q in QUERIES:
            try:
                posts = list(client.search(sub, q))
            except Exception as e:  # a private or banned subreddit should not stop the run
                log.warning("r/%s '%s' failed: %s", sub, q, e)
                continue
            rows = [post_row(p) for p in posts]
            n += db.upsert_candidates(conn, [r for r in rows if r["revenue_monthly"] or r["domain"]])
            for p in posts:
                if (p.get("num_comments") or 0) >= MIN_THREAD_COMMENTS:
                    threads[p["id"]] = p
            conn.commit()
        log.info("r/%s: %d rows so far", sub, n)
    done = set((db.get_state(conn, "reddit_threads_done") or "").split(",")) - {""}
    ordered = sorted(threads.values(), key=lambda p: -(p.get("num_comments") or 0))
    for p in ordered[:thread_limit] if thread_limit else ordered:
        if p["id"] in done:
            continue
        rows = [r for c in client.top_comments(p["id"]) if (r := comment_row(c, p))]
        n += db.upsert_candidates(conn, rows)
        done.add(p["id"])
        db.set_state(conn, "reddit_threads_done", ",".join(sorted(done)))
        conn.commit()
    return n
