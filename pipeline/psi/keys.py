"""sale_key construction per CLAUDE.md "Identity & dedup rules"."""

import hashlib


def current_sale_key(district_code: str, property_id: str, sale_counter: str) -> str:
    return f"C:{district_code}:{property_id}:{sale_counter}"


def archived_sale_key(
    district_code: str,
    unit: str | None,
    house_number: str | None,
    street: str | None,
    locality: str | None,
    postcode: str | None,
    contract_date: str,
    price: int | None,
) -> str:
    """Deterministic key for archived rows, which have no stable IDs.

    sha1 over the normalized identity tuple; blank and NULL fields hash
    identically so re-parses are idempotent.
    """
    parts = (
        district_code,
        unit,
        house_number,
        street,
        locality,
        postcode,
        contract_date,
        "" if price is None else str(price),
    )
    normalized = "|".join((p or "").strip().upper() for p in parts)
    return "A:" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()
