"""Parse PSI .DAT files (both formats) into normalized sales dicts.

Field positions verified against the Valuer General format fact sheets
(May 2020): "Current Property Sales Data File (Format 2001 - current)" and
"Archived Property Sales Data File Format (1990 to 2001)".

Current-format B record (semicolon-split, 0-indexed):
  1 district, 2 property_id, 3 sale_counter, 4 download_datetime,
  5 property_name, 6 unit, 7 house_number, 8 street, 9 locality,
  10 postcode, 11 area, 12 area_type, 13 contract_date, 14 settlement_date,
  15 price, 16 zoning, 17 nature_of_property, 18 primary_purpose,
  19 strata_lot, 23 dealing_number
C record: 1 district, 2 property_id, 3 sale_counter, 5 legal_description
  (multiple C records per sale concatenate in order, no separator).

Archived-format B record:
  1 district, 2 source tag (alphabetic, e.g. ARCHIVE), 3 valuation_num,
  4 property_id (not stable — unused), 5 unit, 6 house_number, 7 street,
  8 suburb, 9 postcode, 10 contract_date, 11 price, 12 legal_description,
  13 area, 14 area_type, 17 zone_code
Archived dates are DD/MM/YYYY in practice (fact sheet says CCYYMMDD — both
are handled). No settlement date / nature / purpose / strata / dealing.
"""

import io
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from .keys import archived_sale_key, current_sale_key

log = logging.getLogger(__name__)

EARLIEST_CONTRACT_DATE = "1990-01-01"


def iter_dat_members(zip_path: Path) -> Iterator[tuple[str, list[str]]]:
    """Yield (member_name, lines) for every .DAT file in the ZIP, recursing
    into nested ZIPs. Decoding errors are replaced, never fatal."""
    with zipfile.ZipFile(zip_path) as zf:
        yield from _walk_zip(zf, zip_path.name)


def _walk_zip(zf: zipfile.ZipFile, prefix: str) -> Iterator[tuple[str, list[str]]]:
    for name in zf.namelist():
        lower = name.lower()
        if lower.endswith(".dat"):
            text = zf.read(name).decode("utf-8", errors="replace")
            yield f"{prefix}/{name}", text.splitlines()
        elif lower.endswith(".zip"):
            try:
                inner = zipfile.ZipFile(io.BytesIO(zf.read(name)))
            except zipfile.BadZipFile:
                log.warning("bad nested zip %s in %s — skipped", name, prefix)
                continue
            yield from _walk_zip(inner, f"{prefix}/{name}")


def _blank_to_none(value: str) -> str | None:
    value = value.strip()
    return value or None


def _iso_date_current(raw: str) -> str | None:
    """CCYYMMDD -> YYYY-MM-DD, None if unparseable."""
    raw = raw.strip()[:8]
    try:
        return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _iso_date_archived(raw: str) -> str | None:
    """DD/MM/YYYY (usual) or CCYYMMDD (per fact sheet) -> YYYY-MM-DD."""
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_price(raw: str) -> int | None:
    """Blank and zero both mean no market price (non-market transfer)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        price = int(float(raw))
    except ValueError:
        return None
    return price or None


def _parse_area(raw_area: str, raw_type: str) -> float | None:
    raw_area = raw_area.strip()
    if not raw_area:
        return None
    try:
        area = float(raw_area)
    except ValueError:
        return None
    if raw_type.strip().upper() == "H":
        area *= 10_000  # hectares -> sqm
    return area


def _contract_date_ok(iso: str | None, today: date) -> bool:
    return iso is not None and EARLIEST_CONTRACT_DATE <= iso <= today.isoformat()


def parse_zip(zip_path: Path, ingested_at: str, today: date | None = None) -> Iterator[dict]:
    """Yield normalized sales dicts (schema columns) from one PSI ZIP."""
    today = today or date.today()
    for member, lines in iter_dat_members(zip_path):
        yield from parse_dat_lines(lines, member, ingested_at, today)


def parse_dat_lines(lines: list[str], member: str, ingested_at: str,
                    today: date) -> Iterator[dict]:
    """Parse one .DAT file's lines. C records join to B records within the
    same file (B and C for a sale always share a file)."""
    # Legal descriptions from current-format C records. Multiple C records
    # for one sale concatenate in file order without a separator.
    legal: dict[tuple[str, str, str], str] = {}
    for line in lines:
        if line.startswith("C;"):
            parts = line.split(";")
            if len(parts) >= 6:
                key = (parts[1].strip(), parts[2].strip(), parts[3].strip())
                legal[key] = legal.get(key, "") + parts[5].strip()

    for line in lines:
        if not line.startswith("B;"):
            continue
        parts = line.split(";")
        if len(parts) < 3:
            continue
        is_archived = any(c.isalpha() for c in parts[2].strip())
        row = (_parse_archived_b(parts) if is_archived else _parse_current_b(parts, legal))
        if row is None:
            continue
        if not _contract_date_ok(row["contract_date"], today):
            continue
        row["source_file"] = member
        row["ingested_at"] = ingested_at
        yield row


def _parse_current_b(parts: list[str], legal: dict) -> dict | None:
    if len(parts) < 24:
        return None
    district = parts[1].strip()
    property_id = parts[2].strip()
    sale_counter = parts[3].strip()
    return {
        "sale_key": current_sale_key(district, property_id, sale_counter),
        "district_code": district,
        "property_id": property_id,
        "sale_counter": sale_counter,
        "download_datetime": _blank_to_none(parts[4]),
        "property_name": _blank_to_none(parts[5]),
        "unit": _blank_to_none(parts[6]),
        "house_number": _blank_to_none(parts[7]),
        "street": _blank_to_none(parts[8]),
        "locality": (parts[9].strip().upper() or None),
        "postcode": _blank_to_none(parts[10]),
        "area_sqm": _parse_area(parts[11], parts[12]),
        "contract_date": _iso_date_current(parts[13]),
        "settlement_date": _iso_date_current(parts[14]),
        "price": _parse_price(parts[15]),
        "zoning": _blank_to_none(parts[16]),
        "nature_of_property": _blank_to_none(parts[17]),
        "primary_purpose": _blank_to_none(parts[18]),
        "strata_lot": _blank_to_none(parts[19]),
        "dealing_number": _blank_to_none(parts[23]),
        "legal_description": legal.get((district, property_id, sale_counter)) or None,
        "source": "current",
    }


def _parse_archived_b(parts: list[str]) -> dict | None:
    if len(parts) < 18:
        return None
    district = parts[1].strip()
    unit = _blank_to_none(parts[5])
    house = _blank_to_none(parts[6])
    street = _blank_to_none(parts[7])
    locality = parts[8].strip().upper() or None
    postcode = _blank_to_none(parts[9])
    contract = _iso_date_archived(parts[10])
    price = _parse_price(parts[11])
    if contract is None:
        return None
    return {
        "sale_key": archived_sale_key(district, unit, house, street, locality,
                                      postcode, contract, price),
        "district_code": district,
        "property_id": None,
        "sale_counter": None,
        "download_datetime": None,
        "property_name": None,
        "unit": unit,
        "house_number": house,
        "street": street,
        "locality": locality,
        "postcode": postcode,
        "area_sqm": _parse_area(parts[13], parts[14]),
        "contract_date": contract,
        "settlement_date": None,
        "price": price,
        "zoning": _blank_to_none(parts[17]),
        "nature_of_property": None,
        "primary_purpose": None,
        "strata_lot": None,
        "dealing_number": None,
        "legal_description": _blank_to_none(parts[12]),
        "source": "archived",
    }
