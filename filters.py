"""
filters.py — shared record-level filtering rules used by both the daily
scraper (main.py) and the retroactive sheet audit tool (audit_recent.py).

Deliberately dependency-free (stdlib `re` only) so both callers can import
it cheaply without pulling in scraping/Sheets dependencies.
"""
import re

# UNIT / APARTMENT(S) / APT / STE / SUITE / BLDG / BUILDING (whole word,
# case-insensitive) OR a bare "#123"-style unit marker.
_MULTIFAMILY_RE = re.compile(
    r'\b(UNIT|APARTMENTS?|APT|STE|SUITE|BLDG|BUILDING)\b|#\s*\d+',
    re.IGNORECASE,
)


def is_multifamily(address: str = '', city: str = '') -> bool:
    """True if either field contains a unit/apt/suite/building marker.

    Business rule: a unit-numbered property is multi-family and must be
    dropped, regardless of which field the marker ended up in — a raw
    "STREET, UNIT 902, TX" table address gets misparsed so the unit lands
    in `city` instead of `address`, and this must still be caught."""
    return bool(_MULTIFAMILY_RE.search(f"{address or ''} {city or ''}"))


if __name__ == '__main__':
    CASES = [
        # (address, city, expected)
        ("9910 Royal Lane", "Unit 902", True),        # the confirmed bug case
        ("123 Main St", "Apt 4", True),
        ("123 Main St #204", "", True),
        ("123 Main St # 204", "", True),
        ("123 Main St Suite 100", "", True),
        ("123 Main St Ste 5", "", True),
        ("123 Main St Bldg C", "", True),
        ("500 Oak Apartments", "Dallas", True),
        ("123 Main St", "Dallas", False),               # plain single-family, must NOT drop
        ("123 Stephens St", "San Antonio", False),      # no false-positive on "STE"
        ("", "", False),
    ]
    failures = 0
    for addr, city, expected in CASES:
        got = is_multifamily(addr, city)
        status = 'OK' if got == expected else 'FAIL'
        if status == 'FAIL':
            failures += 1
        print(f"[{status}] is_multifamily({addr!r}, {city!r}) = {got} (expected {expected})")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
