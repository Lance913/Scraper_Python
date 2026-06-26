"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant, Denton, Johnson

Key design decisions:
  - Advanced Search, 45-day window
  - Enrich (click doc → get sale date) per page before paginating
    so we're always on the right results page when clicking cells
  - URL check after click: if modal opened (URL unchanged), press Escape;
    if navigated (URL changed), use go_back()
  - APPOINTMENT OF TRUSTEE excluded from NTS matches
"""

import re
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper

EXCLUDE_KEYWORDS = [
    'GROUNDWATER', 'CONSERVATION DISTRICT', 'WATER DISTRICT',
    'INTERNAL REVENUE', 'D R HORTON', 'DR HORTON', 'HORTON D R',
    'LENNAR HOMES', 'KB HOME', 'MERITAGE', 'PULTE',
    'CENTEX', 'TAYLOR MORRISON', 'STARLIGHT HOMES',
    'CONTINENTAL HOMES', 'BEAZER HOMES', 'CHESMAR HOMES',
    'M/I HOMES', 'COVENTRY', 'COUTO HOMES',
]

def is_residential_lead(grantor: str) -> bool:
    g = grantor.upper()
    return not any(ex in g for ex in EXCLUDE_KEYWORDS)

# Only "NOTICE OF ..." docs — not plain APPOINTMENT/SUBSTITUTE docs
NTS_DOC_KEYS = {
    'NOTICE OF TRUSTEE',
    'NOTICE OF SUBSTITUTE',
    'TRUSTEE SALE',
    'NTS',
}
NOTICE_BAD = {
    'LIS PENDENS', 'HOSPITAL', 'LIEN', 'CLAIM',
    'COMPLETION', 'COMMENCEMENT', 'APPOINTMENT',
}

def is_nts(doc_type: str) -> bool:
    dt = doc_type.upper().strip()

    # Explicit exclusion first
    if 'APPOINTMENT' in dt:
        return False

    for kw in NTS_DOC_KEYS:
        if kw in dt:
            return True

    if 'NOTICE' in dt:
        for bad in NOTICE_BAD:
            if bad in dt:
                return False
        # Plain 'NOTICE' or any notice with TRUSTEE/SUBSTITUTE
        if dt == 'NOTICE' or 'TRUSTEE' in dt or 'SUBSTITUTE' in dt:
            return True

    return False


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
                browser = pw.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                context = browser.new_context(user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
                ))
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page.set_default_timeout(30_000)

                self.logger.info(f"{self.county}: loading advanced search...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # Navigate directly to avoid tooltip-overlay click intercept
                page.goto(self.base_url + '/search/advanced')
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # Fill date range
                for s, e in [
                    ('input[id*="start" i]',          'input[id*="end" i]'),
                    ('input[placeholder*="Start" i]',  'input[placeholder*="End" i]'),
                    ('input[aria-label*="Start" i]',   'input[aria-label*="End" i]'),
                ]:
                    try:
                        if page.locator(s).count() > 0 and page.locator(e).count() > 0:
                            page.fill(s, start_fmt)
                            page.fill(e, end_fmt)
                            self.logger.info(f"{self.county}: date range {start_fmt}→{end_fmt}")
                            break
                    except Exception:
                        pass

                # Search
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

                # ── Scrape and enrich per page ─────────────────────────────
                # Enrich BEFORE paginating so we're always on the right page
                # when clicking doc number cells.
                records = []
                for page_num in range(1, 16):
                    html     = page.content()
                    nts_rows = self._parse_nts_rows(html)
                    self.logger.info(f"{self.county}: page {page_num} → {len(nts_rows)} NTS rows")

                    for row in nts_rows:
                        # Use original grantor string, not parsed name
                        # (parse_name reverses "HORTON D R TEXAS LTD" → "Horton D R Texas Ltd"
                        #  which breaks substring matches like 'D R HORTON')
                        if not is_residential_lead(row.get('grantor', '')):
                            self.logger.info(f"{self.county}: skip entity: {row.get('grantor')}")
                            continue
                        rec = self._fetch_sale_date(page, row)
                        records.append(rec)

                    if not self._next_page(page):
                        break

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

                def cell(i, _cells=cells):
                    return _cells[i].strip() if 0 <= i < len(_cells) else ''

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

        self.logger.info(f"{self.county}: doc types seen: {all_dts}")
        return rows

    # ── Sale date via Playwright click ────────────────────────────────────────

    def _fetch_sale_date(self, page, row: Dict) -> Dict:
        """
        Click the doc number cell on the CURRENT results page.
        Playwright handles React onClick without needing <a href>.
        
        After extracting sale date:
          - If URL changed (navigated): go_back() to results
          - If URL same (modal opened): press Escape to close modal
        """
        sale_date = ''
        address   = row.get('address', '')
        city      = row.get('city', '')
        zip_c     = row.get('zip_code', '')
        doc_num   = row.get('doc_number', '')
        grantor   = row.get('grantor', '')

        if not doc_num:
            return self.build_record(
                first_name=row.get('first_name', ''), last_name=row.get('last_name', ''),
                address=address, city=city, zip_code=zip_c,
                file_date=row.get('file_date', ''), sale_date='',
            )

        try:
            for sel in [
                f'td:text-is("{doc_num}")',
                f'td:has-text("{doc_num}")',
                f'[class*="docNumber" i]:has-text("{doc_num}")',
            ]:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    results_url = page.url

                    el.scroll_into_view_if_needed()
                    el.click()
                    page.wait_for_load_state('networkidle')
                    page.wait_for_timeout(2000)

                    new_url = page.url
                    text    = BeautifulSoup(page.content(), 'lxml').get_text(' ', strip=True)
                    self.logger.info(
                        f"{self.county}: detail '{grantor}' → {new_url} "
                        f"({len(text)} chars): {text[:300]}"
                    )

                    # Extract sale date from NTS document text
                    for pat in [
                        r'Sale\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                        r'Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                        r'first\s+Tuesday[^,\n]*,?\s*(\w+\s+\d{1,2},?\s*\d{4})',
                        r'will\s+be\s+sold\s+on\s+(\w+\s+\d{1,2},?\s*\d{4})',
                        r'sale\s+will\s+be\s+held[^,\n]*,?\s*(\w+\s+\d{1,2},?\s*\d{4})',
                    ]:
                        m = re.search(pat, text, re.I | re.DOTALL)
                        if m:
                            sale_date = m.group(1).strip()
                            self.logger.info(f"{self.county}: sale_date = {sale_date}")
                            break

                    # Try to get address from doc text if missing
                    if not address:
                        m2 = re.search(
                            r'(\d+\s+[A-Z][A-Z0-9\s]+?'
                            r'(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY|LOOP|HWY)'
                            r'[A-Z\s\.]*?),?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
                            text, re.I
                        )
                        if m2:
                            address = m2.group(1).strip().title()
                            city    = m2.group(2).strip().title()
                            zip_c   = m2.group(3)

                    # Return to results
                    if new_url != results_url:
                        # Navigated to a new page — go_back() works
                        page.go_back()
                        page.wait_for_load_state('networkidle')
                        page.wait_for_timeout(1500)
                    else:
                        # Modal/drawer opened (URL unchanged) — close with Escape
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(500)

                    break  # done with this row

            else:
                self.logger.warning(f"{self.county}: cell not found for doc '{doc_num}'")

        except Exception as e:
            self.logger.warning(f"{self.county}: detail error '{grantor}': {e}")

        return self.build_record(
            first_name=row.get('first_name', ''), last_name=row.get('last_name', ''),
            address=address, city=city, zip_code=zip_c,
            file_date=row.get('file_date', ''), sale_date=self._fmt(sale_date),
        )

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page(self, page) -> bool:
        for sel in [
            'button:has-text("Next")',
            'a:has-text("Next")',
            '[aria-label="Next"]',
            '[aria-label="Next page"]',
            'li:not(.disabled) a:has-text("›")',
            '[class*="next" i]:not([disabled]):not([class*="disabled" i])',
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
