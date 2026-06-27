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
RE_STREET = re.compile(r'^\s*(\d{2,6}\s+[A-Z0-9][A-Za-z0-9 .#-]{3,40}?)(?:\s+\d{7,})?\s*$')

# --- Owner patterns (label-anchored, highest confidence first) ---
RE_OWNER = [
    re.compile(r'Grantor\(s\)\s*/?\s*Mortgagor\(s\)\s*\n?\s*(?:\d{1,2}/\d{1,2}/\d{4}\s+)?([A-Z][A-Z. ]{4,50}?)(?:\s+HUSBAND|\s+WIFE|\s+A\s+SINGLE|\s*\n|\s*$)', re.I),
    re.compile(r'Trustor\(s\)[:\s]+([A-Z][A-Z. ]{4,40}?)(?:\s+Orig|\s+MORTGAGE|\s*\n|\s*$)', re.I),
    re.compile(r'\bexecuted by\s+([A-Z][A-Z. ]{4,40}?)(?:,?\s+A\s+SINGLE|,?\s+WIFE|,?\s+HUSBAND|,\s*$|\s*\n)', re.I),
    re.compile(r'\bon\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4},?\s+([A-Z][A-Z. ]+?)(?:,|\s+A\s+SINGLE|\s+AS\b|\s+AND\b)'),
    re.compile(r'\b([A-Z]{3,}\s+[A-Z]{3,}\s+AND\s+(?:HIS\s+WIFE|HER\s+HUSBAND|EDNA)[A-Z, ]*)', re.I),
]


def _is_venue(s): return any(t in s.upper() for t in VENUE_TOKENS)

def looks_like_owner(name):
    up = name.upper()
    if any(t in up for t in NON_OWNER_TOKENS): return False
    if _is_venue(name): return False
    w = name.split()
    if not (2 <= len(w) <= 7): return False
    return sum(c.isalpha() or c.isspace() or c=='.' for c in name)/max(len(name),1) > 0.82

def clean_name(name):
    name = re.sub(r'\b(A SINGLE PERSON|A SINGLE MAN|A SINGLE WOMAN|A MARRIED|AN UNMARRIED|HUSBAND AND WIFE|WIFE AND HUSBAND|AND HIS WIFE|AND HER HUSBAND).*$','',name,flags=re.I)
    return re.sub(r'\s+',' ',name).strip(' ,.')

def split_name(full):
    if not full: return '',''
    p = full.split()
    if len(p)==1: return p[0].title(),''
    return p[0].title(), ' '.join(p[1:]).title()


def parse_address(text):
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # 1) explicit "Property Address:" label
    m = RE_PROP_LABEL.search(text)
    if m:
        street = re.sub(r'\s+\d{7,}$','',m.group(1).strip())
        if not _is_venue(street):
            return street.strip(' ,.'), '', 'TX', m.group(2)

    # 2) header block: street line followed by CITY, TX ZIP, in first ~7 lines
    for i in range(min(len(lines), 8)):
        ln = lines[i]
        if any(h in ln.upper() for h in HEADER_NOISE): continue
        if _is_venue(ln): continue
        sm = RE_STREET.match(ln)
        if sm and i+1 < len(lines):
            nxt = lines[i+1]
            cm = RE_CSZ.match(nxt)
            if cm and not _is_venue(nxt):
                street = sm.group(1).strip()
                return street, cm.group(1).strip().title(), 'TX', cm.group(2)

    # 3) any CITY, TX ZIP not a venue, with street on the prior line
    for i, ln in enumerate(lines):
        cm = RE_CSZ.match(ln)
        if cm and not _is_venue(ln):
            city, zipc = cm.group(1).strip().title(), cm.group(2)
            street = ''
            if i > 0:
                sm = RE_STREET.match(lines[i-1])
                if sm and not _is_venue(lines[i-1]):
                    street = sm.group(1).strip()
            return street, city, 'TX', zipc

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
    import sys; sys.path.insert(0,'.')
    from test_samples import (SAMPLE_2405, SAMPLE_9391, SAMPLE_2406_P1,
                              SAMPLE_2395_P1, SAMPLE_2462_P1)
    cases = [('2405',SAMPLE_2405),('9391',SAMPLE_9391),('2406',SAMPLE_2406_P1),
             ('2395',SAMPLE_2395_P1),('2462',SAMPLE_2462_P1)]
    for nm,txt in cases:
        r = extract_from_text(txt)
        tier = 'FULL' if (r['first_name'] and r['address']) else ('PARTIAL' if (r['first_name'] or r['address']) else 'NONE')
        print(f"{nm:6} [{tier:7}] name={r['first_name']+' '+r['last_name']!r:28} addr={r['address']!r}, {r['city']!r}, {r['state']} {r['zip_code']}")
