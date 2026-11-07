from bizscraper import db
from bizscraper.sources import abn, abs as abs_source, producthunt

ABS_CSV = """DATAFLOW,MEASURE: Measure,INDUSTRY: Industry,BASIS: Basis,REGION: Region,FREQ: Frequency,TIME_PERIOD: Time Period,OBS_VALUE,UNIT_MEASURE: Unit of Measure,UNIT_MULT: Unit of Multiplier,REPYEAREND: Reporting Year End Day,OBS_STATUS: Observation Status,OBS_COMMENT: Observation Comment
ABS:AUSTRALIAN_INDUSTRY(1.1.0),EMPTOTAL: Employment at end of June,E: Construction,1: Original,AUS: Australia,A: Annual,2024,1300.5,NUM: Number,3: Thousands,30-6: Year Ending 30 June,,
ABS:AUSTRALIAN_INDUSTRY(1.1.0),EMPTOTAL: Employment at end of June,E: Construction,1: Original,NSW: New South Wales,A: Annual,2024,400,NUM: Number,3: Thousands,30-6: Year Ending 30 June,,
"""


def test_abs_parse_keeps_national_rows():
    rows = abs_source.parse_csv(ABS_CSV)
    assert len(rows) == 1
    assert rows[0]["industry"] == "E" and rows[0]["industry_name"] == "Construction"
    assert rows[0]["value"] == 1300.5 and rows[0]["multiplier"] == "Thousands"


def test_abn_parses_jsonp():
    body = abn.parse_jsonp('callback({"Message":"","Names":[{"Abn":"123","Name":"Acme"}]})')
    assert body["Names"][0]["Name"] == "Acme"


def test_producthunt_post_row():
    node = {"id": "9", "name": "Tool", "tagline": "Does a job", "website": "https://www.producthunt.com/r/x",
            "votesCount": 50, "commentsCount": 3, "createdAt": "2026-01-01T00:00:00Z",
            "topics": {"edges": [{"node": {"name": "SaaS"}}]}}
    row = producthunt.post_row(node)
    assert row["tags"] == ["SaaS"] and row["popularity"] == 50
    conn = db.connect(":memory:")
    assert db.upsert_candidates(conn, [row]) == 1


def test_reddit_rows_and_sync():
    from bizscraper.sources import reddit

    post = {"id": "abc", "name": "t3_abc", "subreddit": "sweatystartup", "title": "My pressure washing business",
            "selftext": "Year two, we do $18k/month in revenue", "is_self": True, "score": 400,
            "num_comments": 120, "created_utc": 1_700_000_000}
    comment = {"name": "t1_x", "body": "Mobile detailing here, about $6,000 a month", "body_html": "", "score": 50,
               "created_utc": 1_700_000_100}

    class FakeReddit:
        def search(self, sub, q, pages=3):
            return [post] if sub == "sweatystartup" and q == "revenue" else []

        def top_comments(self, post_id):
            return [comment, {"name": "t1_y", "body": "congrats!", "body_html": ""}]

    row = reddit.post_row(post)
    assert row["revenue_monthly"] == 18000 and row["tags"] == ["r/sweatystartup"]
    conn = db.connect(":memory:")
    assert reddit.sync(conn, client=FakeReddit()) == 2
    assert reddit.sync(conn, client=FakeReddit()) == 1  # thread already done, post re-upserted
