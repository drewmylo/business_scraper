import json
from types import SimpleNamespace

from bizscraper import classify, db, markets


def seeded():
    conn = db.connect(":memory:")
    classify.ensure_schema(conn)
    db.upsert_candidates(conn, [{"source": "x", "source_id": str(i)} for i in range(1, 5)])
    rows = [(1, "dog grooming booking app", 1, 3000), (2, "booking software for dog groomers", 0, None),
            (3, "mobile car detailing", 1, 8000), (4, "unspecified saas", 0, None)]
    for cid, cat, own, rev in rows:
        conn.execute("INSERT INTO classifications VALUES (?, 'm', 't', 1, ?, '', 'B2B', 'S', ?, '', 1, ?, ?, 'US')",
                     (cid, f"Biz{cid}", cat, own, rev))
    conn.commit()
    return conn


class FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        cats = json.loads(kw["messages"][0]["content"].split("Categories:\n", 1)[1])
        table = {"dog grooming booking app": ("booking software for pet services", "software"),
                 "booking software for dog groomers": ("booking software for pet services", "software"),
                 "mobile car detailing": ("mobile car detailing", "service"),
                 "unspecified saas": ("unclear", "unclear")}
        items = [{"category": c, "market": table[c][0], "market_type": table[c][1]} for c in cats]
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps({"items": items}))])


def test_maps_and_ranks_markets():
    conn = seeded()
    msgs = FakeMessages()
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs))
    assert markets.run(conn, client=client, model="t") == 4
    assert markets.unmapped_categories(conn) == []
    assert markets.run(conn, client=client, model="t") == 0
    rows = markets.report(conn, min_items=1)
    assert [r["market"] for r in rows] == ["mobile car detailing", "booking software for pet services"]
    pet = rows[1]
    assert pet["businesses"] == 2 and pet["with_revenue"] == 1 and pet["examples"].startswith("Biz1")
    assert [r["market"] for r in markets.report(conn, min_items=1, market_type="service")] == ["mobile car detailing"]
