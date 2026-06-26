"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant, Denton, Johnson

What works:
  - Advanced Search with 45-day date range
  - Table parsing filtered by NOTICE/NTS doc types
  - Playwright row-click to navigate to document detail for sale date
    (links are React click handlers, not <a href> tags)

Entity filter removes non-residential grantors.
"""

import re
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper

# Homebuilders and non-residential entities to exclude
EXCLUDE_KEYWORDS = [
    'GROUNDWATER', 'CONSERVATION DISTRICT', 'WATER DISTRICT',
    'MUNICIPALITY', 'COUNTY DISTRICT', 'INTERNAL REVENUE',
    'D R HORTON', 'DR HORTON', 'LENNAR HOMES', 'KB HOME',
    'MERITAGE', 'PULTE', 'CENTEX', 'TAYLOR MORRISON',
    'STARLIGHT HOMES', 'CONTINENTAL HOMES', 'BEAZER HOMES',
    'CHESMAR HOMES', 'M/I HOMES', 'COVENTRY',
]

def is_residential_lead(grantor: str) -> bool:
    g = grantor.upper()
    for ex in EXCLUDE_KEYWORDS:
        if ex in g:
            return False
    return True

NTS_DOC_TYPES = {
    'NOTICE OF TRUSTEE', 'NOTICE OF SUBSTITUTE',
    'SUBSTITUTE TRUSTEE', 'TRUSTEE SALE', 'NTS',
}
NOTICE_EXCLUSIONS = {
    'LIS PENDENS', 'HOSPITAL', 'COMPLETION',
    'COMMENCEMENT', 'LIEN', 'CLAIM',
}

def is_nts(doc_type: str) -> bool:
    dt = doc_type.upper().strip()
    for kw in NTS_DOC_TYPES:
        if kw in dt:
            return True
    if 'NOTICE' in dt:
        for ex in NOTICE_EXCLUSIONS:
            if ex in dt:
                return False
        if dt == 'NOTICE' or 'TRUSTEE' in dt or 'SUBSTITUTE' in dt:
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
        records = self._playwright_scrape(target_date)
        if records is None:
            records = []
        self.logger.info(f"{self.county}: {len(records)} NTS records")
        return records

    def _playwright_scrape(self, target_date: date) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        start_fmt = (target_date - timedelta(days=45)).strftime('%m/%d/%Y')
        end_fmt   = target_date.strftime('%m/%d/%Y')

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
                self.logger.info(f"{self.county}: loading advanced search...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                page.locator('text=Advanced Search').first.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # ── Fill date range ────────────────────────────────────────
                for s, e in [
                    ('input[id*="start" i]',           'input[id*="end" i]'),
                    ('input[placeholder*="Start" i]',   'input[placeholder*="End" i]'),
                    ('input[aria-label*="Start" i]',    'input[aria-label*="End" i]'),
                ]:
                    try:
                        if page.locator(s).count() > 0 and page.locator(e).count() > 0:
                            page.fill(s, start_fmt)
                            page.fill(e, end_fmt)
                            self.logger.info(f"{self.county}: filled date range {start_fmt}→{end_fmt}")
                            break
                    except Exception:
                        pass

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

                # ── Collect NTS rows (paginate up to 15 pages) ─────────────
                nts_rows = []
                for page_num in range(1, 16):
                    html = page.content()
                    rows = self._parse_nts_rows(html)
                    nts_rows.extend(rows)
                    self.logger.info(f"{self.county}: page {page_num} → {len(rows)} NTS rows")
                    if not self._next_page(page):
                        break

                self.logger.info(f"{self.county}: {len(nts_rows)} total NTS rows")

                # ── Re-load results to click into each NTS document ────────
                # Go back to results page
                page.go_back()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(2000)

                # ── Enrich each row with sale date ─────────────────────────
                records = []
                for row in nts_rows:
                    if not is_residential_lead(
                        row.get('first_name', '') + ' ' + row.get('last_name', '')
                    ):
                        self.logger.info(f"{self.county}: skipping entity: {row.get('last_name')}")
                        continue

                    rec = self._fetch_sale_date(page, row)
                    records.append(rec)

                browser.close()
                return records

        except Exception as exc:
            self.logger.error(f"{self.county}: error: {exc}", exc_info=True)
            return None

    # ── Row parsing ───────────────────────────────────────────────────────────

    def _parse_nts_rows(self, html: str) -> List[Dict]:
        soup    = BeautifulSoup(html, 'lxml')
        rows    = []
        all_dts = set()

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
                tds   = tr.find_all('td')
                cells = [td.get_text(' ', strip=True) for td in tds]
                if not cells or not any(c.strip() for c in cells):
                    continue

                def cell(i):
                    return cells[i].strip() if 0 <= i < len(cells) else ''

                doc_type = cell(di)
                all_dts.add(doc_type)

                if not is_nts(doc_type):
                    continue

                grantor  = cell(gi)
                rec_date = cell(ri)
                addr_raw = cell(ai)
                doc_num  = cell(ni)

                if not grantor:
                    continue

                first, last = self.parse_name(grantor)
                address, city, zip_c = self._split_address(addr_raw)

                self.logger.info(
                    f"{self.county}: NTS — '{grantor}' | "
                    f"doc='{doc_num}' | date='{rec_date}'"
                )
                rows.append({
                    'first_name': first, 'last_name': last,
                    'address': address, 'city': city, 'zip_code': zip_c,
                    'file_date': self._fmt(rec_date),
                    'doc_number': doc_num,
                    'grantor': grantor,
                })

        self.logger.info(f"{self.county}: doc types: {all_dts}")
        return rows

    # ── Sale date via Playwright click ────────────────────────────────────────

    def _fetch_sale_date(self, page, row: Dict) -> Dict:
        """
        Click the doc number cell on the results page to navigate to
        the document detail, then extract the sale date from the NTS text.
        Playwright handles React click handlers that aren't <a href> links.
        """
        sale_date = ''
        address   = row.get('address', '')
        city      = row.get('city', '')
        zip_c     = row.get('zip_code', '')
        doc_num   = row.get('doc_number', '')
        grantor   = row.get('grantor', '')

        if not doc_num:
            return self.build_record(**row, sale_date='')

        try:
            # Find the cell containing this doc number and click it
            cell_sel = f'td:text-is("{doc_num}")'
            el = page.locator(cell_sel).first

            if el.count() == 0:
                # Try partial match on doc number
                cell_sel = f'td:has-text("{doc_num}")'
                el = page.locator(cell_sel).first

            if el.count() > 0:
                el.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(2000)

                detail_url = page.url
                text = BeautifulSoup(page.content(), 'lxml').get_text(' ', strip=True)
                self.logger.info(
                    f"{self.county}: detail page for '{grantor}' "
                    f"({len(text)} chars): {text[:300]}"
                )

                # Extract sale date
                for pat in [
                    r'Sale\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'first\s+Tuesday[^,\n]*,?\s*(\w+\s+\d{1,2},?\s*\d{4})',
                    r'will\s+be\s+sold\s+on\s+(\w+\s+\d{1,2},?\s*\d{4})',
                    r'sale\s+will\s+be\s+held[^,\n]*,?\s*(\w+\s+\d{1,2},?\s*\d{4})',
                    r'(\d{1,2}/\d{1,2}/\d{4})(?=.*?(?:auction|sale))',
                ]:
                    m = re.search(pat, text, re.I | re.DOTALL)
                    if m:
                        sale_date = m.group(1).strip()
                        self.logger.info(f"{self.county}: sale_date = {sale_date}")
                        break

                # Better address from document text
                if not address:
                    m2 = re.search(
                        r'(\d+\s+[A-Z][A-Z0-9\s]+?'
                        r'(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY|LOOP|HWY)[A-Z\s\.]*?)'
                        r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
                        text, re.I
                    )
                    if m2:
                        address = m2.group(1).strip().title()
                        city    = m2.group(2).strip().title()
                        zip_c   = m2.group(3)

                # Go back to results
                page.go_back()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

            else:
                self.logger.warning(f"{self.county}: could not find cell for doc '{doc_num}'")

        except Exception as e:
            self.logger.warning(f"{self.county}: click detail error: {e}")

        return self.build_record(
            first_name=row.get('first_name', ''),
            last_name=row.get('last_name', ''),
            address=address, city=city, zip_code=zip_c,
            file_date=row.get('file_date', ''),
            sale_date=self._fmt(sale_date),
        )

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page(self, page) -> bool:
        for sel in [
            'button:has-text("Next")',
            'a:has-text("Next")',
            '[aria-label="Next"]',
            '[aria-label="Next page"]',
            'li:not(.disabled) a[rel="next"]',
            'li:not([class*="disabled"]) a:has-text("›")',
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

    # ── Utilities ─────────────────────────────────────────────────────────────

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
