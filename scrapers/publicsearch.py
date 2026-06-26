"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

Advanced Search form fields confirmed:
  - Recorded Date Range → Start date / End date (MM/DD/YYYY)
  - Instrument Date Range → Start date / End date
  - Document Types filter
  - Search button

Fix: use page.fill(selector, value) instead of ElementHandle.triple_click()
Fix: convert dates from YYYY-MM-DD to MM/DD/YYYY
"""

import re
from datetime import date, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from .base import BaseScraper


def to_mddyyyy(iso: str) -> str:
    """Convert '2026-06-19' → '06/19/2026'"""
    parts = iso.split('-')
    return f"{parts[1]}/{parts[2]}/{parts[0]}"


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str, doc_types=None):
        super().__init__(county_name)
        self.slug      = county_slug
        self.base_url  = f"https://{county_slug}.tx.publicsearch.us"
        self.doc_types = doc_types or ['NTS']

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        # Search last 7 days — portal is certified 2-4 days behind
        start_iso = (target_date - timedelta(days=7)).strftime('%Y-%m-%d')
        end_iso   = target_date.strftime('%Y-%m-%d')
        records   = self._playwright_scrape(start_iso, end_iso)
        if records is None:
            records = []
        self.logger.info(f"{self.county}: {len(records)} records")
        return records

    def _playwright_scrape(self, start_iso: str, end_iso: str) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        start_fmt = to_mddyyyy(start_iso)   # MM/DD/YYYY
        end_fmt   = to_mddyyyy(end_iso)

        captured_json = []

        def on_response(response):
            try:
                if response.status != 200:
                    return
                ct = response.headers.get('content-type', '')
                if 'json' not in ct:
                    return
                data = response.json()
                keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                self.logger.info(f"{self.county}: JSON from {response.url} keys:{keys}")
                captured_json.append({'url': response.url, 'data': data})
            except Exception:
                pass

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ))
                page = context.new_page()
                page.on('response', on_response)
                page.set_default_timeout(30_000)

                # ── Load portal and go to Advanced Search ──────────────────
                self.logger.info(f"{self.county}: loading portal...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                page.locator('text=Advanced Search').first.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # ── Fill Recorded Date Range ───────────────────────────────
                # Form labels: "Recorded Date Range" → Start date / End date
                # Date format: MM/DD/YYYY (confirmed from form)
                filled = self._fill_date_range(page, start_fmt, end_fmt)
                self.logger.info(f"{self.county}: date fill → {filled}")

                # ── Click Search ───────────────────────────────────────────
                for btn_sel in [
                    'button[type="submit"]',
                    'button:has-text("Search")',
                    'input[type="submit"]',
                ]:
                    try:
                        btn = page.locator(btn_sel).first
                        if btn.count() > 0:
                            btn.click()
                            self.logger.info(f"{self.county}: clicked Search via '{btn_sel}'")
                            break
                    except Exception:
                        pass

                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(4000)

                body = page.inner_text('body')
                self.logger.info(f"{self.county} results body: {body[:800]}")

                html_content = page.content()
                browser.close()

            # Parse API responses
            records = []
            for entry in captured_json:
                records.extend(self._parse_json(entry['data'], end_iso))

            # DOM fallback
            if not records:
                self.logger.info(f"{self.county}: trying DOM extraction")
                records = self._parse_dom(html_content, end_iso)

            return records

        except Exception as exc:
            self.logger.error(f"{self.county}: Playwright error: {exc}", exc_info=True)
            return None

    def _fill_date_range(self, page, start_fmt: str, end_fmt: str) -> str:
        """
        Fill the Recorded Date Range fields.
        Confirmed field labels: 'Start date' and 'End date' inside 'Recorded Date Range'.
        Uses page.fill() with selector (no ElementHandle needed).
        """
        # Strategy 1: placeholder-based (most reliable)
        pairs = [
            ('input[placeholder="Start date"]',  'input[placeholder="End date"]'),
            ('input[placeholder*="Start" i]',    'input[placeholder*="End" i]'),
            ('input[aria-label*="Start" i]',     'input[aria-label*="End" i]'),
            ('input[id*="start" i]',             'input[id*="end" i]'),
        ]

        for start_sel, end_sel in pairs:
            try:
                start_els = page.locator(start_sel)
                end_els   = page.locator(end_sel)
                if start_els.count() > 0 and end_els.count() > 0:
                    page.fill(start_sel, start_fmt)
                    page.fill(end_sel, end_fmt)
                    page.wait_for_timeout(300)
                    return f"filled '{start_sel}' with {start_fmt} / {end_fmt}"
            except Exception:
                pass

        # Strategy 2: all visible text inputs in order
        try:
            inputs = page.locator('input[type="text"], input:not([type])').all()
            visible = [i for i in inputs if i.is_visible()]
            self.logger.info(f"{self.county}: visible text inputs: {len(visible)}")
            if len(visible) >= 2:
                visible[0].fill(start_fmt)
                visible[1].fill(end_fmt)
                page.wait_for_timeout(300)
                return f"filled first 2 visible inputs with {start_fmt} / {end_fmt}"
        except Exception as e:
            self.logger.warning(f"{self.county}: input fill fallback failed: {e}")

        return "no fields filled"

    def _parse_json(self, data, date_str: str) -> List[Dict]:
        items = (
            data.get('results') or data.get('instruments') or
            data.get('data') or data.get('items') or
            (data if isinstance(data, list) else [])
        )
        if not isinstance(items, list) or not items:
            return []
        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            r = self._item_to_record(item, date_str)
            if r:
                records.append(r)
        return records

    def _item_to_record(self, item: Dict, date_str: str) -> Optional[Dict]:
        raw_name = ''
        grantors = item.get('grantors') or item.get('grantor') or []
        if isinstance(grantors, list) and grantors:
            g = grantors[0]
            raw_name = g.get('name', '') if isinstance(g, dict) else str(g)
        elif isinstance(grantors, str):
            raw_name = grantors
        raw_name = raw_name or item.get('grantorName', '') or ''
        first, last = self.parse_name(raw_name) if raw_name else ('', '')

        raw_addr = (
            item.get('siteAddress') or item.get('propertyAddress') or
            item.get('address') or item.get('legalDescription', '')
        )
        address, city, zip_code = self.parse_address(raw_addr) if raw_addr else ('', '', '')

        file_date = self._fmt(
            item.get('fileDate') or item.get('instrumentDate') or
            item.get('recordingDate') or date_str
        )
        sale_date = self._fmt(item.get('saleDate') or item.get('sale_date') or '')

        return self.build_record(
            first_name=first, last_name=last,
            address=address, city=city, zip_code=zip_code,
            file_date=file_date, sale_date=sale_date,
        )

    def _parse_dom(self, html: str, date_str: str) -> List[Dict]:
        soup    = BeautifulSoup(html, 'lxml')
        records = []
        for table in soup.find_all('table'):
            hdrs = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            self.logger.info(f"{self.county}: table headers: {hdrs}")
            for tr in table.find_all('tr')[1:]:
                cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                row   = ' '.join(cells)
                if not row.strip():
                    continue
                m = re.search(
                    r'(\d+\s+[A-Z][A-Z0-9\s]+?(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY)'
                    r'[A-Z\s\.]*?),?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
                    row, re.I
                )
                if m:
                    records.append(self.build_record(
                        address=m.group(1).strip().title(),
                        city=m.group(2).strip().title(),
                        zip_code=m.group(3),
                        file_date=self._fmt(date_str),
                    ))
        return records

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
