"""
PublicSearch.us scraper — Bexar, Dallas, Tarrant, Denton, Johnson.

Design (proven via the probe_publicsearch.py investigation on GitHub Actions):

  * Search the FORECLOSURES department, not Land Records. The results are fully
    query-driven, so we navigate straight to:
        {base}/results?department=FC&recordedDateRange=YYYYMMDD,YYYYMMDD&searchType=advancedSearch
    (after warming up a session at the base host so cookies/signature are set).

  * The Foreclosures results table is server-rendered HTML with columns:
        Doc Type | Recorded Date | Sale Date | Doc Number | Remarks | Property Address
    so the property ADDRESS, sale date, recorded (file) date and doc number all
    come straight from the table — NO OCR needed for those. Each row also carries
    the internal docId in `input id="table-checkbox-{docId}"`, and the doc detail
    page is {base}/doc/{docId}.

  * The owner NAME is the ONLY field not available without OCR (the doc summary
    shows "Parties: No parties found." — the county doesn't index grantor/grantee
    for foreclosures). So we OCR page 1 of each upcoming doc and parse the owner
    with publicsearch_extract. To stay within the workflow timeout, name OCR is
    bounded by a per-county time budget + doc cap; rows we don't get to are still
    written with their address (a name OR an address is enough for skip-trace).

  * doc_type in this department is "NOTICE OF FORECLOSE" (plus a few "VOID FC"
    which we exclude).
"""
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .base import BaseScraper
from . import publicsearch_extract as pse

# ── Tunables (env-overridable so the workflow can trade runtime for coverage) ──
WINDOW_DAYS = int(os.environ.get('PUBLICSEARCH_WINDOW_DAYS', '60'))
MAX_PAGES = int(os.environ.get('PUBLICSEARCH_MAX_PAGES', '40'))      # 50 rows/page
OCR_BUDGET_SEC = int(os.environ.get('PUBLICSEARCH_OCR_BUDGET', '240'))  # per county
OCR_MAX_DOCS = int(os.environ.get('PUBLICSEARCH_OCR_MAX', '150'))    # per county
IMG_WAIT_MS = int(os.environ.get('PUBLICSEARCH_IMG_WAIT', '7000'))   # max wait for PNG

# Builders / entities we never want as a lead (checked against the OCR'd name).
EXCLUDE_KEYWORDS = [
    'D R HORTON', 'DR HORTON', 'LENNAR', 'KB HOME', 'MERITAGE', 'PULTE', 'CENTEX',
    'TAYLOR MORRISON', 'STARLIGHT', 'CONTINENTAL HOMES', 'BEAZER', 'CHESMAR',
    'M/I HOMES', 'COVENTRY', 'COUTO HOMES', 'LGI HOMES', 'PURCHASING FUND',
    'LLC', 'L.L.C', 'INC', 'CORPORATION', 'COMPANY', 'PARTNERSHIP', 'LTD',
    'ASSOCIATION', 'PROPERTIES', 'INVESTMENTS', 'HOLDINGS',
]


def is_residential_lead(name: str) -> bool:
    """True if an OCR'd owner name looks like an individual, not an entity."""
    if not name:
        return True  # unknown name -> keep the address lead
    g = name.upper()
    return not any(ex in g for ex in EXCLUDE_KEYWORDS)


# Foreclosures-department doc types that ARE notices of (trustee) sale.
_EXCLUDE_DT = ('VOID', 'RESCISS', 'RESCIND', 'CANCEL', 'RELEASE', 'WITHDRAW')


def is_nts(doc_type: str) -> bool:
    dt = (doc_type or '').upper()
    if any(x in dt for x in _EXCLUDE_DT):
        return False
    return 'FORECLOS' in dt or 'TRUSTEE' in dt


