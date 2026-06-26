"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

Strategy:
  1. Navigate to the portal with date params in the URL hash
  2. Intercept ALL JSON API responses (log them all for debugging)
  3. Also interact with the search form directly
  4. Extract data from rendered DOM table as a fallback
"""

import json
import re
import time
from datetime import date
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
        date_str = target_date.strftime('%Y-%m-%d')
        records  = self._playwright_scrape(date_str)
        if records is None:
            records = self._rest_api(date_str)
        self.logger.info(f"{self.county}: {len(records)} records")
        return records

    # ── Playwright ────────────────────────────────────────────────────────────

    def _playwright_scrape(self, date_str: str) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        captured_json = []   # every JSON response, for debug

        def on_response(response):
            try:
                if response.status != 200:
                    return
                ct = response.headers.get('content-type', '')
                if 'json' not in ct:
                    return
                data = response.json()
                self.logger.info(f"{self.county}: captured JSON from {response.url} — keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
                captured_json.append({'url': response.url, 'data': data})
            except Exception:
                pass

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36'
                    )
                )
                page = context.new_page()
                page.on('response', on_response)
                page.set_default_timeout(30_000)

                # ── Approach 1: direct URL with search params ──────────────
                search_url = (
                    f"{self.base_url}/#/"
                    f"?searchType=Quick"
                    f"&dateRangeType=Filing"
                    f"&startDate={date_str}"
                    f"&endDate={date_str}"
                )
                self.logger.info(f"{self.county}: loading {search_url}")
                page.goto(search_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(3000)

                # ── Approach 2: interact with search form ──────────────────
                self._fill_and_submit(page, date_str)

                # ── Approach 3: inject fetch calls ─────────────────────────
                self._inject_api_calls(page, date_str)
                page.wait_for_timeout(3000)

                # Grab rendered HTML for DOM-based fallback
                html_content = page.content()
                body_text    = page.inner_text('body')
                self.logger.info(f"{self.county} body sample: {body_text[:600]}")

                browser.close()

            # ── Parse captured API responses ───────────────────────────────
            records = []
            for entry in captured_json:
                records.extend(self._parse_json(entry['data'], date_str))

            # ── DOM fallback ───────────────────────────────────────────────
            if not records:
                self.logger.info(f"{self.county}: no API records, trying DOM extraction")
                records = self._parse_dom(html_content, date_str)

            return records

        except Exception as exc:
            self.logger.error(f"{self.county}: Playwright error: {exc}", exc_info=True)
            return None

    def _fill_and_submit(self, page, date_str: str):
        """Try to fill date inputs and click Search."""
        try:
            # Date input selectors (Tyler Tech portal patterns)
            date_pairs = [
                ('input[placeholder*="Start" i]',     'input[placeholder*="End" i]'),
                ('input[id*="startDate" i]',           'input[id*="endDate" i]'),
                ('input[aria-label*="Start Date" i]',  'input[aria-label*="End Date" i]'),
                ('input[name*="start" i]',              'input[name*="end" i]'),
            ]
            for start_sel, end_sel in date_pairs:
                start_el = page.query_selector(start_sel)
                end_el   = page.query_selector(end_sel)
                if start_el and end_el:
                    start_el.triple_click()
                    start_el.fill(date_str)
                    end_el.triple_click()
                    end_el.fill(date_str)
                    self.logger.info(f"{self.county}: filled date fields")

                    # Submit
                    btn = (page.query_selector('button[type="submit"]')
                           or page.query_selector('button:has-text("Search")'))
                    if btn:
                        btn.click()
                        page.wait_for_load_state('networkidle')
                        page.wait_for_timeout(2000)
                    return
        except Exception as e:
            self.logger.debug(f"{self.county}: form fill error: {e}")

    def _inject_api_calls(self, page, date_str: str):
        """Inject fetch() into the page to hit API endpoints."""
        for doc_type in self.doc_types:
            script = f"""
            (async () => {{
                const base = window.location.origin;
                const paths = ['/api/instruments', '/api/search', '/results'];
                const body = {{
                    searchType: 'Quick',
                    dateRangeType: 'Filing',
                    startDate: '{date_str}',
                    endDate: '{date_str}',
                    docType: '{doc_type}',
                    page: 0,
                    pageSize: 200,
                }};
                for (const p of paths) {{
                    try {{
                        // GET
                        const qs = new URLSearchParams(body).toString();
                        await fetch(base + p + '?' + qs, {{credentials: 'include'}});
                        // POST
                        await fetch(base + p, {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify(body),
                            credentials: 'include',
                        }});
                    }} catch(e) {{}}
                }}
            }})();
            """
            try:
                page.evaluate(script)
                page.wait_for_timeout(2000)
            except Exception:
                pass

    # ── REST API fallback ─────────────────────────────────────────────────────

    def _rest_api(self, date_str: str) -> List[Dict]:
        params = {
            'searchType':    'Quick',
            'dateRangeType': 'Filing',
            'startDate':     date_str,
            'endDate':       date_str,
            'docType':       'NTS',
            'page':          '0',
            'pageSize':      '200',
        }
        self.session.headers.update({'Accept': 'application/json'})
        for ep in ['/api/instruments', '/api/search']:
            resp = self.get(self.base_url + ep, params=params)
            if not resp:
                continue
            try:
                data    = resp.json()
                records = self._parse_json(data, date_str)
                if records:
                    return records
            except Exception:
                pass
        return []

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_json(self, data, date_str: str) -> List[Dict]:
        """Parse a JSON blob that might be instrument search results."""
        items = (
            data.get('results')
            or data.get('instruments')
            or data.get('data')
            or data.get('items')
            or (data if isinstance(data, list) else [])
        )
        if not isinstance(items, list) or not items:
            return []

        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rec = self._item_to_record(item, date_str)
            if rec:
                records.append(rec)
        return records

    def _item_to_record(self, item: Dict, date_str: str) -> Optional[Dict]:
        # Name
        raw_name = ''
        grantors = item.get('grantors') or item.get('grantor') or []
        if isinstance(grantors, list) and grantors:
            g = grantors[0]
            raw_name = g.get('name', '') if isinstance(g, dict) else str(g)
        elif isinstance(grantors, str):
            raw_name = grantors
        raw_name = raw_name or item.get('grantorName', '') or ''
        first, last = self.parse_name(raw_name) if raw_name else ('', '')

        # Address
        raw_addr = (
            item.get('siteAddress') or item.get('propertyAddress')
            or item.get('address') or item.get('legalDescription', '')
        )
        address, city, zip_code = self.parse_address(raw_addr) if raw_addr else ('', '', '')

        # Dates
        file_date = self._fmt(
            item.get('fileDate') or item.get('instrumentDate') or item.get('recordingDate') or date_str
        )
        sale_date = self._fmt(item.get('saleDate') or item.get('sale_date') or '')

        return self.build_record(
            first_name=first, last_name=last,
            address=address, city=city, zip_code=zip_code,
            file_date=file_date, sale_date=sale_date,
        )

    def _parse_dom(self, html: str, date_str: str) -> List[Dict]:
        """Extract records from rendered page HTML."""
        soup    = BeautifulSoup(html, 'lxml')
        records = []

        for table in soup.find_all('table'):
            hdrs = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            self.logger.info(f"{self.county}: table headers: {hdrs}")

            for tr in table.find_all('tr')[1:]:
                cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                if not any(cells):
                    continue
                row = ' '.join(cells)

                # Try to pull address
                m = re.search(
                    r'(\d+\s+[A-Z][A-Z0-9\s]+?(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY)[A-Z\s\.]*?)'
                    r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
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
