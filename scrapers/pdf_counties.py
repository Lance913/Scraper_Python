"""Foreclosure notices posted as PDFs (2026-10-28): Williamson, Fort Bend, Comal.

  Williamson: apps.wilco.org/countyclerk/trustee_sales/<Month>/files.aspx, one PDF per notice.
              When the clerk posts an index (File_IDX.pdf, typed table) we read owner/address from it;
              notices not on the index are OCR'd.
  Fort Bend:  one scanned PDF per sale month (Month-YYYY.pdf, ~300 pages) -> OCR every page.
  Comal:      one scanned PDF per sale date, linked from comalcounty.gov/213/Foreclosure-Sales -> OCR.

Only notices for upcoming sales (sale date >= today). Doc IDs are stable so the daily run only
reads notices that are not in the sheet yet (Williamson) and the sheet dedups on Doc ID.
"""
import calendar, io, os, re, subprocess, tempfile
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from .base import BaseScraper, HEADERS
from . import harris_extract as hx

MONTHS = list(calendar.month_name)[1:]


def sale_day(y: int, m: int) -> date:
    """First Tuesday; Wednesday if that Tuesday is Jan 1 or Jul 4 (HB 1128)."""
    d = date(y, m, 1)
    d = d.replace(day=1 + (1 - d.weekday()) % 7)
    if (m, d.day) in ((1, 1), (7, 4)):
        d = d.replace(day=d.day + 1)
    return d


def upcoming_months(today: date, n: int = 3):
    y, m, out = today.year, today.month, []
    while len(out) < n:
        if sale_day(y, m) >= today:
            out.append((y, m))
        m += 1
        if m > 12: y, m = y + 1, 1
    return out


# ---------------- text extraction shared by the 3 counties ----------------
_BLOB = (r'(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .\'#-]{2,45}?)'
         r'\s*,?\s+([A-Za-z][A-Za-z .\'-]{2,25}?)\s*,?\s*(?:TX|TEXAS)\s*,?\s*(\d{5})')
RE_LABELED = re.compile(r'(?:property\s+address|commonly\s+known\s+as|street\s+address|'
                        r'address\s+of\s+the\s+property|known\s+as)\s*(?:is|:|-)?\s*' + _BLOB, re.I)
