"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant, Denton, Johnson

Strategy:
  Quick Search → Full Text (OCR) → "notice of trustee" → Last 1 Month
  Full text search finds actual NTS document text, not just index metadata.
  Falls back to Advanced Search if Quick Search doesn't produce results.

Sale dates are extracted from the NTS document detail page.
"""

import re
from datetime import date
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper


# NTS grantor exclusions — these are not residential pre-foreclosure leads
ENTITY_EXCLUSIONS = [
    'GROUNDWATER', 'CONSERVATION DISTRICT', 'WATER DISTRICT',
    'MUNICIPALITY', 'COUNTY', 'CITY OF', 'STATE OF',
    'INTERNAL REVENUE',
]

def looks_like_lead(grantor: str) -> bool:
    """Filter out government entities and water districts."""
    g = grantor.upper()
    for ex in ENTITY_EXCLUSIONS:
        if ex in g:
            return False
    return True


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str):
        super().__init__(county_name)
        self.slug     = county_slug
        self.base_url = f"https://{county_slug}.tx.publicsearch.us"

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        records = self._playwright_scrape()
        if not records:
            records = []
        self.logger.info(f"{self.county}: {len(records)} NTS records")
        return records

    def _playwright_scrape(self) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ))
                page = context.new_page()
                page.set_default_timeout(30_000)

                # ── Quick Search with Full Text OCR ───────────────────────
                self.logger.info(f"{self.county}: loading quick search...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # Enable Full Text OCR search
                for sel in [
                    'text=Search Index & Full Text',
                    'label:has-text("Full Text")',
                    'input[value*="fulltext" i]',
                    'input[id*="fulltext" i]',
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            el.click()
                            self.logger.info(f"{self.county}: enabled full text OCR")
                            break
                    except Exception:
                        pass

                # Type NTS search term
                for sel in ['input[placeholder*="grantor" i]', 'input[placeholder*="search" i]']:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0 and el.is_visible():
                            el.fill('notice of trustee')
                            self.logger.info(f"{self.county}: typed search term")
                            break
                    except Exception:
                        pass

                # Set date range — click dropdown then select Last 1 Month
                self._set_date_range(page)

                # Click Search
                for btn_sel in ['button:has-text("Search")', 'button[type="submit"]',
                                'img[alt*="Search" i]']:
                    try:
                        btn = page.locator(btn_sel).first
                        if btn.count() > 0:
                            btn.click()
                            self.logger.info(f"{self.county}: clicked Search")
                            break
                    except Exception:
                        pass

                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(4000)

                body = page.inner_text('body')
                self.logger.info(f"{self.county} results: {body[:400]}")

                # ── Collect rows (paginate up to 10 pages) ─────────────────
                all_rows = []
                for page_num in range(1, 11):
                    html  = page.content()
                    rows  = self._extract_rows(html)
                    all_rows.extend(rows)
                    self.logger.info(f"{self.county}: page {page_num} → {len(rows)} rows")
                    if not self._next_page(page):
                        break

                # ── If Quick Search gave nothing, fall back to Advanced Search
                if not all_rows:
                    self.logger.info(f"{self.county}: Quick Search empty, trying Advanced Search")
                    all_rows = self._advanced_search(page)

                self.logger.info(f"{self.county}: {len(all_rows)} total NTS rows")

                # ── Fetch each document for sale date ─────────────────────
                records = []
                for row in all_rows:
                    if not looks_like_lead(row.get('last_name', '') + ' ' + row.get('first_name', '')):
                        continue
                    rec = self._get_sale_date(page, row)
                    records.append(rec)

                browser.close()
                return records

        except Exception as exc:
            self.logger.error(f"{self.county}: error: {exc}", exc_info=True)
            return None

    # ── Date range setter ─────────────────────────────────────────────────────

    def _set_date_range(self, page):
        """Try to click the date range dropdown and select Last 1 Month."""
        try:
            # Click the date range trigger first
            for trigger_sel in [
                '[class*="dateRange" i]',
                'text=Recorded Date',
                '[class*="date" i][class*="filter" i]',
            ]:
                try:
                    el = page.locator(trigger_sel).first
                    if el.count() > 0 and el.is_visible():
                        el.click()
                        page.wait_for_timeout(500)
                        break
                except Exception:
                    pass

            # Click Last 1 Month
            for opt_sel in [
                'text=Last 1 Month',
                'li:has-text("Last 1 Month")',
                'button:has-text("Last 1 Month")',
                '[class*="option" i]:has-text("Last 1 Month")',
                'text=1 Month',
            ]:
                try:
                    el = page.locator(opt_sel).first
                    if el.count() > 0:
                        el.click()
                        page.wait_for_timeout(500)
                        self.logger.info(f"{self.county}: set date range to Last 1 Month")
                        return
                except Exception:
                    pass
        except Exception as e:
            self.logger.debug(f"{self.county}: date range error: {e}")

    # ── Advanced Search fallback ──────────────────────────────────────────────

    def _advanced_search(self, page) -> List[Dict]:
        from datetime import timedelta
        start_fmt = (date.today() - timedelta(days=45)).strftime('%m/%d/%Y')
        end_fmt   = date.today().strftime('%m/%d/%Y')

        page.goto(self.base_url)
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(1000)
        page.locator('text=Advanced Search').first.click()
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(1000)

        for s, e in [
            ('input[id*="start" i]', 'input[id*="end" i]'),
            ('input[placeholder*="Start" i]', 'input[placeholder*="End" i]'),
        ]:
            try:
                if page.locator(s).count() > 0:
                    page.fill(s, start_fmt)
                    page.fill(e, end_fmt)
                    break
            except Exception:
                pass

        for btn_sel in ['button[type="submit"]', 'button:has-text("Search")']:
            try:
                btn = page.locator(btn_sel).first
                if btn.count() > 0:
                    btn.click()
                    break
            except Exception:
                pass

        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(4000)

        all_rows = []
        for page_num in range(1, 11):
            html = page.content()
            rows = self._extract_rows(html)
            all_rows.extend(rows)
            self.logger.info(f"{self.county}: adv page {page_num} → {len(rows)} rows")
            if not self._next_page(page):
                break
        return all_rows

    # ── Row extraction ────────────────────────────────────────────────────────

    def _extract_rows(self, html: str) -> List[Dict]:
        """Extract NTS rows. Scans ALL cells for links (not just named column)."""
        soup     = BeautifulSoup(html, 'lxml')
        nts_rows = []
        all_doc_types = set()

        NTS_DOC_TYPES = {
            'NOTICE OF TRUSTEE', 'NOTICE OF SUBSTITUTE', 'NTS',
            'SUBSTITUTE TRUSTEE', 'TRUSTEE SALE',
        }
        # "NOTICE" alone only if NOT a lien/tax/lis pendens notice
        NOTICE_EXCLUSIONS = {'LIS PENDENS', 'LIEN', 'HOSPITAL', 'FILING', 'COMPLETION'}

        def is_nts(dt: str) -> bool:
            dt = dt.upper().strip()
            for kw in NTS_DOC_TYPES:
                if kw in dt:
                    return True
            if dt == 'NOTICE':
                return True
            if 'NOTICE' in dt:
                for ex in NOTICE_EXCLUSIONS:
                    if ex in dt:
                        return False
                return True
            return False

        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers:
                continue

            self.logger.info(f"{self.county}: columns: {headers}")
            h  = {v: i for i, v in enumerate(headers)}
            gi = h.get('grantor', -1)
            di = h.get('doc type', -1)
            ri = h.get('recorded date', -1)
            ai = h.get('property address', h.get('legal description', h.get('town', -1)))

            for tr in table.find_all('tr')[1:]:
                tds = tr.find_all('td')
                cells = [td.get_text(' ', strip=True) for td in tds]
                if not cells or not any(c.strip() for c in cells):
                    continue

                def cell(i):
                    return cells[i].strip() if 0 <= i < len(cells) else ''

                doc_type = cell(di)
                all_doc_types.add(doc_type)

                if not is_nts(doc_type):
                    continue

                grantor  = cell(gi)
                rec_date = cell(ri)
                addr_raw = cell(ai)

                # Find ANY link in ANY cell of this row
                doc_link = ''
                for td in tds:
                    a = td.find('a', href=True)
                    if a:
                        href = a['href']
                        if href and href not in ('/', '#', ''):
                            doc_link = href if href.startswith('http') else self.base_url + href
                            break

                first, last = self.parse_name(grantor) if grantor else ('', '')
                address, city, zip_c = self._split_address(addr_raw)

                self.logger.info(
                    f"{self.county}: NTS row — grantor='{grantor}' "
                    f"doc_type='{doc_type}' file_date='{rec_date}' "
                    f"doc_link='{doc_link[:60]}'"
                )

                nts_rows.append({
                    'first_name': first, 'last_name': last,
                    'address': address, 'city': city, 'zip_code': zip_c,
                    'file_date': self._fmt(rec_date),
                    'doc_link': doc_link,
                })

        self.logger.info(f"{self.county}: doc types seen: {all_doc_types}")
        return nts_rows

    # ── Pagination ────────────────────────────────────────────────────────────

    def _next_page(self, page) -> bool:
        """Try to click the Next page button or next page number."""
        selectors = [
            'button:has-text("Next")',
            'a:has-text("Next")',
            'li:not(.disabled) a:has-text("Next")',
            'li:not(.disabled) a:has-text("›")',
            'li:not(.disabled) a:has-text(">")',
            '[aria-label="Next"]',
            '[aria-label="Next page"]',
            '[class*="next" i]:not([disabled]):not([class*="disabled" i])',
        ]
        for sel in selectors:
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

    # ── Sale date from document ───────────────────────────────────────────────

    def _get_sale_date(self, page, row: Dict) -> Dict:
        sale_date = ''
        address   = row.get('address', '')
        city      = row.get('city', '')
        zip_c     = row.get('zip_code', '')

        doc_link = row.get('doc_link', '')
        self.logger.info(f"{self.county}: fetching detail: {doc_link[:80]}")

        if doc_link:
            try:
                page.goto(doc_link)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(2000)

                text = BeautifulSoup(page.content(), 'lxml').get_text(' ', strip=True)
                self.logger.info(f"{self.county}: detail ({len(text)} chars): {text[:500]}")

                # Sale date patterns common in TX NTS documents
                for pat in [
                    r'Sale\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Auction\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'first\s+Tuesday[^,\n]*,?\s*(\w+\s+\d{1,2},?\s+\d{4})',
                    r'will\s+be\s+sold\s+on\s+(\w+\s+\d{1,2},?\s+\d{4})',
                    r'sale\s+will\s+be\s+held\s+on\s+(\w+\s+\d{1,2},?\s+\d{4})',
                    r'(\d{1,2}/\d{1,2}/\d{4}).*?(?:sale|auction|bid)',
                ]:
                    m = re.search(pat, text, re.I)
                    if m:
                        sale_date = m.group(1).strip()
                        self.logger.info(f"{self.county}: sale date = {sale_date}")
                        break

                # Try to get address from document if we don't have one
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

            except Exception as e:
                self.logger.warning(f"{self.county}: detail error: {e}")

        return self.build_record(
            first_name=row.get('first_name', ''), last_name=row.get('last_name', ''),
            address=address, city=city, zip_code=zip_c,
            file_date=row.get('file_date', ''), sale_date=self._fmt(sale_date),
        )

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
