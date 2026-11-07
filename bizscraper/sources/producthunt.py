"""Product Hunt via its GraphQL API v2. Needs a free developer token in PRODUCTHUNT_TOKEN.

Create one at https://www.producthunt.com/v2/oauth/applications ("Developer token").
The API is free for non-commercial use; ask Product Hunt before using the data commercially.
"""
from __future__ import annotations

import logging
import os
import time

from .. import db
from ..http import PoliteSession
from ..text import domain_of

log = logging.getLogger(__name__)
API = "https://api.producthunt.com/v2/api/graphql"

QUERY = """
query Posts($after: String, $postedAfter: DateTime) {
  posts(order: NEWEST, first: 20, after: $after, postedAfter: $postedAfter) {
    pageInfo { endCursor hasNextPage }
    edges { node {
      id name tagline description url website votesCount commentsCount createdAt
      topics(first: 8) { edges { node { name } } }
    } }
  }
}
"""


class ProductHuntError(RuntimeError):
    pass


def post_row(node: dict) -> dict:
    topics = [e["node"]["name"] for e in (node.get("topics") or {}).get("edges", [])]
    # `website` is a Product Hunt redirect; keep it, the real domain is resolved later.
    return {
        "source": "producthunt",
        "source_id": node["id"],
        "name": node.get("name"),
        "url": node.get("website") or node.get("url"),
        "domain": domain_of(node.get("website")),
        "title": node.get("tagline"),
        "description": node.get("description"),
        "posted_at": node.get("createdAt"),
        "popularity": node.get("votesCount"),
        "comments": node.get("commentsCount"),
        "tags": topics,
        "raw": node,
    }


def sync(conn, token: str | None = None, posted_after: str | None = None, max_pages: int | None = None,
         session: PoliteSession | None = None) -> int:
    token = token or os.environ.get("PRODUCTHUNT_TOKEN")
    if not token:
        raise ProductHuntError("Set PRODUCTHUNT_TOKEN (free developer token) to collect Product Hunt posts")
    s = session or PoliteSession(min_interval=1.0)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    posted_after = posted_after or db.get_state(conn, "ph_posted_after") or "2020-01-01T00:00:00Z"
    after = db.get_state(conn, "ph_cursor")
    n, pages, newest = 0, 0, posted_after
    while True:
        r = s.post(API, json={"query": QUERY, "variables": {"after": after, "postedAfter": posted_after}},
                   headers=headers)
        if r.status_code == 429 or (r.ok and "rate" in str(r.json().get("errors", "")).lower()):
            reset = int(r.headers.get("X-Rate-Limit-Reset", "60"))
            log.info("Product Hunt rate limit, sleeping %ss", reset)
            time.sleep(min(reset, 900))
            continue
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise ProductHuntError(str(body["errors"]))
        posts = body["data"]["posts"]
        rows = [post_row(e["node"]) for e in posts["edges"]]
        n += db.upsert_candidates(conn, rows)
        newest = max([newest, *(row["posted_at"] for row in rows if row["posted_at"])])
        after = posts["pageInfo"]["endCursor"]
        db.set_state(conn, "ph_cursor", after or "")
        conn.commit()
        pages += 1
        if not posts["pageInfo"]["hasNextPage"] or (max_pages and pages >= max_pages):
            break
    if not posts["pageInfo"]["hasNextPage"]:
        # Finished a full pass: next run starts from the newest post seen, with a fresh cursor.
        db.set_state(conn, "ph_posted_after", newest)
        db.set_state(conn, "ph_cursor", "")
        conn.commit()
    return n
