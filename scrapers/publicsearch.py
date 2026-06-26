"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

These three counties all use Tyler Technologies publicsearch.us, which is a
React Single Page Application.  Data is loaded via JavaScript API calls.

Strategy:
  1. Launch headless Chromium via Playwright
  2. Intercept every JSON API response the React app makes internally
  3. Find the response that contains instrument/foreclosure records
  4. Parse names and addresses from those records
  5. Falls back to REST GET if Playwright not available
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

    # ── Entry point ───────────────────────────────────────────────────────────

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        date_str = target_date.strftime('%Y-%m-%d')

        # Primary: Playwright (intercepts real API calls)
        records = self._playwright_scrape(date_str)
        if records is not None:
            self.logger.info(f"{self.county}: {len(records)} records")
            return records

        # Fallback: try REST API directly
        records = self._rest_api(date_str)
        self.logger.info(f"{self.county}: {len(records)} records (via REST fallback)")
        return records

    # ── Playwright scraper ────────────────────────────────────────────────────

    def _playwright_scrape(self, date_str: str) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.warning("Playwright not installed — skipping Playwright scrape")
            return None

        captured = []   # intercepted JSON responses

        def on_response(response):
            try:
                ct = response.headers.get('content-type', '')
                if 'json' not in ct:
                    return
                if response.status != 200:
                    return
                data = response.json()
                # Only keep responses that look like instrument search results
                if self._looks_like_results(data):
                    captured.append(data)
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

                self.logger.info(f"{self.county}: loading portal...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')

                # ── Try to apply date filter ───────────────────────────────
                self._apply_date_filter(page, date_str)

                # ── Also try the search API directly from within page ──────
                self._trigger_api_search(page, date_str)

                page.wait_for_load_state('networkidle')
                browser.close()

            # Parse all captured API responses
            records = []
            for data in captured:
                records.extend(self._parse_results(data, date_str))

            # Deduplicate by address
            seen = set()
            deduped = []
            for r in records:
                key = r.get('address', '').lower().strip()
                if key and key not in seen:
                    seen.add(key)
                    deduped.append(r)

            return deduped

        except Exception as exc:
            self.logger.error(f"{self.county}: Playwright error: {exc}", exc_info=True)
            return None

    def _apply_date_filter(self, page, date_str: str):
        """Try to fill date fields and submit search on the portal."""
        try:
            # Common patterns for date inputs on publicsearch.us
            selectors = [
                ('input[placeholder*="Start" i]', 'input[placeholder*="End" i]'),
                ('input[id*="start" i]',           'input[id*="end" i]'),
                ('input[name*="start" i]',          'input[name*="end" i]'),
                ('input[aria-label*="start" i]',    'input[aria-label*="end" i]'),
            ]

            for start_sel, end_sel in selectors:
                start_el = page.query_selector(start_sel)
                end_el   = page.query_selector(end_sel)
                if start_el and end_el:
                    start_el.triple_click()
                    start_el.type(date_str)
                    end_el.triple_click()
                    end_el.type(date_str)

                    # Try to click Search button
                    btn = page.query_selector('button[type="submit"]') or \
                          page.query_selector('button:has-text("Search")')
                    if btn:
                        btn.click()
                        page.wait_for_load_state('networkidle', timeout=10_000)
                    return
        except Exception:
            pass

    def _trigger_api_search(self, page, date_str: str):
        """
        Inject a fetch() call into the page to hit the API endpoint directly.
        This is a reliable trick for React SPAs — the page already has auth
        cookies/tokens in its context, so the API call succeeds.
        """
        for doc_type in self.doc_types:
            script = f"""
            (async () => {{
                const endpoints = [
                    '/api/instruments',
                    '/api/search',
                    '/api/instruments/search',
                ];
                const payload = {{
                    searchType: 'Quick',
                    dateRangeType: 'Filing',
                    startDate: '{date_str}',
                    endDate: '{date_str}',
                    docType: '{doc_type}',
                    page: 0,
                    pageSize: 200,
                }};
                for (const ep of endpoints) {{
                    try {{
                        // GET
                        const qs = new URLSearchParams(payload).toString();
                        await fetch(ep + '?' + qs, {{credentials: 'include'}});
                        // POST
                        await fetch(ep, {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/json'}},
                            body: JSON.stringify(payload),
                            credentials: 'include',
                        }});
                    }} catch(e) {{}}
                }}
            }})();
            """
            try:
                page.evaluate(script)
                page.wait_for_load_state('networkidle', timeout=8_000)
            except Exception:
                pass

    # ── REST fallback (no browser) ────────────────────────────────────────────

    def _rest_api(self, date_str: str) -> List[Dict]:
        self.session.headers.update({
            'Accept':       'application/json',
            'Content-Type': 'application/json',
            'Referer':      self.base_url + '/',
            'Origin':       self.base_url,
        })

        params = {
            'searchType':    'Quick',
            'dateRangeType': 'Filing',
            'startDate':     date_str,
            'endDate':       date_str,
            'docType':       ','.join(self.doc_types),
            'page':          '0',
            'pageSize':      '200',
        }

        for endpoint in ['/api/instruments', '/api/search', '/api/instruments/search']:
            resp = self.get(self.base_url + endpoint, params=params)
            if not resp:
                continue
            try:
                data = resp.json()
                records = self._parse_results(data, date_str)
                if records:
                    return records
            except Exception:
                continue

        return []

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _looks_like_results(data) -> bool:
        """Return True if a JSON blob looks like instrument search results."""
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return any(k in first for k in ('grantor', 'grantors', 'siteAddress', 'instrumentNumber', 'fileDate'))
        if isinstance(data, dict):
            return any(k in data for k in ('results', 'instruments', 'data', 'total'))
        return False

    def _parse_results(self, data, date_str: str) -> List[Dict]:
        items = (
            data.get('results')
            or data.get('instruments')
            or data.get('data')
            or (data if isinstance(data, list) else [])
        )
        if not isinstance(items, list):
            return []

        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            rec = self._parse_item(item, date_str)
            if rec:
                records.append(rec)
        return records

    def _parse_item(self, item: Dict, date_str: str) -> Optional[Dict]:
        # --- Name ---
        raw_name = ''
        grantors = item.get('grantors') or item.get('grantor') or []
        if isinstance(grantors, list) and grantors:
            g = grantors[0]
            raw_name = g.get('name', '') if isinstance(g, dict) else str(g)
        elif isinstance(grantors, str):
            raw_name = grantors
        raw_name = raw_name or item.get('grantorName', '') or ''
        first, last = self.parse_name(raw_name) if raw_name else ('', '')

        # --- Address ---
        raw_addr = (
            item.get('siteAddress')
            or item.get('propertyAddress')
            or item.get('address')
            or item.get('legalDescription', '')
        )
        address, city, zip_code = self.parse_address(raw_addr) if raw_addr else ('', '', '')

        # --- Dates ---
        file_date = self._fmt(
            item.get('fileDate') or item.get('instrumentDate') or item.get('recordingDate') or date_str
        )
        sale_date = self._fmt(item.get('saleDate') or item.get('sale_date') or '')

        return self.build_record(
            first_name=first, last_name=last,
            address=address, city=city, zip_code=zip_code,
            file_date=file_date, sale_date=sale_date,
        )

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
