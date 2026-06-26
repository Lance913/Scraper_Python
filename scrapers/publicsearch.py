"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

Key fixes:
1. Search last 7 days only (not 30)
2. Filter results to NTS/foreclosure document types only
3. For each NTS record, click into the document detail to extract sale date
"""

import re
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper


def to_mddyyyy(iso: str) -> str:
    parts = iso.split('-')
    return f"{parts[1]}/{parts[2]}/{parts[0]}"


# Document type keywords that indicate a foreclosure notice
NTS_KEYWORDS = [
    'NOTICE OF TRUSTEE', 'NOTICE OF SUBSTITUTE', 'NTS', 'NOTSALE',
    'SUBSTITUTE TRUSTEE', 'TRUSTEE SALE', 'FORECLOSURE', 'NOTICE'
]

def is_nts(doc_type: str) -> bool:
    dt = doc_type.upper().strip()
    return any(kw in dt for kw in NTS_KEYWORDS)


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str, doc_types=None):
        super().__init__(county_name)
        self.slug     = county_slug
        self.base_url = f"https://{county_slug}.tx.publicsearch.us"

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        # 7-day window — portal certified 2-4 days behind, 7 days is plenty
        start_iso = (target_date - timedelta(days=7)).strftime('%Y-%m-%d')
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

                # Load and go to Advanced Search
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)
                page.locator('text=Advanced Search').first.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # Fill date range
                filled = self._fill_date_range(page, start_fmt, end_fmt)
                self.logger.info(f"{self.county}: date fill → {filled}")

                # Click Search
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

                html_content = page.content()

                # Parse table, find NTS rows and their document links
                nts_rows = self._extract_nts_rows(html_content)
                self.logger.info(f"{self.county}: found {len(nts_rows)} NTS rows")

                # For each NTS row, fetch document detail to get sale date
                records = []
                for row in nts_rows:
                    rec = self._enrich_with_sale_date(page, row)
                    records.append(rec)

                browser.close()
                return records

        except Exception as exc:
            self.logger.error(f"{self.county}: error: {exc}", exc_info=True)
            return None

    def _extract_nts_rows(self, html: str) -> List[Dict]:
        """Parse results table, return only NTS rows with their document links."""
        soup = BeautifulSoup(html, 'lxml')
        rows = []

        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers:
                continue

            h  = {v: i for i, v in enumerate(headers)}
            gi = h.get('grantor', -1)
            di = h.get('doc type', -1)
            ri = h.get('recorded date', -1)
            ai = h.get('property address', h.get('legal description', h.get('town', -1)))
            # Doc number column (has the clickable link)
            ni = h.get('doc number', h.get('inst number', -1))

            for tr in table.find_all('tr')[1:]:
                cells   = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                tds_raw = tr.find_all('td')
                if not cells or not any(c.strip() for c in cells):
                    continue

                def cell(i):
                    return cells[i].strip() if 0 <= i < len(cells) else ''

                doc_type = cell(di)
                if not is_nts(doc_type):
                    continue  # Skip non-NTS records

                grantor  = cell(gi)
                rec_date = cell(ri) or ''
                addr_raw = cell(ai)

                # Get doc detail link
                doc_link = ''
                if 0 <= ni < len(tds_raw):
                    a = tds_raw[ni].find('a')
                    if a and a.get('href'):
                        doc_link = a['href']
                        if not doc_link.startswith('http'):
                            doc_link = self.base_url + doc_link

                first, last = self.parse_name(grantor) if grantor else ('', '')
                address, city, zip_c = self._split_address(addr_raw)

                rows.append({
                    'first_name': first,
                    'last_name':  last,
                    'address':    address,
                    'city':       city,
                    'zip_code':   zip_c,
                    'file_date':  self._fmt(rec_date),
                    'doc_type':   doc_type,
                    'doc_link':   doc_link,
                })

        return rows

    def _enrich_with_sale_date(self, page, row: Dict) -> Dict:
        """Navigate to the document detail page and extract sale date."""
        sale_date = ''
        address   = row.get('address', '')
        city      = row.get('city', '')
        zip_c     = row.get('zip_code', '')

        if row.get('doc_link'):
            try:
                page.goto(row['doc_link'])
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                detail_html = page.content()
                detail_text = BeautifulSoup(detail_html, 'lxml').get_text(' ', strip=True)

                self.logger.info(f"{self.county}: detail text sample: {detail_text[:400]}")

                # Extract sale date
                for pattern in [
                    r'Sale\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Auction\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'first\s+Tuesday[^,]*,?\s+(\w+\s+\d{1,2},?\s+\d{4})',
                    r'(\w+\s+\d{1,2},\s+\d{4})',  # "July 7, 2026" format
                ]:
                    m = re.search(pattern, detail_text, re.I)
                    if m:
                        sale_date = m.group(1).strip()
                        self.logger.info(f"{self.county}: sale date found: {sale_date}")
                        break

                # Try to get better address from detail if we don't have one
                if not address:
                    m2 = re.search(
                        r'(\d+\s+[A-Z0-9][A-Z0-9\s]+?'
                        r'(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY|LOOP|HWY)[A-Z\s\.]*?)'
                        r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
                        detail_text, re.I
                    )
                    if m2:
                        address = m2.group(1).strip().title()
                        city    = m2.group(2).strip().title()
                        zip_c   = m2.group(3)

            except Exception as e:
                self.logger.warning(f"{self.county}: detail fetch error: {e}")

        return self.build_record(
            first_name=row.get('first_name', ''),
            last_name=row.get('last_name', ''),
            address=address,
            city=city,
            zip_code=zip_c,
            file_date=row.get('file_date', ''),
            sale_date=self._fmt(sale_date),
        )

    def _fill_date_range(self, page, start_fmt: str, end_fmt: str) -> str:
        pairs = [
            ('input[id*="start" i]',           'input[id*="end" i]'),
            ('input[placeholder="Start date"]', 'input[placeholder="End date"]'),
            ('input[placeholder*="Start" i]',   'input[placeholder*="End" i]'),
        ]
        for start_sel, end_sel in pairs:
            try:
                if page.locator(start_sel).count() > 0 and page.locator(end_sel).count() > 0:
                    page.fill(start_sel, start_fmt)
                    page.fill(end_sel, end_fmt)
                    page.wait_for_timeout(300)
                    return f"filled '{start_sel}'"
            except Exception:
                pass
        return "no fields filled"

    @staticmethod
    def _split_address(raw: str) -> Tuple[str, str, str]:
        if not raw or raw == 'N/A':
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

    # Needed for Tuple type hint
    from typing import Tuple