RE_ANY = re.compile(_BLOB, re.I)
RE_OWNER_EXTRA = [
    re.compile(r'\bwith\s+([A-Z][A-Za-z.\' ]{4,50}?)(?:\s+and\s+[^()]{3,60}?)?,?\s*\(grantor', re.I),
    re.compile(r'\bexecuted\s+by\s+([A-Z][A-Za-z.\' ]{4,50}?)(?:\s+and\s+[A-Z][A-Za-z.\' ]{3,40}?)?\s*,', re.I),
    re.compile(r'\bGrantor(?:\(s\))?\s*[:\-]\s*([A-Z][A-Za-z. ]{4,50}?)(?:\s+and\s+([A-Z][A-Za-z. ]{3,40}?))?\s*(?:,|\n|$)', re.I),
    re.compile(r'\bMortgagor(?:\(s\))?\s*[:\-]\s*([A-Z][A-Za-z. ]{4,50}?)\s*(?:,|\n|$)', re.I),
    re.compile(r'\bBorrower(?:\(s\))?\s*[:\-]\s*([A-Z][A-Za-z. ]{4,50}?)\s*(?:,|\n|$)', re.I),
    re.compile(r'\bMaker\s*[:\-]\s*([A-Z][A-Za-z. ]{4,50}?)\s*(?:,|\n|$)', re.I),
]
RE_FILED = re.compile(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+(\d{1,2})\s*,?\s*(20\d\d)', re.I)


def _fix_city(street, city, cities):
    """'525 Dennis' + 'Dr Round Rock' -> ('525 Dennis Dr', 'Round Rock') using the county's city list."""
    full = f'{street} {city}'.strip()
    for c in sorted(cities, key=len, reverse=True):
        if full.upper().endswith(' ' + c):
            return full[:-len(c)].strip(' ,'), c.title()
    return street, city.title()


RE_FOOTER = re.compile(r'Page\s+\d+\s+(\d{1,6}\s+[A-Za-z][A-Za-z0-9 .\'#-]{3,50})$', re.M)


def extract(text: str, zip_ok, cities=()) -> Dict:
    street = city = zipc = ''
    for rx in (RE_LABELED, RE_ANY):
        for m in rx.finditer(text):
            s, c, z = m.group(1).strip(' ,.-'), m.group(2).strip(' ,.'), m.group(3)
            if not zip_ok(z) or hx._addr_bad_ctx(text, m.start(), m.end()):
                continue
            if re.search(r'courthouse|patton|heimann|academy|p\.?\s?o\.?\s*box', s, re.I):
                continue
            street, city, zipc = _fix_city(s, c, cities), '', z
            street, city = street
            break
        if street: break
    if not street:  # header line "525 Dennis Dr Round Rock TX 78664" (no commas)
        for m in re.finditer(r'(\d{1,6}\s+[A-Za-z0-9 .\'#-]{3,60}?)\s+(?:TX|TEXAS)\s+(\d{5})', text):
            s, z = m.group(1).strip(), m.group(2)
            if not zip_ok(z) or hx._addr_bad_ctx(text, m.start(), m.end()): continue
            for c in sorted(cities, key=len, reverse=True):
                if s.upper().endswith(' ' + c):
                    street, city, zipc = s[:-len(c)].strip(' ,'), c.title(), z; break
            if street: break
    if not street:  # footer "NOTICE OF TRUSTEE'S SALE Page 1 250 Misty Brook Spring Branch" (no zip)
        for m in RE_FOOTER.finditer(text):
            st, ct = _fix_city(m.group(1).strip(), '', cities)
            if ct: street, city = st, ct; break
    owner = ''
    for rx in RE_OWNER_EXTRA + hx.RE_OWNER:
        for m in rx.finditer(text):
            c = hx.clean_name(m.group(1))
            if hx.looks_like_owner(c):
                owner = c; break
        if owner: break
    first, last = hx.split_name(owner)
    fm = RE_FILED.search(text[:1500])
    fdate = ''
    if fm:
        try: fdate = datetime.strptime(f'{fm.group(1)[:3]} {fm.group(2)} {fm.group(3)}', '%b %d %Y').strftime('%m/%d/%Y')
        except ValueError: pass
    return dict(first_name=first, last_name=last, address=street, city=city, zip_code=zipc, file_date=fdate)


def ocr_pages(pdf_path: str, first: int = 1, last: Optional[int] = None, dpi: int = 200) -> List[str]:
    """OCR page by page (keeps memory flat on 300-page scans)."""
    n = int(re.search(r'Pages:\s*(\d+)', subprocess.run(['pdfinfo', pdf_path], capture_output=True, text=True).stdout).group(1))
    last = min(last or n, n)
    out = []
    with tempfile.TemporaryDirectory() as td:
        for p in range(first, last + 1):
            base = os.path.join(td, 'p')
            subprocess.run(['pdftoppm', '-r', str(dpi), '-f', str(p), '-l', str(p), '-gray', '-png', pdf_path, base], check=False)
            imgs = [f for f in os.listdir(td) if f.endswith('.png')]
            t = ''
            if imgs:
                img = os.path.join(td, imgs[0])
                t = subprocess.run(['tesseract', img, '-', '--psm', '3'], capture_output=True, text=True).stdout
                os.remove(img)
            out.append(t)
    return out


RE_NOTICE_START = re.compile(r'NOTICE\s+OF\s+(?:\[?SUBSTITUTE\]?\s+)?TRUSTEE|NOTICE\s+OF\s+(?:FORECLOSURE|SALE)|'
                             r'NOTICE\s+OF\s+CONFIDENTIALITY|ARMED\s+FORCES|MILITARY\s+DUTY', re.I)
RE_CONTINUED = re.compile(r'\bPage\s+([2-9]|1\d)\s+(?:of|/)\s+\d', re.I)


def split_notices(pages: List[str]):
    """Group the pages of a combined scan into notices: [(first_page_no, text), ...]."""
    groups = []
    for i, t in enumerate(pages, 1):
        head = t[:900]
        new = bool(RE_NOTICE_START.search(head)) and not RE_CONTINUED.search(head)
        if new or not groups:
            groups.append([i, t])
        else:
            groups[-1][1] += '\n' + t
    return groups


def sheet_doc_ids(prefix: str) -> set:
    """Doc IDs with this prefix already in the leads sheet (empty if the sheet can't be read)."""
    try:
        if not os.environ.get('GOOGLE_CREDENTIALS'): return set()
        import sheets_writer
        ws = sheets_writer._get_client().open_by_key(sheets_writer.SPREADSHEET_ID).sheet1
        hdr = ws.row_values(1)
        return {v.strip() for v in ws.col_values(hdr.index('Doc ID') + 1)[1:] if v.strip().startswith(prefix)}
    except Exception as e:
        print(f'could not read sheet doc IDs: {e}'); return set()


def _get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=kw.pop('timeout', 180), **kw)
    r.raise_for_status()
    return r


