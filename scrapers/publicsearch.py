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


# JS that parses the FC results table into row dicts (incl. internal docId).
_PARSE_ROWS_JS = """() => {
    const out = []; const t = document.querySelector('table'); if (!t) return out;
    const heads = Array.from(t.querySelectorAll('th')).map(h => (h.textContent||'').trim().toLowerCase());
    const idx = n => heads.findIndex(h => h.includes(n));
    const di=idx('doc type'), rd=idx('recorded'), sd=idx('sale date'),
          dn=idx('doc number'), rm=idx('remark'), pa=idx('property address');
    if (di < 0) return out;
    for (const tr of Array.from(t.querySelectorAll('tr')).slice(1)) {
        const c = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent||'').trim());
        if (!c.length) continue;
        const cb = tr.querySelector('input[id^="table-checkbox-"]');
        out.push({
            doc_type: c[di]||'', recorded: c[rd]||'', sale_date: c[sd]||'',
            doc_number: c[dn]||'', remarks: c[rm]||'', property_address: c[pa]||'',
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
                if not is_nts(r['doc_type']):
                    continue
                sale = self._fmt(r['sale_date'])
                if not self._is_upcoming(sale, target_date):
                    continue
                key = r['doc_number'] or r['doc_id']
                if not key or key in seen_docs:
                    continue
                seen_docs.add(key)
                street, city, zip_c = self._split_address(r['property_address'])
                candidates.append({
                    'doc_id_internal': r['doc_id'],
                    'doc_number': r['doc_number'],
                    'sale_date': sale,
                    'file_date': self._fmt(r['recorded']),
                    'address': street, 'city': city, 'zip_code': zip_c,
                })
                kept += 1
            self.logger.info(f"{self.county}: page {page_num} -> {len(rows)} rows, {kept} upcoming NTS")
            if not self._next_page(page):
                break
        # Soonest sales first, so the most urgent leads get names within budget.
        candidates.sort(key=lambda c: self._sort_key(c['sale_date']))
        return candidates

    def _enrich_and_build(self, page, context, candidates, captured, is_doc_image) -> List[Dict]:
        records: List[Dict] = []
        deadline = time.monotonic() + OCR_BUDGET_SEC
        ocr_done = named = 0
        for cand in candidates:
            first = last = ''
            can_ocr = (cand['doc_id_internal']
                       and ocr_done < OCR_MAX_DOCS
                       and time.monotonic() < deadline)
            if can_ocr:
                ocr_done += 1
                first, last = self._ocr_owner(page, context, cand['doc_id_internal'],
                                              captured, is_doc_image)
                full = f"{first} {last}".strip()
                if full and not is_residential_lead(full):
                    self.logger.info(f"{self.county}: drop entity owner {full!r}")
                    first = last = ''
                elif full:
                    named += 1
            records.append(self.build_record(
                first_name=first, last_name=last,
                address=cand['address'], city=cand['city'],
                state='TX', zip_code=cand['zip_code'],
                file_date=cand['file_date'], sale_date=cand['sale_date'],
                doc_id=cand['doc_number'],
            ))
        self.logger.info(
            f"{self.county}: built {len(records)} records "
            f"({named} with owner name, {len(records) - named} address-only; "
            f"OCR'd {ocr_done})"
        )
        return records

    def _ocr_owner(self, page, context, doc_id_internal: str,
                   captured: List[str], is_doc_image) -> Tuple[str, str]:
        """Open the doc page, grab the page-1 PNG, OCR it, parse the owner name."""
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
                return '', ''
            body = context.request.get(png_url).body()
            return pse.owner_from_png(body)
        except Exception as e:
            self.logger.warning(f"{self.county}: owner OCR error doc {doc_id_internal}: {e}")
            return '', ''

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

    def _is_upcoming(self, sale_date: str, target_date: date) -> bool:
        try:
            return datetime.strptime(sale_date, '%m/%d/%Y').date() >= target_date
        except Exception:
            return False  # no parseable sale date -> skip (can't confirm upcoming)

    @staticmethod
    def _sort_key(sale_date: str):
        try:
            return datetime.strptime(sale_date, '%m/%d/%Y').date()
        except Exception:
            return date.max

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
