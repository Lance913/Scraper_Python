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


# Connector / boilerplate / OCR-noise tokens that are never part of a real name.
JUNK_TOKENS = {
    'THE', 'AND', 'OR', 'OF', 'A', 'AN', 'TO', 'ALL', 'SAID', 'ABOVE', 'NAMED',
    'ABOVE-NAMED', 'UNKNOWN', 'OCCUPANT', 'OCCUPANTS', 'TENANT', 'TENANTS',
    'ESTATE', 'HEIRS', 'DEFENDANT', 'DEFENDANTS', 'PLAINTIFF', 'GRANTOR',
    'GRANTEE', 'MORTGAGOR', 'BORROWER', 'DEBTOR', 'TRUSTEE', 'TRUSTOR', 'MAKER',
    'PAYEE', 'LENDER', 'SATX', 'TX', 'TEXAS', 'TPI', 'VNC', 'NA', 'TBD',
    'ITS', 'SUCCESSOR', 'SUCCESSORS', 'ASSIGN', 'ASSIGNS', 'NOMINEE', 'AKA',
    'PURCHASER', 'PURCHASERS', 'INTEREST', 'PROPERTY', 'NOTICE', 'SALE',
}
NAME_SUFFIXES = {'JR', 'SR', 'II', 'III', 'IV', 'V'}


def _has_vowel(w):
    return any(c in 'AEIOUaeiouY' for c in w)


def looks_like_person(name):
    up = name.upper()
    if any(t in up for t in NON_OWNER_TOKENS):
        return False
    words = name.split()
    if not (2 <= len(words) <= 5):
        return False
    if any(w.upper().strip('.') in JUNK_TOKENS for w in words):
        return False
    # Real name words: >=2 letters AND contain a vowel (kills OCR junk like "Vnc",
    # "Tpi"); standalone initials ("D.") and suffixes ("Jr") don't count as real.
    real = [w for w in words
            if len(w.strip('.')) >= 2 and _has_vowel(w)
            and w.upper().strip('.') not in NAME_SUFFIXES]
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
    # Last name = final token, skipping a trailing suffix (Jr/Sr/III) or a
    # standalone initial.
    last = parts[-1]
    i = len(parts) - 1
    while i > 0 and (last.upper().strip('.') in NAME_SUFFIXES
                     or len(last.replace('.', '')) <= 1):
        i -= 1
        last = parts[i]
    return first, last.title()


# ── Property-address extraction (fallback when the results table has no address,
#    e.g. Denton). The hard part is NOT picking up the servicer/trustee/law-firm
#    address — so we anchor on property labels / the barcode "/ ADDR" line / a
#    header block, and reject any candidate in a servicer/trustee/Suite context.

_ADDR_CORE = r'(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .\'#-]{2,45}?)\s*,\s*([A-Za-z][A-Za-z .\'-]+?)\s*,?\s*(?:TX|TEXAS)\s*,?\s*(\d{5})'

RE_ADDR_LABELED = re.compile(
    r'(?:commonly\s+known\s+as|property\s+address|local\s+address|site\s+address|'
    r'address\s+of\s+(?:the\s+)?property|property\s+to\s+be\s+sold[^:]*)\s*[:\-]?\s*' + _ADDR_CORE,
    re.I)
RE_ADDR_SLASH = re.compile(r'/\s*' + _ADDR_CORE, re.I)           # "<file#> / 3532 CRICKET DRIVE, DENTON, TX 76207"
RE_ADDR_GENERIC = re.compile(_ADDR_CORE, re.I)

RE_CSZ = re.compile(r'^([A-Za-z][A-Za-z .\'-]+?),?\s+(?:TX|TEXAS)\s*,?\s*(\d{5})$', re.I)
RE_STREET_LINE = re.compile(r'^(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .\'#-]{2,45}?)(?:\s+\d{6,})?$')

# Contexts that mark an address as NOT the property: the servicer/trustee/law
# firm, OR the county sale location (courthouse / clerk / place of sale).
_BAD_ADDR_CTX = re.compile(
    r'(trustee|servic|mortgagee|beneficiary|attorney|c/o|\bsuite\b|\bste\.?\b|'
    r'p\.?\s?o\.?\s*box|law\b|title\s+services?|'
    r'courthouse|court\s*house|courts?\s+building|place\s+of\s+sale|commissioner|'
    r'designated\s+by|county\s+clerk|\bclerk\b|filed\s+and\s+recorded|recording\s+requested)',
    re.I)


def _bad_ctx(text, start):
    # Check a wide window on both sides — the courthouse/clerk label can trail
    # the address (e.g. "... 1450 E McKinney St, Denton TX 76209  Denton County Clerk").
    return bool(_BAD_ADDR_CTX.search(text[max(0, start - 120):start + 110]))


def _clean_street(s):
    s = re.sub(r'\s+\d{6,}\s*$', '', s)            # trailing barcode digits
    return re.sub(r'\s+', ' ', s).strip(' ,.-').title()


def _good_zip(z):
    return z[:2] in ('75', '76', '77', '78', '79')  # Texas metros