def _ann(msg):
    print(f'::notice::{msg}', flush=True)


# ---------------- Williamson ----------------
WILCO = 'https://apps.wilco.org/countyclerk/trustee_sales/'
WILCO_ZIP = lambda z: z[:3] in ('786', '787', '765')
WILCO_CITIES = ['ROUND ROCK', 'CEDAR PARK', 'LEANDER', 'GEORGETOWN', 'HUTTO', 'JARRELL', 'TAYLOR',
                'LIBERTY HILL', 'AUSTIN', 'FLORENCE', 'GRANGER', 'THRALL', 'COUPLAND', 'BARTLETT', 'WEIR', 'PFLUGERVILLE']


class WilliamsonForeclosureScraper(BaseScraper):
    def __init__(self):
        super().__init__('Williamson')

    def scrape(self, target_date: date) -> List[Dict]:
        known = sheet_doc_ids('WILCO ')
        recs = []
        for y, m in upcoming_months(target_date):
            mon = MONTHS[m - 1]
            sd = sale_day(y, m).strftime('%m/%d/%Y')
            try:
                html = _get(WILCO + f'{mon}/files.aspx', timeout=60).text
            except Exception as e:
                self.logger.warning(f'Williamson {mon}: {e}'); continue
            files = re.findall(r'HREF="([^"]+\.pdf)"', html, re.I)
            # folder names have no year: keep files created within ~5 months before the sale
            def fdate(fn):
                mm = re.match(r'(\d\d)-(\d\d)-(\d{4})_', fn)
                return date(int(mm.group(3)), int(mm.group(1)), int(mm.group(2))) if mm else None
            files = [f for f in files if fdate(f) and 0 <= (sale_day(y, m) - fdate(f)).days <= 150]
            idx = {}
            for f in [f for f in files if 'IDX' in f.upper()]:
                try:
                    idx.update(self._read_index(_get(WILCO + f'{mon}/{f}').content))
                except Exception as e:
                    self.logger.warning(f'Williamson index {f}: {e}')
            notices = [f for f in files if 'IDX' not in f.upper()]
            from_idx = ocrd = 0
            for f in notices:
                doc = 'WILCO ' + f[:-4]
                if doc in known: continue
                rec = dict(doc_id=doc, sale_date=sd, file_date=fdate(f).strftime('%m/%d/%Y'))
                row = idx.get(f)
                if row and row.get('address'):
                    rec.update(row); from_idx += 1
                else:
                    try:
                        with tempfile.NamedTemporaryFile(suffix='.pdf') as tf:
                            tf.write(_get(WILCO + f'{mon}/{f}', timeout=90).content); tf.flush()
                            x = extract('\n'.join(ocr_pages(tf.name, 1, 3)), WILCO_ZIP, WILCO_CITIES)
                        x.pop('file_date', None)
                        if row and row.get('last_name') and not x.get('last_name'):
                            x['last_name'] = row['last_name']
                        rec.update(x); ocrd += 1
                    except Exception as e:
                        self.logger.warning(f'Williamson OCR {f}: {e}')
                recs.append(self.build_record(**rec))
            _ann(f'Williamson {mon} {y} (sale {sd}): {len(notices)} notices, {from_idx} from index, {ocrd} OCR, '
                 f'{len(notices) - from_idx - ocrd} already in sheet')
        return recs

    @staticmethod
    def _read_index(pdf_bytes) -> Dict[str, Dict]:
        """Index table: ENTRY | OWNER'S LAST NAME | FILE NO | CREATION DATE | ADDRESS | CITY | LEGAL.
        Maps to the notice file '<MM-DD-YYYY>_File_<NNN>.pdf'."""
        with tempfile.NamedTemporaryFile(suffix='.pdf') as tf:
            tf.write(pdf_bytes); tf.flush()
            txt = subprocess.run(['pdftotext', '-layout', tf.name, '-'], capture_output=True, text=True).stdout
        out = {}
        city_rx = '|'.join(sorted(WILCO_CITIES + ['NO CITY'], key=len, reverse=True))
        rx = re.compile(r'^\s*\d+\s+(.+?)\s{2,}(\d{1,3})\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+(.+?)\s{2,}(' + city_rx + r')\b', re.M)
        for m in rx.finditer(txt):
            owner, no, mo, dy, yr, addr, city = m.groups()
            fn = f'{int(mo):02d}-{int(dy):02d}-{yr}_File_{int(no):03d}.pdf'
            addr = addr.strip()
            last = re.split(r'\s*/\s*', owner.strip())[0].title()
            out[fn] = dict(last_name=last, address='' if addr.upper().startswith('NO ADDRESS') else addr.title(),
                           city='' if city == 'NO CITY' else city.title())
        return out


