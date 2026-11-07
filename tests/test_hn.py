from bizscraper import db
from bizscraper.sources import hn


def hit(i, ts, title="Show HN: Widget – invoices for plumbers", **kw):
    return {"objectID": str(i), "created_at_i": ts, "title": title, "url": f"https://w{i}.com",
            "points": 10, "num_comments": 2, "_tags": ["story", "show_hn", f"author_a{i}"], **kw}


class FakeHN(hn.HackerNews):
    def __init__(self, hits):
        self.hits = hits
        self.calls = 0

    def _get(self, path, **params):
        self.calls += 1
        assert path == "search_by_date"
        lo, hi = 0, None
        for f in params["numericFilters"].split(","):
            if ">" in f:
                lo = int(f.split(">")[1])
            else:
                hi = int(f.split("<")[1])
        sel = [h for h in self.hits if h["created_at_i"] > lo and (hi is None or h["created_at_i"] < hi)]
        sel.sort(key=lambda h: -h["created_at_i"])
        return {"hits": sel[: params["hitsPerPage"]]}


def test_by_date_pages_past_cap(monkeypatch):
    monkeypatch.setattr(hn, "PAGE", 3)
    hits = [hit(i, 100 + i // 2) for i in range(10)]  # pairs share a timestamp
    client = FakeHN(hits)
    got = {h["objectID"] for h in client.by_date("show_hn")}
    assert got == {h["objectID"] for h in hits}
    assert client.calls < 20


def test_story_row_extracts_name_and_revenue():
    row = hn.story_row(hit(1, 1_700_000_000, story_text="We are at $4k MRR"), "hn_show")
    assert row["name"] == "Widget"
    assert row["domain"] == "w1.com"
    assert row["revenue_monthly"] == 4000
    assert row["tags"] == ["story", "show_hn"]


def test_sync_show_hn_is_incremental(monkeypatch):
    monkeypatch.setattr(hn, "PAGE", 3)
    conn = db.connect(":memory:")
    client = FakeHN([hit(i, 100 + i) for i in range(5)])
    assert hn.sync_show_hn(conn, client) == 5
    client.hits.append(hit(99, 500))
    assert hn.sync_show_hn(conn, client) == 1
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6


def test_comment_row_needs_url_or_revenue():
    thread = {"id": 1, "title": "Ask HN: side projects making money?"}
    c = {"id": 5, "created_at_i": 1, "text": 'I run <a href="https://shoptool.io">ShopTool</a>, $2k/month'}
    row = hn.comment_row(c, thread)
    assert row["domain"] == "shoptool.io" and row["revenue_monthly"] == 2000
    assert hn.comment_row({"id": 6, "text": "Nice thread!"}, thread) is None
