"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

Search is working. Fixes:
1. DOM parser now uses column-index extraction (no regex address matching)
2. Extracts GRANTOR, DOC TYPE, RECORDED DATE, PROPERTY ADDRESS by column name
3. Includes all doc types so NTS records aren't filtered out
4. Searches last 30 days to capture notices filed earlier in the month
5. Parses "STREET, CITY, TEXAS, ZIP" address format correctly
"""

import re
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper


def to_mddyyyy(iso: str) -> str:
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
        # Search last 30 days — NTS notices for July 7 auction filed from ~June 1
        start_iso = (target_date - timedelta(days=30)).strftime('%Y-%m-%d')
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

        start_fmt = to_mddyyyy(start_iso)
        end_fmt   = to_mddyyyy(end_iso)

        captured_json = []

        def on_response(response):
            try:
                if response.status != 200:
                    return
                if 'json' not in response.headers.get('content-type', ''):
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

                # Load portal and go to Advanced Search
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                page.locator('text=Advanced Search').first.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # Fill date range (confirmed working: input[id*="start"])
                filled = self._fill_date_range(page, start_fmt, end_fmt)
                self.logger.info(f"{self.county}: date fill → {filled}")

                # Click Search
                for btn_sel in ['button[type="submit"]', 'button:has-text("Search")']:
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
                self.logger.info(f"{self.county} results: {body[:600]}")

                html_content = page.content()
                browser.close()

            # Try API responses first
            records = []
            for entry in captured_json:
                records.extend(self._parse_json(entry['data'], end_iso))

            # DOM extraction (primary for this portal)
            if not records:
                records = self._parse_dom(html_content, end_iso)

            return records

        except Exception as exc:
            self.logger.error(f"{self.county}: Playwright error: {exc}", exc_info=True)
            return None

    def _fill_date_range(self, page, start_fmt: str, end_fmt: str) -> str:
        pairs = [
            ('input[id*="start" i]',          'input[id*="end" i]'),
            ('input[placeholder="Start date"]','input[placeholder="End date"]'),
            ('input[placeholder*="Start" i]',  'input[placeholder*="End" i]'),
            ('input[aria-label*="Start" i]',   'input[aria-label*="End" i]'),
        ]
        for start_sel, end_sel in pairs:
            try:
                s = page.locator(start_sel)
                e = page.locator(end_sel)
                if s.count() > 0 and e.count() > 0:
                    page.fill(start_sel, start_fmt)
                    page.fill(end_sel, end_fmt)
                    page.wait_for_timeout(300)
                    return f"filled '{start_sel}' with {start_fmt}/{end_fmt}"
            except Exception:
                pass

        # Last resort: first two visible text inputs
        try:
            inputs = [i for i in page.locator('input[type="text"], input:not([type])').all() if i.is_visible()]
            if len(inputs) >= 2:
                inputs[0].fill(start_fmt)
                inputs[1].fill(end_fmt)
                return f"filled first 2 visible inputs"
        except Exception as e:
            self.logger.warning(f"{self.county}: fallback fill failed: {e}")

        return "no fields filled"

    # ── DOM parser ────────────────────────────────────────────────────────────

    def _parse_dom(self, html: str, date_str: str) -> List[Dict]:
        soup    = BeautifulSoup(html, 'lxml')
        records = []

        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers:
                continue

            self.logger.info(f"{self.county}: results table headers: {headers}")

            # Map column names to indices
            h  = {v: i for i, v in enumerate(headers)}
            gi = h.get('grantor', -1)
            di = h.get('doc type', -1)
            ri = h.get('recorded date', -1)
            # Address column varies by county
            ai = h.get('property address',
                 h.get('legal description',
                 h.get('town', -1)))

            rows_extracted = 0
            for tr in table.find_all('tr')[1:]:
                cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                if not cells or not any(c.strip() for c in cells):
                    continue

                grantor  = cells[gi].strip()  if (gi >= 0 and gi < len(cells))  else ''
                doc_type = cells[di].strip()  if (di >= 0 and di < len(cells))  else ''
                rec_date = cells[ri].strip()  if (ri >= 0 and ri < len(cells))  else date_str
                addr_raw = cells[ai].strip()  if (ai >= 0 and ai < len(cells))  else ''

                if not grantor:
                    continue

                first, last           = self.parse_name(grantor)
                address, city, zip_c  = self._split_address(addr_raw)

                records.append(self.build_record(
                    first_name=first, last_name=last,
                    address=address, city=city, zip_code=zip_c,
                    file_date=self._fmt(rec_date),
                    sale_date='',
                ))
                rows_extracted += 1

            self.logger.info(f"{self.county}: extracted {rows_extracted} rows")

        return records

    @staticmethod
    def _split_address(raw: str) -> Tuple[str, str, str]:
        """Parse 'STREET, CITY, TEXAS, 78239' → (street, city, zip)."""
        if not raw:
            return '', '', ''
        parts   = [p.strip() for p in raw.split(',')]
        street  = parts[0].title() if parts else ''
        city    = ''
        zip_c   = ''
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

    # ── JSON parser (if API responses captured) ───────────────────────────────

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
            raw_name = ''
            grantors = item.get('grantors') or item.get('grantor') or []
            if isinstance(grantors, list) and grantors:
                g = grantors[0]
                raw_name = g.get('name', '') if isinstance(g, dict) else str(g)
            elif isinstance(grantors, str):
                raw_name = grantors
            first, last = self.parse_name(raw_name) if raw_name else ('', '')
            raw_addr = (item.get('siteAddress') or item.get('propertyAddress')
                        or item.get('address') or '')
            address, city, zip_c = self._split_address(raw_addr) if raw_addr else ('', '', '')
            file_date = self._fmt(item.get('fileDate') or item.get('recordingDate') or date_str)
            sale_date = self._fmt(item.get('saleDate') or '')
            records.append(self.build_record(
                first_name=first, last_name=last,
                address=address, city=city, zip_code=zip_c,
                file_date=file_date, sale_date=sale_date,
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
