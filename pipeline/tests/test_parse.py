"""Parser tests for both PSI formats. Fixture lines are invented but follow
the Valuer General format fact sheets exactly (May 2020 editions)."""

import io
import zipfile
from datetime import date

from psi.parse import parse_dat_lines, iter_dat_members

TODAY = date(2026, 7, 16)
INGESTED = "2026-07-16T00:00:00+00:00"

# Current format (2001-now): B fields per fact sheet, C record carries the
# legal description; two C records concatenate without a separator.
CURRENT_B = (
    "B;001;12345;1;20260710 01:05;OAKDENE;2;42A;MACQUARIE ST;Sydney;2000;"
    "250.5;M;20260615;20260701;1500000;R2;R;;;;;;AB123456;"
)
CURRENT_C1 = "C;001;12345;1;20260710 01:05;LOT 1 DP12345 SOME VERY LONG PLAN TEXT;"
CURRENT_C2 = "C;001;12345;1;20260710 01:05; CONTINUED;"

# Archived format (1990-2001): source tag in field 2, DD/MM/YYYY dates.
ARCHIVED_B = (
    "B;213;ARCHIVE;1287600000000;987654;1A;27A;BATHURST ST;Dubbo;2830;"
    "15/06/1995;120000;SEC B LOT 23 DP4748;1300;M;20.72 X 40.23;AA;A;;"
)


def run(lines):
    return list(parse_dat_lines(lines, "test.zip/test.dat", INGESTED, TODAY))


def test_current_b_field_mapping():
    [row] = run([CURRENT_B])
    assert row["sale_key"] == "C:001:12345:1"
    assert row["district_code"] == "001"
    assert row["property_id"] == "12345"
    assert row["sale_counter"] == "1"
    assert row["download_datetime"] == "20260710 01:05"
    assert row["property_name"] == "OAKDENE"
    assert row["unit"] == "2"
    assert row["house_number"] == "42A"
    assert row["street"] == "MACQUARIE ST"
    assert row["locality"] == "SYDNEY"  # uppercased on ingest
    assert row["postcode"] == "2000"
    assert row["area_sqm"] == 250.5
    assert row["contract_date"] == "2026-06-15"
    assert row["settlement_date"] == "2026-07-01"
    assert row["price"] == 1500000
    assert row["zoning"] == "R2"
    assert row["nature_of_property"] == "R"
    assert row["primary_purpose"] is None
    assert row["strata_lot"] is None
    assert row["dealing_number"] == "AB123456"
    assert row["source"] == "current"
    assert row["source_file"] == "test.zip/test.dat"


def test_c_records_join_and_concatenate():
    [row] = run([CURRENT_C1, CURRENT_B, CURRENT_C2])  # C before or after B
    assert row["legal_description"] == "LOT 1 DP12345 SOME VERY LONG PLAN TEXTCONTINUED"


def test_hectares_converted_to_sqm():
    line = CURRENT_B.replace(";250.5;M;", ";1.3;H;")
    [row] = run([line])
    assert row["area_sqm"] == 13000.0


def test_blank_price_kept_as_null():
    line = CURRENT_B.replace(";1500000;", ";;")
    [row] = run([line])
    assert row["price"] is None


def test_zero_price_normalized_to_null():
    line = CURRENT_B.replace(";1500000;", ";0;")
    [row] = run([line])
    assert row["price"] is None


def test_contract_date_bounds():
    too_old = CURRENT_B.replace(";20260615;", ";19891231;")
    future = CURRENT_B.replace(";20260615;", ";20270101;")
    unparseable = CURRENT_B.replace(";20260615;", ";NOTADATE;")
    boundary = CURRENT_B.replace(";20260615;", ";19900101;")
    assert run([too_old]) == []
    assert run([future]) == []
    assert run([unparseable]) == []
    assert run([boundary])[0]["contract_date"] == "1990-01-01"


def test_non_b_records_ignored():
    assert run([
        "A;RTSALEDATA;001;20260710 01:05;",
        "D;001;12345;1;20260710 01:05;P;",
        "Z;10;5;3;1;",
        "",
    ]) == []


def test_archived_b_field_mapping():
    [row] = run([ARCHIVED_B])
    assert row["sale_key"].startswith("A:")
    assert len(row["sale_key"]) == 2 + 40  # 'A:' + sha1 hex
    assert row["district_code"] == "213"
    assert row["property_id"] is None
    assert row["sale_counter"] is None
    assert row["download_datetime"] is None
    assert row["unit"] == "1A"
    assert row["house_number"] == "27A"
    assert row["street"] == "BATHURST ST"
    assert row["locality"] == "DUBBO"
    assert row["postcode"] == "2830"
    assert row["contract_date"] == "1995-06-15"
    assert row["settlement_date"] is None
    assert row["price"] == 120000
    assert row["legal_description"] == "SEC B LOT 23 DP4748"
    assert row["area_sqm"] == 1300.0
    assert row["zoning"] == "A"
    assert row["source"] == "archived"


def test_archived_accepts_ccyymmdd_date_too():
    line = ARCHIVED_B.replace(";15/06/1995;", ";19950615;")
    [row] = run([line])
    assert row["contract_date"] == "1995-06-15"


def test_archived_key_is_deterministic_and_content_sensitive():
    [a] = run([ARCHIVED_B])
    [b] = run([ARCHIVED_B])
    assert a["sale_key"] == b["sale_key"]
    [c] = run([ARCHIVED_B.replace(";120000;", ";120001;")])
    assert c["sale_key"] != a["sale_key"]


def test_nested_zip_extraction(tmp_path):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("week.dat", CURRENT_B + "\n")
    outer_path = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer_path, "w") as zf:
        zf.writestr("nested.zip", inner.getvalue())
        zf.writestr("top.dat", ARCHIVED_B + "\n")
    members = dict(iter_dat_members(outer_path))
    assert "outer.zip/nested.zip/week.dat" in members
    assert "outer.zip/top.dat" in members
    assert members["outer.zip/nested.zip/week.dat"] == [CURRENT_B]


def test_encoding_errors_tolerated(tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("bad.dat", (CURRENT_B + "\n").encode("utf-8") + b"\xff\xfe garbage\n")
    [(_, lines)] = list(iter_dat_members(path))
    assert lines[0] == CURRENT_B  # good line survives a bad byte later in file
