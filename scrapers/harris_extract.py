"""
Harris NTS extraction — rewritten around REAL document anchors observed in OCR.

PROPERTY ADDRESS sources (in priority order):
  1. "Property Address: <street> TX <zip>"  (explicit label, e.g. doc 2462)
  2. Header block: the street+barcode line immediately followed by "CITY, TX ZIP"
     appearing in the first ~6 lines, right after the FRCL number (docs 2405/2406).
  These are the PROPERTY. We explicitly REJECT the auction venue:
     9401 KNIGHT ROAD / BAYOU CITY EVENT CENTER / HOUSTON TEXAS 77045.

OWNER sources (in priority order):
  1. "Grantor(s)/Mortgagor(s)" label followed by NAME (doc 2395)
  2. "Trustor(s): NAME"  (doc 2462)
  3. "executed by NAME[, A SINGLE MAN/WIFE AND HUSBAND]"  (docs 2406/2462)
  4. "on <Month DD, YYYY>, NAME, A SINGLE PERSON as"  (doc 9391)
  Reject lenders/servicers/trustees/MERS/banks.

Tiered: full (name+addr) / partial (one) / none (caller writes doc_id).
"""
import re, logging
logger = logging.getLogger('harris_extract')

# --- Venue / noise we must never treat as property or owner ---
VENUE_TOKENS = ['KNIGHT', 'BAYOU CITY', 'EVENT CENTER', 'BALLROOM', '77045',
                'COMMISSION', 'COURTHOUSE', 'COUNTY COR']
HEADER_NOISE = ['HUDSPETH', 'COUNTY CLERK', 'FILED', 'RECORDING REQUESTED',
                'WHEN RECORDED', 'TENESHIA', 'NOTICE OF']

NON_OWNER_TOKENS = [
    'WELLS FARGO','MORTGAGE','BANK','MERS','NOMINEE','REGISTRATION','SYSTEMS',
    'SERVICER','SERVICING','LLC','LLP','TRUSTEE','CORPS','CARRINGTON','LOANDEPOT',
    'CISNEROS','N.A.','NATIONAL','STATEVIEW','BLVD','AVENUE','CORPORATION','LOANCARE',
    'COMPANY','INC','ADVISORS','CAPITAL','FINANCIAL','HOLDINGS','FARGO','LAKEVIEW',
    'HUDSPETH','COUNTY CLERK','CLERK','TENESHIA','HARRIS COUNTY','TEXAS','ASSOCIATION',
    'RECORDING','BASTAIN','EVENT CENTER','BALLROOM','COMMISSION','US BANK','ELECTRONIC',
    'AUCTION','BARRETT','DAFFIN','FRAPPIER','ENGEL','PRESTIGE','DEFAULT','MCCARTHY',
    'HOLTHUS','SAUCEDO','DASIGENIS','BENEFICIARY','MORTGAGEE','ALLIANCE','LENDING',
]

# --- Address patterns ---
# explicit label
RE_PROP_LABEL = re.compile(r'Property\s+Address[:\s]+(.+?)(?:\s+TX|\s+TEXAS)\s+(\d{5})', re.I)
# city,state,zip line
RE_CSZ = re.compile(r'^([A-Z][A-Za-z .]+?),?\s+(?:TX|TEXAS)\s+(\d{5})(?:-\d{4})?\s*$', re.I)
# street line (number + words), trailing barcode digits stripped
RE_STREET = re.compile(r'^\s*(\d{2,6}\s+[A-Z0-9][A-Za-z0-9 .#-]{3,45}?)(?:\s+\d{6,})?\s*-?\s*$')

# --- Fallback: a "STREET, CITY, TX ZIP" blob anywhere in the text, labeled
# ("commonly known as"), slash-prefixed (case# // address, e.g. barcode/
# reference lines), or bare. Broader than RE_PROP_LABEL/RE_STREET above (not
# limited to a clean label or the first ~8 lines), so it only runs as a last
# resort and is gated hard by the Harris-area zip + a proximity check for
# trustee/servicer/suite language, to avoid ever picking up a law firm's
# mailing address instead of the property (same trap RE_PROP_LABEL/RE_STREET
# already avoid via SERVICER_CITIES/_is_venue).
_ADDR_BLOB = (r'(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .\'#-]{2,45}?)'
              r'\s*,\s*([A-Za-z][A-Za-z .\'-]+?)\s*,?\s*(?:TX|TEXAS)\s*,?\s*(\d{5})')
