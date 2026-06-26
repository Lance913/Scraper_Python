"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant, Denton, Johnson

Advanced Search with 45-day window. Filter results table by NTS doc types.
Paginate through results to collect all NOTICE/NTS records.
Click each NTS document for sale date.

Doc types confirmed in county portals:
  Bexar/Denton/Tarrant: 'NOTICE' (Notice of Trustee Sale)
  Dallas: May use 'NOTICE OF TRUSTEE SALE' or similar
"""

import re
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper

# Doc type keywords that indicate a foreclosure notice
NTS_KEYWORDS = [
    'NOTICE OF TRUSTEE',
    'NOTICE OF SUBSTITUTE',
    'SUBSTITUTE TRUSTEE',
    'TRUSTEE SALE',
    'NTS',
    'NOTSALE',
]

# "NOTICE" alone matches NTS in Tarrant/Denton/Bexar
# but we also want to avoid "NOTICE OF LIS PENDENS" etc.
NOTICE_EXCLUSIONS = [
    'LIS PENDENS', 'COMPLETION', 'COMMENCEMENT',
    'LIEN', 'CLAIM', 'FILING',
]

def is_nts(doc_type: str) -> bool:
    dt = doc_type.upper().strip()
    # Check exclusions first
    for ex in NOTICE_EXCLUSIONS:
        if ex in dt:
            return False
    # Direct NTS keywords
    for kw in NTS_KEYWORDS:
        if kw in dt:
            return True
    # "NOTICE" alone (Tarrant/Denton/Bexar use this for NTS)
    if dt == 'NOTICE':
        return True
    return False


def to_mddyyyy(iso: str) -> str:
    parts = iso.split('-')
    return f"{parts[1]}/{parts[2]}/{parts[0]}"


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str):
        super().__init__(county_name)
        self.slug     = county_slug
        self.base_url = f"https://{county_slug}.tx.publicsearch.us"

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        # 45 days captures July 7 auction notices (filed May 16 - June 16)
        start_iso = (target_date - timedelta(days=45)).strftime('%Y-%m-%d')
        end_iso   = target_date.strftime('%Y-%m-%d')
        records   = self._playwright_scrape(start_iso, end_iso)
        if records is None:
            records = []
        self.logger.info(f"{self.county}: {len(records)} NTS records")
        return records

    def _playwright_scrape(self, start_iso: str, end_iso: str) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        start_fmt = to_mddyyyy(start_iso)
        end_fmt   = to_mddyyyy(end_iso)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ))
                page = context.new_page()
                page.set_default_timeout(30_000)

                # ── Advanced Search ────────────────────────────────────────
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                page.locator('text=Advanced Search').first.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # ── Fill date range ────────────────────────────────────────
                filled = self._fill_date_range(page, start_fmt, end_fmt)
                self.logger.info(f"{self.county}: date fill → {filled}")

                # ── Try to set Document Type filter ───────────────────────
                self._try_set_doc_type(page)

                # ── Search ─────────────────────────────────────────────────
                for btn_sel in ['button[type="submit"]', 'button:has-text("Search")']:
                    try:
                        btn = page.locator(btn_sel).first
                        if btn.count() > 0:
                            btn.click()
                            break
                    except Exception:
                        pass

                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(3000)

                # ── Collect NTS rows across pages ──────────────────────────
                nts_rows = []
                page_num = 1
                max_pages = 20  # cap to avoid infinite loops

                while page_num <= max_pages:
                    html = page.content()
                    rows, total_rows = self._extract_nts_rows(html)
                    nts_rows.extend(rows)
                    self.logger.info(
                        f"{self.county}: page {page_num} — "
                        f"{total_rows} total rows, {len(rows)} NTS rows found"
                    )

                    # Try to go to next page
                    if not self._go_next_page(page):
                        break
                    page_num += 1

                self.logger.info(f"{self.county}: total NTS rows: {len(nts_rows)}")

                # ── Fetch each NTS document for sale date ──────────────────
                records = []
                for row in nts_rows:
                    rec = self._enrich_with_sale_date(page, row)
                    records.append(rec)

                browser.close()
                return records

        except Exception as exc:
            self.logger.error(f"{self.county}: error: {exc}", exc_info=True)
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fill_date_range(self, page, start_fmt: str, end_fmt: str) -> str:
        pairs = [
            ('input[id*="start" i]',           'input[id*="end" i]'),
            ('input[placeholder="Start date"]', 'input[placeholder="End date"]'),
            ('input[placeholder*="Start" i]',   'input[placeholder*="End" i]'),
            ('input[aria-label*="Start" i]',    'input[aria-label*="End" i]'),
        ]
        for s, e in pairs:
            try:
                if page.locator(s).count() > 0 and page.locator(e).count() > 0:
                    page.fill(s, start_fmt)
                    page.fill(e, end_fmt)
                    page.wait_for_timeout(300)
                    return f"filled '{s}'"
            except Exception:
                pass
        return "no fields filled"

    def _try_set_doc_type(self, page):
        """Attempt to filter by NTS doc type in the Advanced Search form."""
        keywords = ['Notice of Trustee', 'NTS', 'Trustee Sale']
        for kw in keywords:
            try:
                for sel in [
                    'input[id*="docType" i]',
                    'input[placeholder*="document" i]',
                    'input[aria-label*="document type" i]',
                ]:
                    el = page.locator(sel)
                    if el.count() > 0 and el.is_visible():
                        el.fill(kw)
                        page.wait_for_timeout(800)
                        # Click autocomplete option if shown
                        for opt_sel in ['[role="option"]', '.dropdown-item', '[class*="option"]']:
                            opt = page.locator(opt_sel).first
                            if opt.count() > 0 and opt.is_visible():
                                opt.click()
                                self.logger.info(f"{self.county}: selected doc type '{kw}'")
                                return
            except Exception:
                pass

    def _extract_nts_rows(self, html: str) -> Tuple[List[Dict], int]:
        """Parse results table, return (nts_rows, total_rows_on_page)."""
        soup      = BeautifulSoup(html, 'lxml')
        nts_rows  = []
        total     = 0
        all_types = set()

        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers:
                continue

            h  = {v: i for i, v in enumerate(headers)}
            gi = h.get('grantor', -1)
            di = h.get('doc type', -1)
            ri = h.get('recorded date', -1)
            ai = h.get('property address', h.get('legal description', h.get('town', -1)))
            ni = h.get('doc number', h.get('inst number', -1))

            for tr in table.find_all('tr')[1:]:
                cells   = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                tds_raw = tr.find_all('td')
                if not cells or not any(c.strip() for c in cells):
                    continue

                total += 1

                def cell(i):
                    return cells[i].strip() if 0 <= i < len(cells) else ''

                doc_type = cell(di)
                all_types.add(doc_type)

                if not is_nts(doc_type):
                    continue

                grantor  = cell(gi)
                rec_date = cell(ri)
                addr_raw = cell(ai)

                # Get doc link
                doc_link = ''
                if 0 <= ni < len(tds_raw):
                    a = tds_raw[ni].find('a')
                    if a and a.get('href'):
                        href = a['href']
                        doc_link = href if href.startswith('http') else self.base_url + href

                first, last = self.parse_name(grantor) if grantor else ('', '')
                address, city, zip_c = self._split_address(addr_raw)

                nts_rows.append({
                    'first_name': first, 'last_name':  last,
                    'address':    address, 'city': city, 'zip_code': zip_c,
                    'file_date':  self._fmt(rec_date),
                    'doc_type':   doc_type, 'doc_link': doc_link,
                })

        self.logger.info(f"{self.county}: doc types on page: {all_types}")
        return nts_rows, total

    def _go_next_page(self, page) -> bool:
        """Click the Next page button. Returns True if successful."""
        for sel in [
            'button:has-text("Next")',
            'a:has-text("Next")',
            '[aria-label="Next page"]',
            '[class*="next" i]:not([disabled])',
            'li.next:not(.disabled) a',
        ]:
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

    def _enrich_with_sale_date(self, page, row: Dict) -> Dict:
        sale_date = ''
        address   = row.get('address', '')
        city      = row.get('city', '')
        zip_c     = row.get('zip_code', '')

        if row.get('doc_link'):
            try:
                page.goto(row['doc_link'])
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                text = BeautifulSoup(page.content(), 'lxml').get_text(' ', strip=True)
                self.logger.info(f"{self.county}: doc detail: {text[:300]}")

                for pat in [
                    r'Sale\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'first\s+Tuesday.*?(\w+\s+\d{1,2},\s+\d{4})',
                    r'sold\s+on\s+(\w+\s+\d{1,2},\s+\d{4})',
                ]:
                    m = re.search(pat, text, re.I)
                    if m:
                        sale_date = m.group(1).strip()
                        self.logger.info(f"{self.county}: sale date: {sale_date}")
                        break

                if not address:
                    m2 = re.search(
                        r'(\d+\s+[A-Z0-9][A-Z0-9\s]+?'
                        r'(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY|LOOP|HWY)[A-Z\s\.]*?)'
                        r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
                        text, re.I
                    )
                    if m2:
                        address = m2.group(1).strip().title()
                        city    = m2.group(2).strip().title()
                        zip_c   = m2.group(3)

            except Exception as e:
                self.logger.warning(f"{self.county}: detail error: {e}")

        return self.build_record(
            first_name=row.get('first_name', ''), last_name=row.get('last_name', ''),
            address=address, city=city, zip_code=zip_c,
            file_date=row.get('file_date', ''), sale_date=self._fmt(sale_date),
        )

    @staticmethod
    def _split_address(raw: str) -> Tuple[str, str, str]:
        if not raw or raw in ('N/A', ''):
            return '', '', ''
        parts  = [p.strip() for p in raw.split(',')]
        street = parts[0].title() if parts else ''
        city, zip_c = '', ''
        for part in parts[1:]:
            m = re.search(r'\b(\d{5})\b', part)
            if m:
                zip_c = m.group(1)
                continue
            if re.match(r'^(TX|TEXAS)$', part.strip(), re.I):
                continue
            if not city and part.strip():
                city = part.strip().title()
        return street, city, zip_c

    @staticmethod
    def _fmt(raw: str) -> str:
        if not raw:
            return ''
        raw = str(raw).strip()
        if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', raw):
            return raw
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
        if m:
            return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
        return raw
