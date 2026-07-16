"""One-time backfill: download all PSI files, build data/nsw_property.db.

Idempotent: cached downloads are skipped, upserts follow the dedup rules
(current rows: newest download_datetime wins; archived rows: first write
wins), and locality_monthly is recomputed from scratch each run.
"""

import logging
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from psi import download_all, parse_zip

log = logging.getLogger("backfill")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "nsw_property.db"
QA_REPORT_PATH = ROOT / "qa_report.md"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sales (
  sale_key          TEXT PRIMARY KEY,
  district_code     TEXT,
  property_id       TEXT,
  sale_counter      TEXT,
  download_datetime TEXT,
  property_name     TEXT,
  unit              TEXT,
  house_number      TEXT,
  street            TEXT,
  locality          TEXT,
  postcode          TEXT,
  area_sqm          REAL,
  contract_date     TEXT,
  settlement_date   TEXT,
  price             INTEGER,
  zoning            TEXT,
  nature_of_property TEXT,
  primary_purpose   TEXT,
  strata_lot        TEXT,
  dealing_number    TEXT,
  legal_description TEXT,
  source            TEXT CHECK (source IN ('current','archived')),
  source_file       TEXT,
  ingested_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_postcode_date ON sales(postcode, contract_date);
CREATE INDEX IF NOT EXISTS idx_sales_locality_date ON sales(locality, contract_date);
CREATE INDEX IF NOT EXISTS idx_sales_contract_date ON sales(contract_date);

CREATE TABLE IF NOT EXISTS locality_monthly (
  locality     TEXT,
  postcode     TEXT,
  month        TEXT,
  n_sales      INTEGER,
  median_price INTEGER,
  mean_price   INTEGER,
  min_price    INTEGER,
  max_price    INTEGER,
  PRIMARY KEY (locality, postcode, month)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

COLUMNS = [
    "sale_key", "district_code", "property_id", "sale_counter",
    "download_datetime", "property_name", "unit", "house_number", "street",
    "locality", "postcode", "area_sqm", "contract_date", "settlement_date",
    "price", "zoning", "nature_of_property", "primary_purpose", "strata_lot",
    "dealing_number", "legal_description", "source", "source_file",
    "ingested_at",
]
_PLACEHOLDERS = ",".join(f":{c}" for c in COLUMNS)

# Current rows: newest download_datetime wins (string compare is safe:
# format is 'CCYYMMDD HH24:MI').
UPSERT_CURRENT = f"""
INSERT INTO sales ({",".join(COLUMNS)}) VALUES ({_PLACEHOLDERS})
ON CONFLICT(sale_key) DO UPDATE SET
  {",".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "sale_key")}
WHERE excluded.download_datetime > sales.download_datetime
"""

# Archived rows: yearly archives are static — first write wins.
INSERT_ARCHIVED = f"""
INSERT OR IGNORE INTO sales ({",".join(COLUMNS)}) VALUES ({_PLACEHOLDERS})
"""

BATCH_SIZE = 5000

# The archived data (1990-2001) and current data (2001-now) overlap in early
# 2001: the same sale can appear under an A: key and a C: key. Delete archived
# rows exactly matched by a current row on full address + date + price.
# Runs after ingest so re-runs converge to the same counts.
DEDUPE_CROSS_FORMAT = """
DELETE FROM sales WHERE source = 'archived' AND contract_date >= '2001-01-01'
AND EXISTS (
  SELECT 1 FROM sales c WHERE c.source = 'current'
    AND c.contract_date = sales.contract_date
    AND coalesce(c.price, -1) = coalesce(sales.price, -1)
    AND coalesce(c.unit, '') = coalesce(sales.unit, '')
    AND coalesce(c.house_number, '') = coalesce(sales.house_number, '')
    AND coalesce(c.street, '') = coalesce(sales.street, '')
    AND coalesce(c.locality, '') = coalesce(sales.locality, '')
)
"""


def ingest_file(conn: sqlite3.Connection, zip_path: Path, ingested_at: str) -> int:
    n = 0
    current_batch: list[dict] = []
    archived_batch: list[dict] = []
    for row in parse_zip(zip_path, ingested_at):
        (current_batch if row["source"] == "current" else archived_batch).append(row)
        n += 1
        if len(current_batch) >= BATCH_SIZE:
            conn.executemany(UPSERT_CURRENT, current_batch)
            current_batch.clear()
        if len(archived_batch) >= BATCH_SIZE:
            conn.executemany(INSERT_ARCHIVED, archived_batch)
            archived_batch.clear()
    if current_batch:
        conn.executemany(UPSERT_CURRENT, current_batch)
    if archived_batch:
        conn.executemany(INSERT_ARCHIVED, archived_batch)
    conn.commit()
    return n


def rebuild_locality_monthly(conn: sqlite3.Connection) -> int:
    """Full recompute. Median has no SQLite builtin — computed in Python by
    streaming priced sales grouped by (locality, postcode, month)."""
    conn.execute("DELETE FROM locality_monthly")
    cur = conn.execute(
        """SELECT locality, postcode, substr(contract_date, 1, 7) AS month, price
           FROM sales
           WHERE price IS NOT NULL AND locality IS NOT NULL
           ORDER BY locality, postcode, month"""
    )
    inserted = 0
    group_key = None
    prices: list[int] = []

    def flush():
        nonlocal inserted
        if group_key is None or not prices:
            return
        conn.execute(
            "INSERT INTO locality_monthly VALUES (?,?,?,?,?,?,?,?)",
            (
                group_key[0], group_key[1], group_key[2], len(prices),
                round(statistics.median(prices)),
                round(statistics.fmean(prices)),
                min(prices), max(prices),
            ),
        )
        inserted += 1

    for locality, postcode, month, price in cur:
        key = (locality, postcode, month)
        if key != group_key:
            flush()
            group_key, prices = key, []
        prices.append(price)
    flush()
    conn.commit()
    return inserted


def write_meta(conn: sqlite3.Connection, last_weekly_file: str | None) -> None:
    row_count = conn.execute("SELECT count(*) FROM sales").fetchone()[0]
    now = datetime.now(timezone.utc).isoformat()
    for key, value in [
        ("schema_version", "1"),
        ("row_count", str(row_count)),
        ("last_weekly_file", last_weekly_file or ""),
        ("last_sync_at", now),
    ]:
        conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    conn.commit()


def write_qa_report(conn: sqlite3.Connection, missing_urls: list[str],
                    elapsed_s: float) -> None:
    q = lambda sql: conn.execute(sql).fetchall()
    total = q("SELECT count(*) FROM sales")[0][0]
    per_year = q("""SELECT substr(contract_date,1,4) AS y, count(*)
                    FROM sales GROUP BY y ORDER BY y""")
    null_price = q("SELECT count(*) FROM sales WHERE price IS NULL")[0][0]
    dmin, dmax = q("SELECT min(contract_date), max(contract_date) FROM sales")[0]
    dupes = q("""SELECT count(*) FROM
                 (SELECT sale_key FROM sales GROUP BY sale_key HAVING count(*) > 1)""")[0][0]
    top_localities = q("""SELECT locality, count(*) AS n FROM sales
                          GROUP BY locality ORDER BY n DESC LIMIT 10""")
    db_mb = DB_PATH.stat().st_size / 1_048_576

    lines = [
        "# Backfill QA report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()} "
        f"(elapsed {elapsed_s / 60:.1f} min)",
        "",
        f"- **Total rows:** {total:,}",
        f"- **DB file size:** {db_mb:,.0f} MB",
        f"- **NULL price:** {null_price:,} ({100 * null_price / total:.2f}%)",
        f"- **contract_date range:** {dmin} .. {dmax}",
        f"- **Duplicate sale_key count:** {dupes} (must be 0)",
        "",
        "## Rows per contract year",
        "",
        "| Year | Rows |",
        "|---|---|",
    ]
    lines += [f"| {y} | {n:,} |" for y, n in per_year]
    lines += ["", "## Top 10 localities by sale count", "", "| Locality | Rows |", "|---|---|"]
    lines += [f"| {loc} | {n:,} |" for loc, n in top_localities]
    if missing_urls:
        lines += ["", "## Files not published (404)", ""]
        lines += [f"- {u}" for u in missing_urls]
    QA_REPORT_PATH.write_text("\n".join(lines) + "\n")
    log.info("QA report written to %s", QA_REPORT_PATH)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    start = time.time()
    ingested_at = datetime.now(timezone.utc).isoformat()

    log.info("Downloading PSI files into %s", RAW_DIR)
    dl = download_all(RAW_DIR)
    yearly_missing = [u for u in dl["missing"] if "/yearly/" in u]
    if yearly_missing:
        log.error("Yearly archives returned 404 (investigate!): %s", yearly_missing)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA synchronous=OFF")  # local one-time build; crash = rerun

    for i, zip_path in enumerate(sorted(dl["files"]), 1):
        t = time.time()
        n = ingest_file(conn, zip_path, ingested_at)
        log.info("[%d/%d] %s: %s records parsed (%.0fs)",
                 i, len(dl["files"]), zip_path.name, f"{n:,}", time.time() - t)

    log.info("Dedupe: archived rows shadowed by current rows (2001 overlap) ...")
    n_deduped = conn.execute(DEDUPE_CROSS_FORMAT).rowcount
    conn.commit()
    log.info("Removed %s cross-format duplicates", f"{n_deduped:,}")

    log.info("Rebuilding locality_monthly ...")
    n_agg = rebuild_locality_monthly(conn)
    log.info("locality_monthly: %s rows", f"{n_agg:,}")

    weeklies = sorted(p.name for p in dl["files"] if len(p.stem) == 8)
    write_meta(conn, weeklies[-1] if weeklies else None)

    log.info("Checkpointing WAL ...")
    conn.execute("PRAGMA synchronous=FULL")
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log.info("journal_mode=%s", mode)

    write_qa_report(conn, dl["missing"], time.time() - start)
    conn.close()
    log.info("Done in %.1f min", (time.time() - start) / 60)


if __name__ == "__main__":
    main()