RE_ADDR_LABELED = re.compile(r'(?:commonly\s+known\s+as)\s*[:\-]?\s*' + _ADDR_BLOB, re.I)
RE_ADDR_SLASH = re.compile(r'/\s*' + _ADDR_BLOB)
RE_ADDR_GENERIC = re.compile(_ADDR_BLOB)

# Deliberately NOT "trustee": every Harris doc is titled "Notice of
# Substitute Trustee's Sale" and mentions "Substitute Trustee" repeatedly as
# normal boilerplate unrelated to any specific address, so it wouldn't
# discriminate at all here (unlike the other keywords below, which only show
# up when describing a specific mailing address).
_ADDR_BAD_CTX = re.compile(
    r'(servic|mortgagee|beneficiary|attorney|c/o|\bsuite\b|\bste\.?\b|'
    r'p\.?\s?o\.?\s*box|\blaw\b|title\s+services?)', re.I)


def _addr_bad_ctx(text, start, end):
    # Check a window spanning before AND through the match — "Suite 1230"
    # is often captured as part of the street text itself, not just nearby.
    return bool(_ADDR_BAD_CTX.search(text[max(0, start - 120):end + 60]))

# --- Owner patterns (label-anchored, highest confidence first) ---
RE_OWNER = [
    re.compile(r'Grantor\(s\)\s*/?\s*Mortgagor\(s\)\s*\n?\s*(?:\d{1,2}/\d{1,2}/\d{4}\s+)?([A-Z][A-Z. ]{4,50}?)(?:\s+HUSBAND|\s+WIFE|\s+A\s+SINGLE|\s*\n|\s*$)', re.I),
    re.compile(r'Trustor\(s\)[:\s]+([A-Z][A-Z. ]{4,40}?)(?:\s+Orig|\s+MORTGAGE|\s*\n|\s*$)', re.I),
    re.compile(r'\bexecuted by\s+([A-Z][A-Z. ]{4,40}?)(?:,?\s+A\s+SINGLE|,?\s+WIFE|,?\s+HUSBAND|,\s*$|\s*\n)', re.I),
    re.compile(r'\bon\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4},?\s+([A-Z][A-Z. ]+?)(?:,|\s+A\s+SINGLE|\s+AS\b|\s+AND\b)'),
    re.compile(r'\b([A-Z]{3,}\s+[A-Z]{3,}\s+AND\s+(?:HIS\s+WIFE|HER\s+HUSBAND|EDNA)[A-Z, ]*)', re.I),
]


def _is_venue(s): return any(t in s.upper() for t in VENUE_TOKENS)

def _is_harris_zip(zipc):
    '''Harris County and immediate metro ZIPs start 770-775 (+ 77562, 77571 etc).
    Reject Dallas/Plano/Addison (75xxx), Austin (78xxx), El Paso (79xxx) — those
    are law-firm / servicer mailing addresses, NOT the foreclosed property.'''
    if not zipc or len(zipc) < 3:
        return False
    prefix = zipc[:3]
    # Greater Houston / Harris area
    return prefix in ('770','771','772','773','774','775','776','777')

# Law-firm / servicer cities that must never be treated as the property city
SERVICER_CITIES = ['ADDISON','PLANO','IRVING','DALLAS','COPPELL','EL PASO',
                   'FORT MILL','OWENSBORO','VIRGINIA','AUSTIN','SAN ANTONIO']

JUNK_NAMES = {'TO THE','THE PROPERTY','OF THE','IN THE','AND THE','TO BE',
              'AS THE','FOR THE','BY THE','OF SALE','DEED OF'}

def looks_like_owner(name):
    up = name.upper().strip()
    if up in JUNK_NAMES: return False
    if any(t in up for t in NON_OWNER_TOKENS): return False
    if _is_venue(name): return False
    w = name.split()
    if not (2 <= len(w) <= 7): return False
    # each word should be a plausible name token (>=2 chars, mostly alpha)
    real_words = [x for x in w if len(x) >= 2 and sum(ch.isalpha() for ch in x) >= 2]
    if len(real_words) < 2: return False
    return sum(c.isalpha() or c.isspace() or c=='.' for c in name)/max(len(name),1) > 0.82

