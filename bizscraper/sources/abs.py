"""Australian Bureau of Statistics Data API (free, no key): industry size by ANZSIC division/subdivision.

Uses the Australian Industry dataflow (cat. 8155): employment, sales income, profit and more per
industry, which sizes the AU customer pool for a B2B category. Business counts by industry (CABEE,
cat. 8165) are not in the Data API; they are published as spreadsheets on abs.gov.au.
"""
from __future__ import annotations

import csv
import io

from .. import db
from ..http import PoliteSession

API = "https://data.api.abs.gov.au/rest/data"
DATAFLOW = "ABS,AUSTRALIAN_INDUSTRY,1.1.0"
MEASURES = ["EMPTOTAL", "INCSALGDSSERV", "OPBT", "INDUSTRY"]


def _split(label: str) -> tuple[str, str]:
    code, _, name = (label or "").partition(": ")
    return code, name


def parse_csv(text: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        measure, measure_name = _split(r.get("MEASURE: Measure", ""))
        industry, industry_name = _split(r.get("INDUSTRY: Industry", ""))
        region, _ = _split(r.get("REGION: Region", ""))
        if region != "AUS" or not r.get("OBS_VALUE"):
            continue
        rows.append({
            "measure": measure, "measure_name": measure_name, "industry": industry,
            "industry_name": industry_name, "period": r.get("TIME_PERIOD: Time Period"),
            "value": float(r["OBS_VALUE"]), "unit": _split(r.get("UNIT_MEASURE: Unit of Measure", ""))[1],
            "multiplier": _split(r.get("UNIT_MULT: Unit of Multiplier", ""))[1],
        })
    return rows


def sync(conn, start_period: str = "2015", session: PoliteSession | None = None) -> int:
    s = session or PoliteSession()
    key = "+".join(MEASURES) + "...."
    r = s.get(f"{API}/{DATAFLOW}/{key}", params={"startPeriod": start_period},
              headers={"Accept": "application/vnd.sdmx.data+csv;labels=both"})
    r.raise_for_status()
    rows = parse_csv(r.text)
    ts = db.now()
    conn.executemany(
        "INSERT INTO au_industry(measure, measure_name, industry, industry_name, period, value, unit, multiplier, fetched_at) "
        "VALUES (:measure, :measure_name, :industry, :industry_name, :period, :value, :unit, :multiplier, :ts) "
        "ON CONFLICT(measure, industry, period) DO UPDATE SET value = excluded.value, fetched_at = excluded.fetched_at",
        [{**row, "ts": ts} for row in rows],
    )
    conn.commit()
    return len(rows)
