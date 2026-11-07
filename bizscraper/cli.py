"""Command line entry point: `bizscraper sync all`, `bizscraper stats`, `bizscraper export`."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import traceback
from pathlib import Path

from . import db
from .sources import abn, abs as abs_source, hn, producthunt

log = logging.getLogger("bizscraper")


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


SOURCES = {
    "hn-show": lambda conn, a: hn.sync_show_hn(conn, limit=a.limit),
    "hn-revenue": lambda conn, a: hn.sync_revenue(conn, thread_limit=a.limit),
    "producthunt": lambda conn, a: producthunt.sync(conn, max_pages=a.limit),
    "abs": lambda conn, a: abs_source.sync(conn),
}
NEEDS_KEY = {"producthunt": "PRODUCTHUNT_TOKEN"}


def cmd_sync(conn, args) -> int:
    names = list(SOURCES) if args.source == "all" else [args.source]
    failed = 0
    for name in names:
        key = NEEDS_KEY.get(name)
        if key and not os.environ.get(key):
            log.warning("skipping %s: set %s", name, key)
            continue
        run = db.start_run(conn, name)
        try:
            n = SOURCES[name](conn, args)
            db.finish_run(conn, run, n)
            log.info("%s: %d rows", name, n)
        except Exception as e:  # keep going with the other sources
            conn.rollback()
            db.finish_run(conn, run, 0, f"{type(e).__name__}: {e}")
            log.error("%s failed: %s", name, e)
            log.debug(traceback.format_exc())
            failed += 1
    return 1 if failed else 0


def cmd_abn(conn, args) -> int:
    for row in abn.search(conn, args.name, max_results=args.max):
        print(f"{row['abn']}  {row['state'] or '--':3} {row['score'] or 0:>3}  {row['name']}")
    return 0


def cmd_stats(conn, args) -> int:
    print("Candidates by source:")
    for r in conn.execute(
        "SELECT source, COUNT(*) n, COUNT(revenue_monthly) with_rev, COUNT(DISTINCT domain) domains "
        "FROM candidates GROUP BY source ORDER BY n DESC"):
        print(f"  {r['source']:22} {r['n']:>8} rows  {r['with_rev']:>6} with revenue  {r['domains']:>7} domains")
    print("\nTop revenue figures found:")
    for r in conn.execute(
        "SELECT name, domain, revenue_monthly, revenue_currency, source FROM candidates "
        "WHERE revenue_monthly IS NOT NULL ORDER BY revenue_monthly DESC LIMIT ?", (args.top,)):
        print(f"  {r['revenue_monthly']:>11,.0f} {r['revenue_currency']}/mo  {(r['domain'] or '-')[:30]:30} {(r['name'] or '')[:60]}")
    print("\nLast runs:")
    for r in conn.execute("SELECT source, started_at, items, status, error FROM runs ORDER BY id DESC LIMIT 8"):
        print(f"  {r['started_at']}  {r['source']:12} {r['status']:6} {r['items'] or 0:>7}  {r['error'] or ''}")
    return 0


def cmd_reparse(conn, args) -> int:
    """Re-run revenue extraction over stored text, after the patterns change."""
    from .text import find_revenue
    n = 0
    rows = conn.execute("SELECT id, title, description FROM candidates").fetchall()
    for r in rows:
        rev = find_revenue(f"{r['title'] or ''}\n{r['description'] or ''}")
        conn.execute("UPDATE candidates SET revenue_monthly = ?, revenue_currency = ?, revenue_text = ? WHERE id = ?",
                     (rev.monthly if rev else None, rev.currency if rev else None, rev.text if rev else None, r["id"]))
        n += bool(rev)
    conn.commit()
    log.info("reparsed %d rows, %d with revenue", len(rows), n)
    return 0


def cmd_export(conn, args) -> int:
    where = "WHERE revenue_monthly IS NOT NULL" if args.revenue_only else ""
    cols = ["source", "source_id", "name", "domain", "url", "title", "posted_at", "popularity", "comments",
            "revenue_monthly", "revenue_currency", "revenue_text", "tags", "first_seen"]
    out = open(args.out, "w", newline="") if args.out != "-" else sys.stdout
    w = csv.writer(out)
    w.writerow(cols)
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM candidates {where} ORDER BY revenue_monthly DESC NULLS LAST, popularity DESC"):
        w.writerow([r[c] for c in cols])
    if out is not sys.stdout:
        out.close()
        log.info("wrote %s", args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="bizscraper", description=__doc__)
    p.add_argument("--db", help="SQLite path (default data/bizscraper.db or $BIZSCRAPER_DB)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="collect from a source")
    s.add_argument("source", choices=[*SOURCES, "all"])
    s.add_argument("--limit", type=int, help="cap rows/pages/threads, for a quick trial run")
    s.set_defaults(fn=cmd_sync)

    a = sub.add_parser("abn", help="search ABN Lookup for Australian businesses by name")
    a.add_argument("name")
    a.add_argument("--max", type=int, default=20)
    a.set_defaults(fn=cmd_abn)

    st = sub.add_parser("stats", help="summarise what has been collected")
    st.add_argument("--top", type=int, default=25)
    st.set_defaults(fn=cmd_stats)

    rp = sub.add_parser("reparse", help="re-run revenue extraction on stored rows")
    rp.set_defaults(fn=cmd_reparse)

    e = sub.add_parser("export", help="export candidates to CSV")
    e.add_argument("--out", default="data/candidates.csv")
    e.add_argument("--revenue-only", action="store_true")
    e.set_defaults(fn=cmd_export)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    conn = db.connect(args.db)
    return args.fn(conn, args)


if __name__ == "__main__":
    sys.exit(main())