def clean_name(name):
    name = re.sub(r'\b(A SINGLE PERSON|A SINGLE MAN|A SINGLE WOMAN|A MARRIED|AN UNMARRIED|HUSBAND AND WIFE|WIFE AND HUSBAND|AND HIS WIFE|AND HER HUSBAND).*$','',name,flags=re.I)
    # cut at OCR garble markers that follow the real name
    name = re.sub(r'\b(Orig|Inal|Origi|Original|Iginal|Ginal|Ped|Yel)\b.*$','',name,flags=re.I)
    # drop a dangling "And" / "&" at the end (incomplete co-borrower)
    name = re.sub(r'\s+(And|&)\s*$','',name,flags=re.I)
    # trailing 1-2 char fragment
    name = re.sub(r'\s+[A-Z]{1,2}$','',name)
    return re.sub(r'\s+',' ',name).strip(' ,.')

def split_name(full):
    if not full: return '',''
    p = full.split()
    if len(p)==1: return p[0].title(),''
    return p[0].title(), ' '.join(p[1:]).title()


HOUSTON_CITIES = ['HOUSTON','KATY','HUMBLE','SPRING','CYPRESS','CHANNELVIEW',
    'PASADENA','PEARLAND','TOMBALL','BAYTOWN','CONROE','RICHMOND','ROSENBERG',
    'MISSOURI CITY','SUGAR LAND','STAFFORD','DEER PARK','LA PORTE','FRIENDSWOOD']

def _split_city_from_street(street):
    '''If a city name is glued to the end of the street, split it off.'''
    up = street.upper()
    for city in sorted(HOUSTON_CITIES, key=len, reverse=True):
        # city at end, possibly with trailing OCR garble like 'HO! ON'
        if up.endswith(' '+city):
            return street[:-(len(city)+1)].strip(' ,.'), city.title()
    return street, ''