# ---------------- Fort Bend ----------------
FB = ('https://www.fortbendcountytx.gov/sites/default/files/document-central/document-central/'
      'county-clerk-documents/home-page-county-clerk/research-foreclosures/{mon}-{y}.pdf')
FB_ZIP = lambda z: z[:3] in ('774', '775', '770')
FB_CITIES = ['RICHMOND', 'ROSENBERG', 'SUGAR LAND', 'MISSOURI CITY', 'STAFFORD', 'KATY', 'FULSHEAR', 'NEEDVILLE',
             'FRESNO', 'HOUSTON', 'SIMONTON', 'BEASLEY', 'ARCOLA', 'THOMPSONS', 'GUY', 'ORCHARD', 'KENDLETON', 'MEADOWS PLACE']


class _CombinedScanScraper(BaseScraper):
    """A county that posts one big scanned PDF per sale: OCR it, split into notices."""
    prefix, zip_ok, cities = '', None, []

    def _pdfs(self, target_date):  # -> [(url, label, sale_date)]
        raise NotImplementedError

    def scrape(self, target_date: date) -> List[Dict]:
        recs = []
        for url, label, sd in self._pdfs(target_date):
            try:
                r = _get(url, timeout=600)
            except Exception as e:
                self.logger.info(f'{self.county} {label}: not posted ({e})'); continue
            if 'pdf' not in r.headers.get('content-type', '').lower():
                self.logger.info(f'{self.county} {label}: not a PDF'); continue
            with tempfile.NamedTemporaryFile(suffix='.pdf') as tf:
                tf.write(r.content); tf.flush()
                pages = ocr_pages(tf.name)
            seen, n_addr = set(), 0
            groups = split_notices(pages)
            for first_page, text in groups:
                x = extract(text[:12000], type(self).zip_ok, self.cities)
                if not x['address'] and not x['last_name']:
                    continue
                key = x['address'].upper() or f"{x['last_name']}|{first_page}"
                if key in seen: continue
                seen.add(key); n_addr += bool(x['address'])
                recs.append(self.build_record(doc_id=f'{self.prefix} {label} p{first_page}', sale_date=sd, **x))
            _ann(f'{self.county} {label}: {len(pages)} pages, {len(groups)} notices, {len(seen)} kept, {n_addr} with address')
        return recs


class FortBendForeclosureScraper(_CombinedScanScraper):
    prefix, zip_ok, cities = 'FTBEND', staticmethod(FB_ZIP), FB_CITIES

    def __init__(self):
        super().__init__('Fort Bend')

    def _pdfs(self, target_date):
        return [(FB.format(mon=MONTHS[m - 1], y=y), f'{y}-{m:02d}', sale_day(y, m).strftime('%m/%d/%Y'))
                for y, m in upcoming_months(target_date, 2)]


# ---------------- Comal ----------------
COMAL_PAGE = 'https://www.comalcounty.gov/213/Foreclosure-Sales'
COMAL_ZIP = lambda z: z[:3] in ('780', '781', '786')
COMAL_CITIES = ['NEW BRAUNFELS', 'CANYON LAKE', 'SPRING BRANCH', 'BULVERDE', 'GARDEN RIDGE', 'SCHERTZ',
                'SAN ANTONIO', 'FISCHER', 'STARTZVILLE', 'SATTLER', 'SMITHSON VALLEY', 'SELMA', 'CIBOLO']


class ComalForeclosureScraper(_CombinedScanScraper):
    prefix, zip_ok, cities = 'COMAL', staticmethod(COMAL_ZIP), COMAL_CITIES

    def __init__(self):
        super().__init__('Comal')

    def _pdfs(self, target_date):
        html = _get(COMAL_PAGE, timeout=60).text
        out = []
        for href in sorted(set(re.findall(r'href="([^"]*DocumentCenter/View/\d+/[^"]*Foreclosure-Sales[^"]*)"', html, re.I))):
            m = re.search(r'/(' + '|'.join(MONTHS) + r')-(\d{1,2})', href)
            if not m: continue
            mo = MONTHS.index(m.group(1)) + 1
            y = target_date.year + (1 if mo < target_date.month - 6 else 0)
            sd = date(y, mo, int(m.group(2)))
            if sd < target_date: continue
            url = href if href.startswith('http') else 'https://www.comalcounty.gov' + href
            out.append((url, f'{y}-{mo:02d}', sd.strftime('%m/%d/%Y')))
        return out
