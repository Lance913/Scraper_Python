"""
zip_lookup.py — backfill missing ZIP codes via the free US Census Bureau
Geocoder (no API key required). Resilience-first: must never crash or hang
the daily scrape job — every failure mode degrades to "leave zip blank."
"""
import logging
import time

import requests

logger = logging.getLogger('zip_lookup')

GEOCODER_URL = 'https://geocoding.geo.census.gov/geocoder/locations/onelineaddress'
TIMEOUT_SEC = 8
REQUEST_DELAY_SEC = 0.2  # be polite to a free gov service; no published rate limit


def _parse_response(data: dict) -> str:
    """Pure parsing, factored out so it's unit-testable without network."""
    try:
        matches = (data.get('result') or {}).get('addressMatches') or []
        if not matches:
            return ''
        if len(matches) > 1:
            logger.info(f"Ambiguous geocode: {len(matches)} matches, using first "
                        f"({matches[0].get('matchedAddress')!r})")
        zip_code = str((matches[0].get('addressComponents') or {}).get('zip', '')).strip()
        return zip_code if zip_code.isdigit() and len(zip_code) == 5 else ''
    except Exception:
        return ''


def lookup_zip(address: str, city: str = '', state: str = 'TX') -> str:
    """Single-address lookup. Returns '' on ANY failure/timeout/no-match —
    callers must never treat '' as an error to propagate."""
    if not address:
        return ''
    one_line = f"{address}, {city}, {state}" if city else f"{address}, {state}"
    try:
        resp = requests.get(
            GEOCODER_URL,
            params={'address': one_line, 'benchmark': 'Public_AR_Current', 'format': 'json'},
            timeout=TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return _parse_response(resp.json())
    except Exception as exc:
        logger.warning(f"Zip lookup failed for {one_line!r}: {exc}")
        return ''


def backfill_zip(records, *, max_lookups=None):
    """Mutate `records` in place: for any record with an address but empty
    zip_code, try to fill it. Returns (filled, attempted).

    Only meant to run on records that already passed the multi-family
    filter — no caller-side check needed here since a missing zip on a
    dropped record is moot (the record never gets written either way)."""
    if not records:
        return 0, 0
    attempted = filled = 0
    for r in records:
        if r.get('zip_code') or not (r.get('address') or '').strip():
            continue
        if max_lookups is not None and attempted >= max_lookups:
            break
        attempted += 1
        z = lookup_zip(r['address'], r.get('city', ''), r.get('state', 'TX'))
        if z:
            r['zip_code'] = z
            filled += 1
        time.sleep(REQUEST_DELAY_SEC)
    logger.info(f"Zip backfill: {filled}/{attempted} filled (of {len(records)} total records)")
    return filled, attempted


if __name__ == '__main__':
    # Offline: validated against a real response captured for 9910 Royal
    # Lane, Dallas TX — no network needed for these.
    SAMPLE_RESPONSE = {"result": {"addressMatches": [
        {"addressComponents": {"zip": "75231"}, "matchedAddress": "9910 ROYAL LN, DALLAS, TX, 75231"},
        {"addressComponents": {"zip": "75238"}, "matchedAddress": "9910 ROYAL LN, DALLAS, TX, 75238"},
    ]}}
    assert _parse_response(SAMPLE_RESPONSE) == '75231'
    assert _parse_response({"result": {"addressMatches": []}}) == ''
    assert _parse_response({}) == ''
    print("_parse_response offline tests passed.")

    import sys
    if '--online' in sys.argv:
        logging.basicConfig(level=logging.INFO)
        for addr, city in [("9910 Royal Lane", "Dallas"), ("1234 Main St", "Houston")]:
            print(addr, city, '->', lookup_zip(addr, city))
