"""SQLite storage. Every row keeps its raw payload so later classification can re-read it."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PATH = Path("data/bizscraper.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,           -- hn_show, hn_revenue, producthunt, ...
    source_id       TEXT NOT NULL,
    name            TEXT,
    url             TEXT,
    domain          TEXT,
    title           TEXT,
    description     TEXT,
    posted_at       TEXT,                    -- ISO 8601, UTC
    popularity      INTEGER,                 -- points / votes
    comments        INTEGER,
    revenue_monthly REAL,                    -- best revenue figure found, per month, in revenue_currency
    revenue_currency TEXT,
    revenue_text    TEXT,                    -- the phrase the figure came from
    tags            TEXT,                    -- JSON list
    raw             TEXT,                    -- JSON payload from the source
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_candidates_domain ON candidates(domain);
CREATE INDEX IF NOT EXISTS idx_candidates_revenue ON candidates(revenue_monthly);

CREATE TABLE IF NOT EXISTS au_industry (
    measure      TEXT NOT NULL,
    measure_name TEXT,
    industry     TEXT NOT NULL,
    industry_name TEXT,
    period       TEXT NOT NULL,
    value        REAL,
    unit         TEXT,
    multiplier   TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (measure, industry, period)
);

CREATE TABLE IF NOT EXISTS au_entities (
    query       TEXT NOT NULL,
    abn         TEXT NOT NULL,
    name        TEXT,
    name_type   TEXT,
    state       TEXT,
    postcode    TEXT,
    score       INTEGER,
    is_current  INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (query, abn, name)
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    items       INTEGER,
    status      TEXT,
    error       TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or os.environ.get("BIZSCRAPER_DB") or DEFAULT_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


CANDIDATE_FIELDS = (
    "source", "source_id", "name", "url", "domain", "title", "description", "posted_at",
    "popularity", "comments", "revenue_monthly", "revenue_currency", "revenue_text", "tags", "raw",
)


def upsert_candidates(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    ts = now()
    n = 0
    for row in rows:
        values = {f: row.get(f) for f in CANDIDATE_FIELDS}
        for f in ("tags", "raw"):
            if values[f] is not None and not isinstance(values[f], str):
                values[f] = json.dumps(values[f], ensure_ascii=False)
        cols = ", ".join(CANDIDATE_FIELDS)
        marks = ", ".join(f":{f}" for f in CANDIDATE_FIELDS)
        updates = ", ".join(f"{f} = excluded.{f}" for f in CANDIDATE_FIELDS[2:])
        conn.execute(
            f"INSERT INTO candidates ({cols}, first_seen, last_seen) VALUES ({marks}, :ts, :ts) "
            f"ON CONFLICT(source, source_id) DO UPDATE SET {updates}, last_seen = :ts",
            {**values, "ts": ts},
        )
        n += 1
    return n


def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute("INSERT INTO runs(source, started_at, status) VALUES (?, ?, 'running')", (source, now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, items: int, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, items = ?, status = ?, error = ? WHERE id = ?",
        (now(), items, "error" if error else "ok", error, run_id),
    )
    conn.commit()