# JS that parses the FC results table into row dicts, tolerant of the two known
# column schemas:
#   A (Bexar/Dallas/Denton/Johnson): Doc Type | Recorded Date | Sale Date |
#                                    Doc Number | Remarks | Property Address
#   B (Tarrant):                     Grantor | Sale Date | Filed Date | Property Address
# Columns are matched by header name; missing ones come back as ''.
_PARSE_ROWS_JS = """() => {
    const out = []; const t = document.querySelector('table'); if (!t) return out;
    const heads = Array.from(t.querySelectorAll('th')).map(h => (h.textContent||'').trim().toLowerCase());
    const idx = (...names) => { for (const n of names) { const i = heads.findIndex(h => h.includes(n)); if (i >= 0) return i; } return -1; };
    const gi=idx('grantor'), di=idx('doc type'), rd=idx('recorded','filed'),
          sd=idx('sale date'), dn=idx('doc number','instrument'),
          rm=idx('remark'), pa=idx('property address','legal');
    const get = (c, i) => (i >= 0 && i < c.length) ? c[i] : '';
    for (const tr of Array.from(t.querySelectorAll('tr')).slice(1)) {
        const c = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent||'').trim());
        if (!c.length) continue;
        const cb = tr.querySelector('input[id^="table-checkbox-"]');
        out.push({
            grantor: get(c, gi), doc_type: get(c, di), recorded: get(c, rd),
            sale_date: get(c, sd), doc_number: get(c, dn), remarks: get(c, rm),
            property_address: get(c, pa),
            doc_id: cb ? cb.id.replace('table-checkbox-','') : '',
        });
    }
    return out;
}"""


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str):
        super().__init__(county_name)
        self.slug = county_slug
        self.base_url = f"https://{county_slug}.tx.publicsearch.us"

    # ── Entry point ───────────────────────────────────────────────────────────

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        records = self._playwright_scrape(target_date)
        if records is None:
            records = []
        self.logger.info(f"{self.county}: {len(records)} NTS records")
        return records

    # ── Core ──────────────────────────────────────────────────────────────────

    def _playwright_scrape(self, target_date: date) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error(f"{self.county}: playwright not installed")
            return None

        start = (target_date - timedelta(days=WINDOW_DAYS)).strftime('%Y%m%d')
        end = target_date.strftime('%Y%m%d')
        results_url = (f"{self.base_url}/results?department=FC"
                       f"&recordedDateRange={start},{end}&searchType=advancedSearch")

        captured: List[str] = []

        def is_doc_image(u: str) -> bool:
            return '/files/documents/' in u and '/images/' in u and '.png' in u

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled'],
                )
                context = browser.new_context(user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                ))
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page.set_default_timeout(30_000)
                page.on('response',
                        lambda r: captured.append(r.url) if is_doc_image(r.url) else None)

                # Warm a session, then go straight to the Foreclosures results.
                self.logger.info(f"{self.county}: warming session...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(800)
                self.logger.info(f"{self.county}: FC results -> {results_url}")
                page.goto(results_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(3500)

                # 1) Collect every upcoming NTS row across all result pages.
                candidates = self._collect_candidates(page, target_date)
                self.logger.info(
                    f"{self.county}: {len(candidates)} upcoming NTS rows "
                    f"(name OCR budget {OCR_BUDGET_SEC}s / {OCR_MAX_DOCS} docs)"
                )

                # 2) Enrich with owner name via bounded OCR; build records.
                records = self._enrich_and_build(
                    page, context, candidates, captured, is_doc_image
                )

                browser.close()
                return records

        except Exception as exc:
            self.logger.error(f"{self.county}: error: {exc}", exc_info=True)
            return None

    def _collect_candidates(self, page, target_date: date) -> List[Dict]:
        candidates: List[Dict] = []
        seen_docs = set()
        for page_num in range(1, MAX_PAGES + 1):
            rows = page.evaluate(_PARSE_ROWS_JS)
            kept = 0
            for r in rows:
                # Doc-type filter only applies to schemas that have the column;
                # in the FC department a row with no doc type is still a foreclosure.
                if r['doc_type'] and not is_nts(r['doc_type']):
                    continue

                # Name from the table grantor when present (Tarrant: "LAST FIRST").
                first = last = ''
                if r['grantor']:
                    if not is_residential_lead(r['grantor']):
                        continue  # builder / HOA / LLC
                    first, last = self.parse_table_grantor(r['grantor'])

                sale_disp, sale_cmp, coarse = self._sale_info(r['sale_date'])
                if not self._is_upcoming(sale_cmp, coarse, target_date):
                    continue

                key = r['doc_number'] or r['doc_id']
                if not key or key in seen_docs:
                    continue
                seen_docs.add(key)

                street, city, zip_c = self._table_address(r['property_address'])
                candidates.append({
                    'doc_id_internal': r['doc_id'],
                    'doc_number': r['doc_number'] or r['doc_id'],
                    'first': first, 'last': last,
                    'sale_date': sale_disp,
                    'sale_cmp': sale_cmp,
                    'file_date': self._fmt(r['recorded']),
                    'address': street, 'city': city, 'zip_code': zip_c,
                })
                kept += 1
            self.logger.info(f"{self.county}: page {page_num} -> {len(rows)} rows, {kept} upcoming NTS")
            if not self._next_page(page):
                break
        # Soonest sales first, so the most urgent leads get OCR'd within budget.
        candidates.sort(key=lambda c: c['sale_cmp'] or date.max)
        return candidates

    def _enrich_and_build(self, page, context, candidates, captured, is_doc_image) -> List[Dict]:
        records: List[Dict] = []
        deadline = time.monotonic() + OCR_BUDGET_SEC
        ocr_done = named = addressed = 0
        for cand in candidates:
            first, last = cand['first'], cand['last']          # from table grantor, if any
            address, city, zip_c = cand['address'], cand['city'], cand['zip_code']
            # Drop courthouse/clerk/commercial addresses from the table.
            if address and pse.is_nonproperty_address(address):
                address = city = zip_c = ''
            # OCR only to fill what the table didn't give us (name and/or address).
            needs = (not (first or last)) or (not address)
            can_ocr = (needs and cand['doc_id_internal']
                       and ocr_done < OCR_MAX_DOCS
                       and time.monotonic() < deadline)
            if can_ocr:
                ocr_done += 1
                o_first, o_last, o_street, o_city, o_zip = self._ocr_doc(
                    page, context, cand['doc_id_internal'], captured, is_doc_image)
                if not (first or last):
                    full = f"{o_first} {o_last}".strip()
                    if full and not is_residential_lead(full):
                        self.logger.info(f"{self.county}: drop entity owner {full!r}")
                    elif full:
                        first, last = o_first, o_last
                if not address and o_street and not pse.is_nonproperty_address(o_street):
                    address, city, zip_c = o_street, o_city, o_zip
            if first or last:
                named += 1
            if address:
                addressed += 1
            records.append(self.build_record(
                first_name=first, last_name=last,
                address=address, city=city, state='TX', zip_code=zip_c,
                file_date=cand['file_date'], sale_date=cand['sale_date'],
                doc_id=cand['doc_number'],
            ))
        self.logger.info(
            f"{self.county}: built {len(records)} records "
            f"({named} with name, {addressed} with address; OCR'd {ocr_done})"
        )
        return records

    def _ocr_doc(self, page, context, doc_id_internal: str,
                 captured: List[str], is_doc_image) -> Tuple[str, str, str, str, str]:
        """Open the doc page, grab the page-1 PNG, OCR it, parse owner + address."""
        try:
            captured.clear()
            page.goto(f"{self.base_url}/doc/{doc_id_internal}", wait_until='domcontentloaded')
            # Break as soon as the page-1 PNG response arrives (don't burn a fixed wait).
            deadline = time.monotonic() + IMG_WAIT_MS / 1000
            while time.monotonic() < deadline:
                if any(is_doc_image(u) for u in captured):
                    break
                page.wait_for_timeout(250)
            png_url = next((u for u in captured if is_doc_image(u)), None)
            if not png_url:
                return '', '', '', '', ''
            body = context.request.get(png_url).body()
            return pse.address_and_owner_from_png(body)
        except Exception as e:
            self.logger.warning(f"{self.county}: OCR error doc {doc_id_internal}: {e}")
            return '', '', '', '', ''

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page(self, page) -> bool:
        for sel in ['[aria-label="next page"]', 'button:has-text("Next")',
                    'a:has-text("Next")', '[aria-label="Next"]']:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible() and el.is_enabled():
                    el.click()
                    page.wait_for_load_state('networkidle')
                    page.wait_for_timeout(2000)
                    return True
            except Exception:
                pass
        return False

    # ── Utilities ─────────────────────────────────────────────────────────────

    _MONTHS = {m: i for i, m in enumerate(
        ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
         'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}

    def _sale_info(self, raw: str) -> Tuple[str, Optional[date], bool]:
        """Parse a sale date. Returns (display, comparable_date, is_coarse).

        Handles precise 'mm/dd/yyyy' and Tarrant's coarse 'Jul 2026' (month only)."""
        raw = (raw or '').strip()
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', raw)
        if m:
            mm, dd, yy = (int(x) for x in m.groups())
            try:
                return f"{mm:02d}/{dd:02d}/{yy}", date(yy, mm, dd), False
            except ValueError:
                return raw, None, False
        m = re.match(r'^([A-Za-z]{3,9})\.?\s+(\d{4})$', raw)
        if m:
            mon = self._MONTHS.get(m.group(1)[:3].lower())
            if mon:
                yy = int(m.group(2))
                # Display as "Jul 2026"; compare on the first of that month.
                return f"{m.group(1).title()[:3]} {yy}", date(yy, mon, 1), True
        return raw, None, False

    def _is_upcoming(self, sale_cmp: Optional[date], coarse: bool, target_date: date) -> bool:
        if sale_cmp is None:
            return False  # unparseable sale date -> can't confirm upcoming
        if coarse:
            # Month-only: keep current month and later (can't tell the exact day).
            return (sale_cmp.year, sale_cmp.month) >= (target_date.year, target_date.month)
        return sale_cmp >= target_date

    @staticmethod
    def parse_table_grantor(g: str) -> Tuple[str, str]:
        """Tarrant grantor is 'LASTNAME FIRSTNAME [MIDDLE]' -> (first, last)."""
        g = re.sub(r'\s+(ET\s+AL|ET\s+UX|AND\b|&).*$', '', g, flags=re.I).strip(' ,.')
        parts = [p for p in g.split() if p]
        if not parts:
            return '', ''
        if len(parts) == 1:
            return '', parts[0].title()
        return parts[1].title(), parts[0].title()

    def _table_address(self, raw: str) -> Tuple[str, str, str]:
        """Parse a table Property Address. Returns ('','','') for a legal
        description (LOT/BLOCK/no street number) — those get an OCR fallback."""
        raw = (raw or '').strip()
        if not raw or raw.upper() in ('N/A', ''):
            return '', '', ''
        # Legal descriptions ("LOT 14 BLOCK 4 ...", "BEING LOT 1 ...") aren't a street.
        if not re.match(r'^\d{1,6}\s+\S', raw) or re.search(r'\b(LOT|BLOCK|ABST|TRACT)\b', raw, re.I):
            return '', '', ''
        return self._split_address(raw)

    @staticmethod
    def _split_address(raw: str) -> Tuple[str, str, str]:
        """'8914 ARABIAN KING, CONVERSE, TEXAS, 78109' -> ('8914 Arabian King','Converse','78109')."""
        if not raw or raw.strip() in ('N/A', ''):
            return '', '', ''
        parts = [p.strip() for p in raw.split(',') if p.strip()]
        street = parts[0].title() if parts else ''
        city, zip_c = '', ''
        for part in parts[1:]:
            m = re.search(r'\b(\d{5})\b', part)
            if m:
                zip_c = m.group(1)
                continue
            if re.match(r'^(TX|TEXAS)$', part, re.I):
                continue
            if not city:
                city = part.title()
        return street, city, zip_c

    @staticmethod
    def _fmt(raw: str) -> str:
        if not raw:
            return ''
        raw = str(raw).strip()
        if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', raw):
            mm, dd, yy = raw.split('/')
            return f"{int(mm):02d}/{int(dd):02d}/{yy}"
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
        if m:
            return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
        return raw
