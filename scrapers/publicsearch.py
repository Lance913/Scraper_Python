"""
PublicSearch.us Scraper — Bexar, Dallas, Tarrant

Data confirmed working. Improvements:
- Bexar: has PROPERTY ADDRESS column → parse street/city/zip correctly
- Dallas: has TOWN + LEGAL DESCRIPTION → use TOWN for city, skip legal descriptions
- Tarrant: has LEGAL DESCRIPTION with "CITY, Subdivision:..." → parse city from it
- All: extract DOC TYPE so user can filter in Sheets for NTS specifically
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
        # 30-day window: NTS for July 7 auction filed from ~June 1+
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
                captured_json.append(data)
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

                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                page.locator('text=Advanced Search').first.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                filled = self._fill_date_range(page, start_fmt, end_fmt)
                self.logger.info(f"{self.county}: date fill → {filled}")

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
                browser.close()

            records = []
            for data in captured_json:
                records.extend(self._parse_json(data, end_iso))

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
                if page.locator(start_sel).count() > 0 and page.locator(end_sel).count() > 0:
                    page.fill(start_sel, start_fmt)
                    page.fill(end_sel, end_fmt)
                    page.wait_for_timeout(300)
                    return f"filled '{start_sel}'"
            except Exception:
                pass
        try:
            inputs = [i for i in page.locator('input[type="text"], input:not([type])').all() if i.is_visible()]
            if len(inputs) >= 2:
                inputs[0].fill(start_fmt)
                inputs[1].fill(end_fmt)
                return "filled first 2 visible inputs"
        except Exception as e:
            self.logger.warning(f"{self.county}: fill fallback error: {e}")
        return "no fields filled"

    # ── DOM parser ────────────────────────────────────────────────────────────

    def _parse_dom(self, html: str, date_str: str) -> List[Dict]:
        soup    = BeautifulSoup(html, 'lxml')
        records = []

        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers:
                continue

            self.logger.info(f"{self.county}: table headers: {headers}")
            h  = {v: i for i, v in enumerate(headers)}

            gi = h.get('grantor', -1)
            di = h.get('doc type', -1)
            ri = h.get('recorded date', -1)

            # Address columns vary by county
            # Bexar has 'property address'
            # Dallas has 'legal description' + 'town' (city)
            # Tarrant has 'legal description' (contains city)
            addr_i = h.get('property address', -1)
            town_i = h.get('town', -1)
            desc_i = h.get('legal description', -1)

            count = 0
            for tr in table.find_all('tr')[1:]:
                cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                if not cells or not any(c.strip() for c in cells):
                    continue

                def cell(i):
                    return cells[i].strip() if 0 <= i < len(cells) else ''

                grantor  = cell(gi)
                doc_type = cell(di)
                rec_date = cell(ri) or date_str
                if not grantor:
                    continue

                first, last = self.parse_name(grantor)
                address, city, zip_c = self._extract_address(
                    cell(addr_i), cell(town_i), cell(desc_i)
                )

                records.append(self.build_record(
                    first_name=first, last_name=last,
                    address=address, city=city, zip_code=zip_c,
                    file_date=self._fmt(rec_date),
                    sale_date='',
                ))
                count += 1

            self.logger.info(f"{self.county}: extracted {count} rows")

        return records

    def _extract_address(self, prop_addr: str, town: str, legal_desc: str) -> Tuple[str, str, str]:
        """
        Bexar:  prop_addr = "6518 LOWRIE BLOCK, SAN ANTONIO, TEXAS, 78239"
        Dallas: town = "GARLAND", legal_desc = "Subdivision - Name: ..."
        Tarrant: legal_desc = "FORT WORTH, Subdivision: COBBS ORCHARD, ..."
        """

        # ── Bexar: full address in prop_addr ──────────────────────────────
        if prop_addr and prop_addr != 'N/A':
            return self._split_csv_address(prop_addr)

        # ── Dallas: TOWN column has city, legal_desc has legal description ─
        if town and town not in ('N/A', ''):
            city = town.strip().title()
            return '', city, ''

        # ── Tarrant: legal_desc starts with "CITY, Subdivision: ..." ──────
        if legal_desc and legal_desc not in ('N/A', ''):
            # Try "FORT WORTH, Subdivision: ..." pattern
            m = re.match(r'^([A-Z][A-Z\s]+?),\s*(?:Subdivision|Survey|Lot)', legal_desc, re.I)
            if m:
                return '', m.group(1).strip().title(), ''

        return '', '', ''

    @staticmethod
    def _split_csv_address(raw: str) -> Tuple[str, str, str]:
        """Parse 'STREET, CITY, TEXAS, 78239' → (street, city, zip)."""
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

    def _parse_json(self, data, date_str: str) -> List[Dict]:
        items = (data.get('results') or data.get('instruments') or
                 data.get('data') or (data if isinstance(data, list) else []))
        if not isinstance(items, list):
            return []
        records = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_name = ''
            g = item.get('grantors') or item.get('grantor') or []
            if isinstance(g, list) and g:
                raw_name = g[0].get('name', '') if isinstance(g[0], dict) else str(g[0])
            elif isinstance(g, str):
                raw_name = g
            first, last = self.parse_name(raw_name) if raw_name else ('', '')
            raw_addr = item.get('siteAddress') or item.get('propertyAddress') or item.get('address', '')
            addr, city, zip_c = self._split_csv_address(raw_addr) if raw_addr else ('', '', '')
            records.append(self.build_record(
                first_name=first, last_name=last,
                address=addr, city=city, zip_code=zip_c,
                file_date=self._fmt(item.get('fileDate') or date_str),
                sale_date=self._fmt(item.get('saleDate') or ''),
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