def parse_address(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # 1) explicit "Property Address:" label
    m = RE_PROP_LABEL.search(text)
    if m:
        street = re.sub(r'\s+\d{7,}$','',m.group(1).strip())
        street = re.sub(r'\s*-\s*$','',street).strip(' ,.')
        if not _is_venue(street) and _is_harris_zip(m.group(2)):
            street, city = _split_city_from_street(street)
            if city.upper() not in SERVICER_CITIES:
                return street, city, 'TX', m.group(2)

    # 2) header block: street line followed by CITY, TX ZIP, in first ~7 lines
    for i in range(min(len(lines), 8)):
        ln = lines[i]
        if any(h in ln.upper() for h in HEADER_NOISE): continue
        if _is_venue(ln): continue
        sm = RE_STREET.match(ln)
        if sm and i+1 < len(lines):
            nxt = lines[i+1]
            cm = RE_CSZ.match(nxt)
            if cm and not _is_venue(nxt) and _is_harris_zip(cm.group(2)):
                street = re.sub(r'\s*-\s*$','',sm.group(1).strip()).strip(' ,.')
                street2, scity = _split_city_from_street(street)
                city = scity or cm.group(1).strip().title()
                if city.upper() not in SERVICER_CITIES:
                    return street2, city, 'TX', cm.group(2)

    # 3) Broader fallback: a labeled ("commonly known as"), slash-prefixed, or
    # bare "STREET, CITY, TX ZIP" blob anywhere in the text. Unlike the removed
    # first-city-on-the-page approach this NEVER got tried before (that one
    # took whatever it found first, which was almost always the trustee law
    # firm's address) — this instead tries every candidate in order and keeps
    # the same Harris-zip + SERVICER_CITIES + venue guards, plus a proximity
    # check for trustee/servicer/suite language, so a law-firm address still
    # gets rejected even when it happens to sit in Houston with a 770-777 zip
    # (e.g. a title company's own Houston office).
    for rx in (RE_ADDR_LABELED, RE_ADDR_SLASH, RE_ADDR_GENERIC):
        for m in rx.finditer(text):
            street, city_raw, zipc = m.group(1), m.group(2), m.group(3)
            if not _is_harris_zip(zipc):
                continue
            if _is_venue(street) or _addr_bad_ctx(text, m.start(), m.end()):
                continue
            street = re.sub(r'\s*-\s*$', '', street.strip()).strip(' ,.')
            street2, scity = _split_city_from_street(street)
            city = scity or city_raw.strip().title()
            if city.upper() in SERVICER_CITIES:
                continue
            return street2, city, 'TX', zipc

    return '', '', 'TX', ''


def parse_owner(text):
    for rx in RE_OWNER:
        for m in rx.finditer(text):
            c = clean_name(m.group(1))
            if looks_like_owner(c):
                return c
    return ''


def extract_from_text(text):
    owner = parse_owner(text)
    street, city, state, zipc = parse_address(text)
    first, last = split_name(owner)
    return {'first_name':first,'last_name':last,'address':street,
            'city':city,'state':state,'zip_code':zipc,
            'has_data': bool(owner or street)}


def ocr_pdf_bytes(pdf_bytes, max_pages=3, dpi=200):
    from pdf2image import convert_from_bytes
    import pytesseract
    text=''
    try:
        for im in convert_from_bytes(pdf_bytes, dpi=dpi, first_page=1, last_page=max_pages):
            text += pytesseract.image_to_string(im) + '\n'
    except Exception as e:
        logger.warning(f"OCR failed: {e}")
    return text

def extract_from_pdf_bytes(pdf_bytes):
    return extract_from_text(ocr_pdf_bytes(pdf_bytes, max_pages=3))


if __name__ == '__main__':
    # Real excerpts pulled via a live OCR run against actual Harris NONE-tier
    # docs (2026-09-16) — see the "Diag - Harris OCR" workflow run this was
    # captured from. Positive cases confirm the new fallback (RE_ADDR_LABELED/
    # SLASH/GENERIC) recovers addresses RE_PROP_LABEL/RE_STREET miss; negative
    # cases confirm it still never picks up a trustee/servicer address.
    ADDR_CASES = {
        # FRCL-2026-2935: address only appears in a barcode/reference line
        # near the bottom of the page — RE_STREET/RE_PROP_LABEL never see it.
        'slash_edgemoor': (
            "I am whose address is c/o AVT Title Services, LLC, 5177 Richmond Avenue Suite 1230,\n"
            "Houston, TX 77056 I declare under penalty of perjury that I filed this Notice of "
            "Foreclosure Sale at the office\n"
            "of the Harris County Clerk and caused it to be posted at the place designated by "
            "the Harris County Commissioners Court\n\n"
            "26-000041-210-1 // 6922 EDGEMOOR DRIVE, HOUSTON, TX 77074",
            # Street stays raw OCR case here, matching RE_PROP_LABEL/RE_STREET
            # above (neither of which .title()s the street either) — only
            # publicsearch_extract.py's counties do that, not Harris.
            ('6922 EDGEMOOR DRIVE', 'Houston', 'TX', '77074'),
        ),
        # FRCL-2026-4878: bare "street, city, TX zip" near the top of the
        # page, no label and not isolated on its own line the way RE_STREET
        # + RE_CSZ require (it's followed by " ; case-number" on the same line).
        'bare_bennett': (
            "FILED 7/9/2026 10:44:06 AM\n"
            "715 Bennett Dr, Pasadena, TX 77503 ; 26-010010\n"
            "NOTICE OF SUBSTITUTE TRUSTEE'S SALE",
            ('715 Bennett Dr', 'Pasadena', 'TX', '77503'),
        ),
        # Trap: AVT Title Services' own Houston office has a valid 770xx zip,
        # so the zip-prefix gate alone would accept it — must be rejected via
        # the "c/o"/"suite" proximity check (_addr_bad_ctx), same as the
        # slash_edgemoor case above but with NO real property address present.
        'trap_avt_servicer': (
            "Tam whose address 1s c/o AVT Title Services, LLC, 5177 Richmond Avenue Suite 1230,\n"
            "Houston, TX 77056 I declare under penalty of perjury",
            ('', '', 'TX', ''),
        ),
        # Trap: the standard Harris auction venue (Bayou City Event Center) —
        # must stay rejected by _is_venue even via the new broader fallback.
        'trap_venue': (
            "Place of Sale of Property: THE BAYOU CITY EVENT CENTER, MAGNOLIA SOUTH BALLROOM, "
            "LOCATED AT 9401 KNIGHT RD, HOUSTON, TX 77045 OR AS DESIGNATED BY THE COMMISSIONER'S OFFICE.",
            ('', '', 'TX', ''),
        ),
        # Legal-description-only doc (Lot/Block, no street address anywhere) —
        # must stay empty; there's genuinely nothing to extract.
        'no_address_legal_only': (
            "Legal Description: LOT 3, BLOCK 1, HOLEMAN ESTATES, AN ADDITION IN HARRIS COUNTY, TEXAS, "
            "ACCORDING TO THE MAP OR PLAT RECORDED IN FILM CODE NO. 704441, MAP RECORDS OF HARRIS COUNTY, TEXAS.",
            ('', '', 'TX', ''),
        ),
    }
    print("--- address (new fallback patterns) ---")
    for nm, (txt, exp) in ADDR_CASES.items():
        got = parse_address(txt)
        print(f"{nm:20} {'OK ' if got == exp else 'FAIL'} got={got} exp={exp}")
