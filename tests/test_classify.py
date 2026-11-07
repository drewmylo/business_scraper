import json
from types import SimpleNamespace

from bizscraper import classify, db


def make_conn():
    conn = db.connect(":memory:")
    db.upsert_candidates(conn, [
        {"source": "hn_revenue_comment", "source_id": "1", "name": "I run GroomBook", "domain": "groombook.com",
         "description": "Booking for dog groomers, $4k MRR", "revenue_monthly": 4000, "revenue_currency": "USD",
         "revenue_text": "$4k MRR"},
        {"source": "hn_show", "source_id": "2", "name": "Tiny game", "popularity": 5},
        {"source": "hn_show", "source_id": "3", "name": "PawSlots", "popularity": 300},
    ])
    conn.commit()
    return conn


def item(i, **kw):
    base = {"id": i, "is_business": True, "business_name": f"B{i}", "one_liner": "x", "buyer": "B2B",
            "customer_industry": "S", "category": "Booking Software For Dog Groomers", "search_phrase": "dog grooming software",
            "software_leveraged": True, "revenue_is_own": False, "revenue_monthly_usd": None, "country": "US"}
    return {**base, **kw}


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        ids = [r["id"] for r in json.loads(kwargs["messages"][0]["content"].split("Items:\n", 1)[1])]
        items = [item(i, revenue_is_own=(i == 1), revenue_monthly_usd=4000 if i == 1 else None) for i in ids]
        items.append(item(999))  # an id that was not sent must be ignored
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps({"items": items}))],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0),
        )


def fake_classifier():
    msgs = FakeMessages()
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    return classify.Classifier(client=client, model="test-model"), msgs


def test_selects_revenue_and_popular_rows_only():
    conn = make_conn()
    classify.ensure_schema(conn)
    names = [r["name"] for r in classify.select_rows(conn, min_points=200)]
    assert names == ["I run GroomBook", "PawSlots"]


def test_run_saves_and_is_resumable():
    conn = make_conn()
    clf, msgs = fake_classifier()
    assert classify.run(conn, classifier=clf) == 2
    req = msgs.calls[0]
    assert req["fallbacks"] == "default" and req["output_config"]["format"]["type"] == "json_schema"
    assert classify.run(conn, classifier=clf) == 0  # nothing left
    assert conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2
    report = classify.categories_report(conn)
    assert report[0]["category"] == "booking software for dog groomers"
    assert report[0]["businesses"] == 2 and report[0]["with_revenue"] == 1


def test_refusal_skips_batch():
    conn = make_conn()
    clf, msgs = fake_classifier()
    msgs.create = lambda **kw: SimpleNamespace(stop_reason="refusal", content=[],
                                               usage=SimpleNamespace(input_tokens=1, output_tokens=0, cache_read_input_tokens=0))
    assert classify.run(conn, classifier=clf) == 0


def test_time_budget_stops_starting_batches():
    conn = make_conn()
    clf, msgs = fake_classifier()
    assert classify.run(conn, classifier=clf, max_minutes=0) == 0
    assert msgs.calls == []


def test_snapshot_copies_database(tmp_path):
    from bizscraper import cli
    src = tmp_path / "a.db"
    conn = db.connect(src)
    db.upsert_candidates(conn, [{"source": "x", "source_id": "1"}])
    conn.commit()
    out = tmp_path / "snap.db"
    assert cli.main(["--db", str(src), "snapshot", str(out)]) == 0
    import sqlite3
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
