"""
publicsearch_extract.py — owner-name extraction for the publicsearch.us
Notice-of-Foreclosure / Notice-of-(Substitute)-Trustee's-Sale layout.

Why a separate parser from harris_extract.py:
  - On publicsearch the property ADDRESS, sale date, recorded date and doc number
    all come straight from the Foreclosures results table — so OCR is needed ONLY
    for the owner name.
  - The owner name on these docs is usually MIXED CASE (clean digital PDFs, not
    scans) and sits under law-firm-specific labels, which harris_extract's
    ALL-CAPS, terminator-specific patterns miss. Real examples observed:
        "Grantor: Stephanie D. Collett & Adrian C. Collett"   (also Maker:/BORROWER:)
        "Debtor(s): Reymundo Camacho"                          (HOA assessment foreclosure)
        "WHEREAS, on September 4, 2013, Neri Ulises Ramirez Villasenor, single man executed a Deed of Trust"
        "...executed by KURT WALLACE EDWARDS, securing the payment..."

The owner is the Grantor/Mortgagor/Maker/Borrower/Debtor — NOT the
Payee/Beneficiary/Lender/Trustee/Servicer (those are the bank/law firm).
"""
import io
import logging
import re

logger = logging.getLogger('publicsearch_extract')

# Tokens that mean the captured string is an institution, not a person.
NON_OWNER_TOKENS = [
    'BANK', 'MORTGAGE', 'MERS', 'ELECTRONIC REGISTRATION', 'SYSTEMS', 'N.A.',
    'NATIONAL ASSOCIATION', 'ASSOCIATION', 'HOMEOWNERS', 'HOME OWNERS', 'HOA',
    'LLC', 'L.L.C', 'LLP', 'L.P', ' LP', 'INC', 'CORPORATION', 'CORP', 'COMPANY',
    'TRUST', 'TRUSTEE', 'SERVICING', 'SERVICER', 'FUND', 'CAPITAL', 'FINANCIAL',
    'HOLDINGS', 'LENDER', 'BENEFICIARY', 'PAYEE', 'SAVINGS', 'FSB', 'FEDERAL',
    'WELLS FARGO', 'SOCIETY', 'INVESTMENTS', 'PARTNERS', 'PARTNERSHIP', 'LTD',
    'D R HORTON', 'DR HORTON', 'LENNAR', 'PULTE', 'KB HOME', 'MERITAGE', 'CENTEX',
    'BEAZER', 'CHESMAR', 'M/I HOMES', 'STARLIGHT', 'COUTO', 'TAYLOR MORRISON',
    'CITY OF', 'COUNTY OF', 'DEPARTMENT', 'REVENUE', 'AUTHORITY', 'DISTRICT',
    'PROPERTIES', 'REALTY', 'GROUP', 'VENTURES', 'ENTERPRISES',
]

# A person-name token: starts uppercase, then letters / apostrophe / hyphen, or
# an initial like "D." — handles both Mixed Case and ALL CAPS.
_NAME_WORD = r"(?:[A-Z][A-Za-z'\-]+|[A-Z]\.)"
_NAME = rf"{_NAME_WORD}(?:\s+(?:{_NAME_WORD})){{1,4}}"

# Priority 1: labeled fields naming the borrower (forward: "Label: NAME").
RE_LABEL = re.compile(
    r'\b(?:Grantor|Mortgagor|Maker|Borrower|Debtor|Obligor|Trustor)\(?s?\)?\s*[:\-]\s*'
    r'([^\n]+)', re.I)

# Priority 2: narrative anchors.
RE_NARRATIVE = [
    re.compile(rf'\bexecuted by\s+({_NAME})', re.I),
    # "WHEREAS, on <date>, NAME, (a) single/married/unmarried man/woman executed"
    re.compile(rf'\bWHEREAS,?\s+on\s+[A-Za-z0-9 ,\.]+?,\s*({_NAME})\s*,\s*'
               r'(?:a\s+|an\s+)?(?:single|married|unmarried|husband|wife|individual)', re.I),
    # "NAME, grantor(s)"  (label trails the name)
    re.compile(rf'({_NAME})\s*,?\s+grantor\(?s?\)?\b', re.I),
]

