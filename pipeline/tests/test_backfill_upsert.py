"""Dedup semantics: current rows newest-download-wins, archived first-write-wins."""

import sqlite3

import backfill


def make_row(**overrides):
    row = {c: None for c in backfill.COLUMNS}
    row.update(
        sale_key="C:001:12345:1",
        district_code="001",
        property_id="12345",
        sale_counter="1",
        download_datetime="20260701 01:05",
        price=1000000,
        contract_date="2026-06-15",
        source="current",
    )
    row.update(overrides)
    return row


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(backfill.SCHEMA)
    return conn


def test_newer_download_wins():
    conn = fresh_db()
    conn.execute(backfill.UPSERT_CURRENT, make_row())
    conn.execute(backfill.UPSERT_CURRENT,
                 make_row(download_datetime="20260708 01:05", price=1100000))
    assert conn.execute("SELECT price FROM sales").fetchall() == [(1100000,)]


def test_older_download_does_not_overwrite():
    conn = fresh_db()
    conn.execute(backfill.UPSERT_CURRENT, make_row())
    conn.execute(backfill.UPSERT_CURRENT,
                 make_row(download_datetime="20260601 01:05", price=900000))
    assert conn.execute("SELECT price FROM sales").fetchall() == [(1000000,)]


def test_archived_first_write_wins():
    conn = fresh_db()
    row = make_row(sale_key="A:" + "0" * 40, source="archived",
                   download_datetime=None, price=500000)
    conn.execute(backfill.INSERT_ARCHIVED, row)
    conn.execute(backfill.INSERT_ARCHIVED, {**row, "price": 999999})
    assert conn.execute("SELECT price FROM sales").fetchall() == [(500000,)]


def test_rerun_is_idempotent():
    conn = fresh_db()
    rows = [make_row(), make_row(sale_key="C:001:12345:2", sale_counter="2")]
    for _ in range(2):
        conn.executemany(backfill.UPSERT_CURRENT, rows)
    assert conn.execute("SELECT count(*) FROM sales").fetchone() == (2,)


def test_cross_format_dedupe_removes_exact_match_only():
    conn = fresh_db()
    current = make_row(contract_date="2001-03-10", price=250000,
                       unit=None, house_number="7", street="DATE ST",
                       locality="ADAMSTOWN", postcode="2289")
    shadowed = make_row(sale_key="A:" + "1" * 40, source="archived",
                        property_id=None, sale_counter=None,
                        download_datetime=None, contract_date="2001-03-10",
                        price=250000, unit=None, house_number="7",
                        street="DATE ST", locality="ADAMSTOWN")
    unique = {**shadowed, "sale_key": "A:" + "2" * 40, "house_number": "9"}
    pre_2001 = {**shadowed, "sale_key": "A:" + "3" * 40,
                "contract_date": "2000-03-10"}
    conn.execute(backfill.UPSERT_CURRENT, current)
    conn.executemany(backfill.INSERT_ARCHIVED, [shadowed, unique, pre_2001])
    conn.execute(backfill.DEDUPE_CROSS_FORMAT)
    keys = {k for (k,) in conn.execute("SELECT sale_key FROM sales")}
    assert keys == {current["sale_key"], unique["sale_key"], pre_2001["sale_key"]}


def test_locality_monthly_median():
    conn = fresh_db()
    for i, price in enumerate([100, 200, 400, None]):
        conn.execute(backfill.UPSERT_CURRENT, make_row(
            sale_key=f"C:001:1:{i}", sale_counter=str(i), price=price,
            locality="SYDNEY", postcode="2000", contract_date="2026-06-15"))
    n = backfill.rebuild_locality_monthly(conn)
    assert n == 1
    row = conn.execute("SELECT * FROM locality_monthly").fetchone()
    # (locality, postcode, month, n_sales, median, mean, min, max) — NULL price excluded
    assert row == ("SYDNEY", "2000", "2026-06", 3, 200, 233, 100, 400)