def parse_address(text):
    """Best-effort property (street, city, zip) from NTS OCR text, or ('', '', '').

    Prefers labeled / barcode-line / header-block addresses and rejects anything
    in a servicer/trustee/law-firm context. Returns empty rather than risk
    writing the law firm's address."""
    for rx in (RE_ADDR_LABELED, RE_ADDR_SLASH):
        for m in rx.finditer(text):
            if _good_zip(m.group(3)) and not _bad_ctx(text, m.start()):
                return _clean_street(m.group(1)), m.group(2).strip().title(), m.group(3)

    # Header block: a street line immediately followed by "CITY, TX ZIP".
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for i in range(min(len(lines), 10)):
        sm = RE_STREET_LINE.match(lines[i])
        if sm and i + 1 < len(lines):
            cm = RE_CSZ.match(lines[i + 1])
            if cm and _good_zip(cm.group(2)):
                return _clean_street(sm.group(1)), cm.group(1).strip().title(), cm.group(2)

    # Last resort: a generic "street, city, TX zip" not in a bad context.
    for m in RE_ADDR_GENERIC.finditer(text):
        if _good_zip(m.group(3)) and not _bad_ctx(text, m.start()):
            return _clean_street(m.group(1)), m.group(2).strip().title(), m.group(3)
    return '', '', ''


# Known non-property addresses (county courthouse / clerk / sale locations) and
# commercial markers — these must never be written as a homeowner's property,
# whether they come from OCR or from the results table.
_BLOCKLIST_STREETS = [
    ('1450', 'mckinney'),     # Denton County Courts / Records building
    ('100', 'dolorosa'),      # Bexar County Courthouse
]


def is_nonproperty_address(street: str) -> bool:
    s = (street or '').lower()
    if not s:
        return False
    if re.search(r'\b(suite|ste)\b', s):     # commercial/office, not a homeowner
        return True
    return any(num in s and name in s for num, name in _BLOCKLIST_STREETS)


def address_and_owner_from_png(body):
    """OCR a page-1 PNG once and return (first, last, street, city, zip)."""
    txt = ocr_png_bytes(body)
    owner = parse_owner(txt)
    first, last = split_name(owner) if owner else ('', '')
    street, city, zip_c = parse_address(txt)
    return first, last, street, city, zip_c


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
        # Garbage parses observed in the Bexar dry-run — must now reject (-> '').
        'junk_and': "Grantor: Blanca And",
        'junk_above': "executed by The Above-Named",
        'junk_vnc': "Grantor: Michael Vnc",
        'junk_tpi': "Debtor(s): Cordova Tpi",
        'junk_satx': "Grantor: San Satx",
        'suffix_ok': "Grantor: John Smith Jr & Mary Smith",
    }
    for k, txt in SAMPLES.items():
        owner = parse_owner(txt)
        f, l = split_name(owner) if owner else ('', '')
        print(f"{k:12} owner={owner!r:42} -> first={f!r} last={l!r}")

    print("\n--- address ---")
    ADDR = {
        'denton_slash': ("26-000055-516-1 / 3532 CRICKET DRIVE, DENTON, TX 76207",
                         ('3532 Cricket Drive', 'Denton', '76207')),
        'bexar_known':  ("Commonly known as: 8914 ARABIAN KING, CONVERSE, TEXAS 78109",
                         ('8914 Arabian King', 'Converse', '78109')),
        'bexar_local':  ("Local Address: 22915 Savannah Heights, Von Ormy, TX 78073",
                         ('22915 Savannah Heights', 'Von Ormy', '78073')),
        'header_block': ("12443 ALSTROEMERIA 00000008006629\nSAN ANTONIO, TX 78253\nNOTICE",
                         ('12443 Alstroemeria', 'San Antonio', '78253')),
        # Law-firm / servicer addresses must be REJECTED (-> empty).
        'lawfirm_trap': ("Mortgage Servicer's Address:\n820 Follin Lane SE, Vienna, VA 22180\n"
                         "McCarthy & Holthus, LLP\n1255 West 15th Street, Suite 1060\nPlano, TX 75075\n"
                         "Legal Description: LOT 28, BLOCK 11, LEGEND CREST",
                         ('', '', '')),
        'avt_trap':     ("c/o AVT Title Services, LLC, 5177 Richmond Avenue, Suite 1230, Houston, TX 77056",
                         ('', '', '')),
        # County sale-location / clerk addresses must be REJECTED.
        'courthouse':   ("Place of Sale: Denton County Courts Building, "
                         "1450 East Mckinney Street, Denton, TX 76209", ('', '', '')),
        'clerk_trail':  ("1450 East Mckinney Street, Denton, TX 76209 Denton County Clerk", ('', '', '')),
        # A real property line near a file number still works.
        'real_after':   ("Cause No 2025-1234 / 330 Brook Cove Ln, Lewisville, TX 75067",
                         ('330 Brook Cove Ln', 'Lewisville', '75067')),
    }
    for k, (txt, exp) in ADDR.items():
        got = parse_address(txt)
        print(f"{k:12} {'OK ' if got == exp else 'FAIL'} got={got} exp={exp}")
