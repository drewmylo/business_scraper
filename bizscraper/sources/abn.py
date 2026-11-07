"""ABN Lookup name search (free). Needs a GUID in ABN_LOOKUP_GUID.

Register at https://abr.business.gov.au/Tools/WebServicesAgreement. Use it to check whether an
Australian business already trades under a name, e.g. a local clone of an overseas product.
"""
from __future__ import annotations

import json
import os
import re

from .. import db
from ..http import PoliteSession

API = "https://abr.business.gov.au/json/MatchingNames.aspx"
_JSONP = re.compile(r"^\s*\w+\((.*)\)\s*;?\s*$", re.DOTALL)


class AbnLookupError(RuntimeError):
    pass


def parse_jsonp(text: str) -> dict:
    m = _JSONP.match(text)
    return json.loads(m.group(1) if m else text)


def search(conn, name: str, max_results: int = 20, guid: str | None = None,
           session: PoliteSession | None = None) -> list[dict]:
    guid = guid or os.environ.get("ABN_LOOKUP_GUID")
    if not guid:
        raise AbnLookupError("Set ABN_LOOKUP_GUID (free from abr.business.gov.au) to search ABN Lookup")
    s = session or PoliteSession()
    r = s.get(API, params={"name": name, "maxResults": max_results, "guid": guid})
    r.raise_for_status()
    body = parse_jsonp(r.text)
    if body.get("Message"):
        raise AbnLookupError(body["Message"])
    ts = db.now()
    rows = [{
        "query": name, "abn": n.get("Abn"), "name": n.get("Name"), "name_type": n.get("NameType"),
        "state": n.get("State"), "postcode": n.get("Postcode"), "score": n.get("Score"),
        "is_current": 1 if n.get("IsCurrent") else 0, "ts": ts,
    } for n in body.get("Names", [])]
    conn.executemany(
        "INSERT OR REPLACE INTO au_entities(query, abn, name, name_type, state, postcode, score, is_current, fetched_at) "
        "VALUES (:query, :abn, :name, :name_type, :state, :postcode, :score, :is_current, :ts)", rows)
    conn.commit()
    return rows
