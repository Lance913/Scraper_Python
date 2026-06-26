"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

Key findings from debug run:
- Page loads Quick Search form but makes NO API calls until user searches
- Need to: click Advanced Search → fill date range → click Search → extract results
- Portal data is certified 2-3 days behind, so we search last 7 days
- Capture API responses AND extract from rendered DOM table
"""

import json
import re
from datetime import date, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from .base import BaseScraper


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str, doc_types=None):
        super().__init__(county_name)
        self.slug      = county_slug
        self.base_url  = f"https://{county_slug}.tx.publicsearch.us"
        self.doc_types = doc_types or ['NTS']

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        # Search last 7 days — portal certifies data 2-3 days behind
        start_date = (target_date - timedelta(days=7)).strftime('%Y-%m-%d')
        end_date   = target_date.strftime('%Y-%m-%d')

        records = self._playwright_scrape(start_date, end_date)
        if records is None:
            records = []
        self.logger.info(f"{self.county}: {len(records)} records")
        return records

    # ── Playwright ────────────────────────────────────────────────────────────

    def _playwright_scrape(self, start_date: str, end_date: str) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

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
                self.logger.info(f"{self.county}: JSON from {response.url} — keys:{keys}")
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

                # ── Step 1: Load portal ────────────────────────────────────
                self.logger.info(f"{self.county}: loading portal...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(2000)

                # ── Step 2: Click Advanced Search ──────────────────────────
                adv_clicked = False
                for sel in ['text=Advanced Search', 'a:has-text("Advanced")', '[href*="advanced" i]']:
                    el = page.query_selector(sel)
                    if el:
                        el.click()
                        page.wait_for_load_state('networkidle')
                        page.wait_for_timeout(2000)
                        self.logger.info(f"{self.county}: clicked Advanced Search via '{sel}'")
                        adv_clicked = True
                        break

                body_adv = page.inner_text('body')
                self.logger.info(f"{self.county} advanced search form: {body_adv[:800]}")

                # ── Step 3: Fill date range ────────────────────────────────
                filled = self._fill_dates(page, start_date, end_date)
                self.logger.info(f"{self.county}: date fill result: {filled}")

                # ── Step 4: Click Search ───────────────────────────────────
                for btn_sel in [
                    'button[type="submit"]',
                    'button:has-text("Search")',
                    'input[type="submit"]',
                    '[class*="search" i][class*="btn" i]',
                ]:
                    btn = page.query_selector(btn_sel)
                    if btn:
                        btn.click()
                        self.logger.info(f"{self.county}: clicked Search via '{btn_sel}'")
                        break

                # ── Step 5: Wait for results ───────────────────────────────
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(4000)

                body_results = page.inner_text('body')
                self.logger.info(f"{self.county} after search: {body_results[:800]}")

                html_content = page.content()
                browser.close()

            # ── Parse captured API responses ───────────────────────────────
            records = []
            for entry in captured_json:
                records.extend(self._parse_json(entry['data'], end_date))

            # ── DOM fallback ───────────────────────────────────────────────
            if not records:
                self.logger.info(f"{self.county}: no API JSON captured, trying DOM")
                records = self._parse_dom(html_content, end_date)

            return records

        except Exception as exc:
            self.logger.error(f"{self.county}: Playwright error: {exc}", exc_info=True)
            return None

    def _fill_dates(self, page, start_date: str, end_date: str) -> bool:
        """Try every known date-input pattern on the advanced search form."""
        patterns = [
            # Placeholder-based
            ('input[placeholder*="Start" i]',         'input[placeholder*="End" i]'),
            ('input[placeholder*="From" i]',           'input[placeholder*="To" i]'),
            ('input[placeholder*="Begin" i]',          'input[placeholder*="End" i]'),
            # aria-label
            ('input[aria-label*="Start" i]',           'input[aria-label*="End" i]'),
            ('input[aria-label*="From" i]',            'input[aria-label*="To" i]'),
            ('input[aria-label*="Filing Date From" i]','input[aria-label*="Filing Date To" i]'),
            # id/name
            ('input[id*="startDate" i]',               'input[id*="endDate" i]'),
            ('input[name*="start" i]',                 'input[name*="end" i]'),
            # Generic — first two date inputs on page
        ]

        for start_sel, end_sel in patterns:
            start_el = page.query_selector(start_sel)
            end_el   = page.query_selector(end_sel)
            if start_el and end_el:
                start_el.triple_click()
                start_el.fill(start_date)
                end_el.triple_click()
                end_el.fill(end_date)
                page.wait_for_timeout(500)
                self.logger.info(f"{self.county}: filled '{start_sel}' / '{end_sel}'")
                return True

        # Last resort: fill first two <input type="text"> on page
        inputs = page.query_selector_all('input[type="text"], input:not([type])')
        date_inputs = [i for i in inputs if i.is_visible()]
        if len(date_inputs) >= 2:
            date_inputs[0].triple_click()
            date_inputs[0].fill(start_date)
            date_inputs[1].triple_click()
            date_inputs[1].fill(end_date)
            self.logger.info(f"{self.county}: filled first 2 visible text inputs")
            return True

        return False

    # ── Parsers ───────────────────────────────────────────────────────────────

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
