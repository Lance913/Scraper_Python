"""
Production Harris document extraction: download PDF -> OCR all pages -> parse owner+address.
Returns tiered result; caller falls back to doc_id when owner+address both empty.
"""
import re, logging, io

logger = logging.getLogger('harris_extract')

CITY_STATE_ZIP = re.compile(r'([A-Z][A-Za-z .]+),\s*(TX|TEXAS)\s+(\d{5})(?:-\d{4})?', re.I)
STREET_LINE = re.compile(r'^\s*(\d{3,6}\s+[A-Z0-9][A-Za-z0-9 .#-]{3,40}?)(?:\s+\d{8,})?\s*$')

NON_OWNER_TOKENS = [
    'WELLS FARGO','MORTGAGE','BANK','MERS','NOMINEE','REGISTRATION','SYSTEMS',
    'SERVICER','SERVICING','LLC','TRUSTEE','CORPS','CARRINGTON','SOURCE LOGIC',
    'CISNEROS','N.A.','NATIONAL','STATEVIEW','BLVD','AVENUE','CORPORATION',
    'COMPANY','INC','ADVISORS','CAPITAL','FINANCIAL','HOLDINGS','FARGO',
    'HUDSPETH','COUNTY CLERK','CLERK','TENESHIA','HARRIS COUNTY','TEXAS',
    'RECORDING','RECORDED','BASTAIN','EVENT CENTER','BALLROOM','COMMISSION',
    'LANE','STREET','DRIVE','ROAD','AVENUE','BOULEVARD','COURT','CIRCLE',
    'TRAIL','WAY','PLACE','TERRACE','PARKWAY','EXHIBIT','PROPERTY','DEPICTED',
]
HEADER_NOISE = ['HUDSPETH','COUNTY CLERK','FILED','RECORDING REQUESTED','WHEN RECORDED','TENESHIA']

OWNER_HINTS = [
    re.compile(r'\bon\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4},?\s+([A-Z][A-Z. ]+?)(?:,|\s+A\s+SINGLE|\s+A\s+MARRIED|\s+AS\b|\s+AND\b)'),
    re.compile(r'\b(?:executed by|grantor[:\s]+|borrower[:\s]+|mortgagor[:\s]+)\s*([A-Z][A-Z. ]{4,40})', re.I),
    re.compile(r'\b([A-Z]{3,}\s+[A-Z]{3,}\s+AND\s+HIS\s+WIFE,?\s+[A-Z]{3,})'),
    re.compile(r'\b([A-Z]{3,}\s+[A-Z]{3,}\s+AND\s+HER\s+HUSBAND,?\s+[A-Z]{3,})'),
]
GENERIC_NAME = re.compile(r'\b([A-Z]{3,}\s+(?:[A-Z]\.?\s+)?[A-Z]{3,})\b')


def looks_like_owner(name):
    up = name.upper()
    if any(t in up for t in NON_OWNER_TOKENS): return False
    w = name.split()
    if not (2 <= len(w) <= 6): return False
    return sum(c.isalpha() or c.isspace() or c=='.' for c in name)/max(len(name),1) > 0.85

def clean_name(name):
    name = re.sub(r'\b(A SINGLE PERSON|A MARRIED|AN UNMARRIED|AND HIS WIFE|AND HER HUSBAND|HUSBAND AND WIFE).*$','',name,flags=re.I)
    return re.sub(r'\s+',' ',name).strip(' ,.')

def parse_address(text):
    lines=[l.strip() for l in text.split('\n') if l.strip()]
    city=state=zipc=street=''
    m=CITY_STATE_ZIP.search(text)
    if m:
        city=m.group(1).strip(); state='TX'; zipc=m.group(3)
        for i,ln in enumerate(lines):
            if CITY_STATE_ZIP.search(ln):
                if i>0:
                    sm=STREET_LINE.match(lines[i-1])
                    if sm: street=sm.group(1).strip()
                break
    if not street:
        for ln in lines[:10]:
            if any(h in ln.upper() for h in HEADER_NOISE): continue
            sm=STREET_LINE.match(ln)
            if sm:
                cand=sm.group(1).strip()
                if not any(t in cand.upper() for t in ['KNIGHT','BAYOU','EVENT CENTER','STATEVIEW','GILLETTE']):
                    street=cand; break
    if street and any(t in street.upper() for t in ['STATEVIEW','GILLETTE','KNIGHT','BAYOU']):
        street=''
    return street,city,state,zipc

def parse_owner(text):
    safe=[]
    for ln in text.split('\n'):
        u=ln.upper()
        if any(h in u for h in HEADER_NOISE): continue
        if CITY_STATE_ZIP.search(ln): continue
        if STREET_LINE.match(ln): continue
        safe.append(ln)
    st='\n'.join(safe)
    for rx in OWNER_HINTS:
        for m in rx.finditer(st):
            c=clean_name(m.group(1))
            if looks_like_owner(c): return c
    for m in GENERIC_NAME.finditer(st):
        c=clean_name(m.group(1))
        if looks_like_owner(c): return c
    return ''

def split_owner_name(full):
    """Split 'RICARDO CORREA' -> (first='Ricardo', last='Correa'). Title-case."""
    if not full: return '',''
    parts=full.split()
    if len(parts)==1: return parts[0].title(),''
    first=parts[0].title()
    last=' '.join(parts[1:]).title()
    return first,last

def ocr_pdf_bytes(pdf_bytes, max_pages=3, dpi=200):
    """OCR up to max_pages of a PDF given as bytes. Returns combined text."""
    from pdf2image import convert_from_bytes
    import pytesseract
    text=''
    try:
        imgs=convert_from_bytes(pdf_bytes, dpi=dpi, first_page=1, last_page=max_pages)
        for im in imgs:
            text += pytesseract.image_to_string(im) + '\n'
    except Exception as e:
        logger.warning(f"OCR failed: {e}")
    return text

def extract_from_pdf_bytes(pdf_bytes):
    """Full pipeline. Returns dict with first_name,last_name,address,city,state,zip + flag."""
    text = ocr_pdf_bytes(pdf_bytes, max_pages=3)
    owner = parse_owner(text)
    street,city,state,zipc = parse_address(text)
    first,last = split_owner_name(owner)
    return {
        'first_name':first,'last_name':last,
        'address':street,'city':city.title() if city else '','state':state,'zip_code':zipc,
        'has_data': bool(owner or street),
    }

if __name__=='__main__':
    # Validate parse logic on the saved text samples
    import sys
    sys.path.insert(0,'.')
    from test_samples import SAMPLE_2405, SAMPLE_9391
    for nm,txt in [('2405',SAMPLE_2405),('9391',SAMPLE_9391)]:
        o=parse_owner(txt); s,c,st,z=parse_address(txt)
        f,l=split_owner_name(o)
        print(f"{nm}: first={f!r} last={l!r} addr={s!r} city={c.title()!r} zip={z!r}")