_STOP_AFTER = re.compile(
    r'\b(a\s+single|an?\s+unmarried|single|married|husband|wife|individually|'
    r'aka|a/k/a|fka|f/k/a|whose|securing|grantor|and\s+spouse)\b', re.I)


def _first_person(raw):
    """Take a captured string and reduce it to the first individual's name."""
    s = raw.strip()
    # Co-borrowers: keep only the first person.
    s = re.split(r'\s*&\s*|\s+and\s+', s, maxsplit=1, flags=re.I)[0]
    # Cut at a comma or a status/role word that follows the name.
    s = s.split(',')[0]
    m = _STOP_AFTER.search(s)
    if m:
        s = s[:m.start()]
    # Drop OCR noise / stray non-name characters at the edges.
    s = re.sub(r'[^A-Za-z\'\-. ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip(' .,-')


def looks_like_person(name):
    up = name.upper()
    if any(t in up for t in NON_OWNER_TOKENS):
        return False
    words = name.split()
    if not (2 <= len(words) <= 5):
        return False
    # At least two words that are real (>=2 alpha chars), i.e. not just initials.
    real = [w for w in words if sum(c.isalpha() for c in w) >= 2]
    if len(real) < 2:
        return False
    alpha_ratio = sum(c.isalpha() or c in " .'-" for c in name) / max(len(name), 1)
    return alpha_ratio > 0.85


def parse_owner(text):
    """Return the borrower/owner full name from NTS OCR text, or ''."""
    candidates = []
    for m in RE_LABEL.finditer(text):
        candidates.append(_first_person(m.group(1)))
    for rx in RE_NARRATIVE:
        for m in rx.finditer(text):
            candidates.append(_first_person(m.group(1)))
    for c in candidates:
        if looks_like_person(c):
            return c
    return ''


def split_name(full):
    """'Stephanie D. Collett' -> ('Stephanie', 'Collett'). Standard order."""
    parts = [p for p in full.split() if p]
    if not parts:
        return '', ''
    if len(parts) == 1:
        return '', parts[0].title()
    first = parts[0].title()
    # Last name = final token (skip a trailing standalone initial if present).
    last = parts[-1]
    if len(last.replace('.', '')) <= 1 and len(parts) >= 3:
        last = parts[-2]
    return first, last.title()


# ── OCR helpers ─────────────────────────────────────────────────────────────

def ocr_png_bytes(body):
    """OCR a single PNG image (publicsearch serves doc pages as PNGs)."""
    import pytesseract
    from PIL import Image
    try:
        return pytesseract.image_to_string(Image.open(io.BytesIO(body)))
    except Exception as e:
        logger.warning(f"OCR failed: {e}")
        return ''


def owner_from_png(body):
    """Convenience: OCR a page-1 PNG and return (first, last)."""
    owner = parse_owner(ocr_png_bytes(body))
    if not owner:
        return '', ''
    return split_name(owner)


if __name__ == '__main__':
    # Smoke test against the real OCR snippets observed via the probes.
    SAMPLES = {
        'collett': "Grantor: Stephanie D. Collett & Adrian C. Collett\nTrustee: Dudley Beadles\n"
                   "Beneficiary: Wells Fargo Bank, National Association\nBORROWER: Stephanie D. Collett & Adrian C. Collett",
        'camacho': "Association: Savannah Heights Home Owners Association\nDebtor(s): Reymundo Camacho\n"
                   "Substitute Trustee: James W. King, Renee Roberts",
        'villasenor': "WHEREAS, on September 4, 2013, Neri Ulises Ramirez Villasenor, single man executed a "
                      "Deed of Trust conveying to North O. West, Trustee, the real property",
        'edwards': "Deed of Trust or Contract Lien executed by KURT WALLACE EDWARDS, securing the payment "
                   "of the indebtednesses in the original principal amount of $340,907.00",
        'bank_only': "Beneficiary: Wells Fargo Bank, National Association\nLENDER: Wilmington Savings Fund Society, FSB",
    }
    for k, txt in SAMPLES.items():
        owner = parse_owner(txt)
        f, l = split_name(owner) if owner else ('', '')
        print(f"{k:12} owner={owner!r:42} -> first={f!r} last={l!r}")
