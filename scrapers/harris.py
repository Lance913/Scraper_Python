"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

Key insight: ViewECdocs.aspx uses a session token in the ?ID= param.
The token is only valid inside the same browser session that generated it.
HTTP requests from a different session get 404.
FIX: Stay inside the same Playwright browser to fetch detail pages.

Table column layout: [checkbox][doc_id][sale_date][file_date][pages]
"""

import re
import time
from datetime import date
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from .base import BaseScraper

SEARCH_URL  = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
BASE_URL    = "https://www.cclerk.hctx.net"
YEAR_NAME   = 'ctl00$ContentPlaceHolder1$ddlYear'
MONTH_NAME  = 'ctl00$ContentPlaceHolder1$ddlMonth'
SEARCH_NAME = 'ctl00$ContentPlaceHolder1$btnSearch'

MONTH_LABELS = {
    1: 'January', 2: 'February', 3: 'March',    4: 'April',
    5: 'May',     6: 'June',     7: 'July',      8: 'August',
    9: 'September', 10: 'October', 11: 'November', 12: 'December',
}


class HarrisCountyScraper(BaseScraper):

    def __init__(self):
        super().__init__('Harris')

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping Harris County for {target_date}")

        months_to_scrape = []
        months_to_scrape.append((target_date.year, target_date.month))
        if target_date.month == 12:
            months_to_scrape.append((target_date.year + 1, 1))
        else:
            months_to_scrape.append((target_date.year, target_date.month + 1))

        all_records = []
        for year, month in months_to_scrape:
            all_records.extend(self._scrape_month(year, month))

        self.logger.info(f"Harris: {len(all_records)} total records")
        return all_records

    def _scrape_month(self, year: int, month: int) -> List[Dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return []

        year_val  = str(year)
        month_val = str(month)
        month_str = MONTH_LABELS[month]
        records   = []

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                page = browser.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page.set_default_timeout(30_000)

                # ── 1. Load search results ───────────────────────────────
                self.logger.info(f"Harris: loading portal for {month_str} {year_val}...")
                page.goto(SEARCH_URL)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                page.evaluate(f"""
                    () => {{
                        var s = document.querySelector('select[name="{YEAR_NAME}"]');
                        if (s) s.value = '{year_val}';
                        s && s.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                """)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                page.evaluate(f"""
                    () => {{
                        var s = document.querySelector('select[name="{MONTH_NAME}"]');
                        if (s) s.value = '{month_val}';
                        s && s.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                """)
                page.wait_for_timeout(300)

                try:
                    with page.expect_navigation(wait_until='networkidle', timeout=20_000):
                        page.click(f'input[name="{SEARCH_NAME}"]')
                    self.logger.info(f"Harris: Search clicked for {month_str} {year_val}")
                except Exception as e:
                    self.logger.warning(f"Harris: Search click: {e}")
                page.wait_for_timeout(3000)

                content = page.content()
                soup    = BeautifulSoup(content, 'lxml')
                rows    = self._parse_results_table(soup)
                self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_val}")
                if rows:
                    self.logger.info(f"Harris: sample row: {rows[0]}")

                # ── 2. Fetch detail pages WITHIN the same browser session ─
                # The ?ID= token is session-scoped; HTTP requests from a
                # different session get 404. Stay inside Playwright.
                results_url = page.url

                for row in rows:
                    detail_url = row.get('detail_url', '')
                    first, last, address, city, zip_code = '', '', '', '', ''

                    if detail_url:
                        try:
                            page.goto(detail_url)
                            page.wait_for_load_state('networkidle', timeout=12_000)
                            page.wait_for_timeout(500)
                            detail_text = page.inner_text('body')
                            if not records:  # Log first detail page only
                                self.logger.info(
                                    f"Harris DETAIL PAGE SAMPLE "
                                    f"({len(detail_text)} chars): "
                                    + detail_text[:500].replace(chr(10),' ')[:300]
                                )
                            parsed   = self._parse_detail_text(detail_text)
                            first    = parsed.get('first_name', '')
                            last     = parsed.get('last_name', '')
                            address  = parsed.get('address', '')
                            city     = parsed.get('city', '')
                            zip_code = parsed.get('zip_code', '')
                        except Exception as e:
                            self.logger.warning(f"Harris: detail page error: {e}")

                    records.append(self.build_record(
                        first_name=first,
                        last_name=last,
                        address=address,
                        city=city,
                        zip_code=zip_code,
                        file_date=row.get('file_date', ''),
                        sale_date=row.get('sale_date', ''),
                    ))
                    page.wait_for_timeout(150)

                browser.close()

        except Exception as exc:
            self.logger.error(f"Harris {month_str} {year_val} error: {exc}", exc_info=True)

        return records

    # ── Results table ──────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Confirmed column layout:
          tds[0] = checkbox (empty)
          tds[1] = Doc ID (FRCL-YYYY-XXXX) — link here
          tds[2] = Sale Date (MM/DD/YYYY)
          tds[3] = File Date (MM/DD/YYYY)
          tds[4] = Pages
        """
        rows = []

        target_table = None
        for t in soup.find_all('table'):
            txt = t.get_text()
            if 'FRCL' in txt and re.search(r'\d{2}/\d{2}/\d{4}', txt):
                target_table = t
                break

        if not target_table:
            self.logger.warning("Harris: no results table found")
            return rows

        for tr in target_table.find_all('tr'):
            tds = tr.find_all('td')
            if len(tds) < 4:
                continue

            doc_id    = tds[1].get_text(strip=True)
            sale_date = tds[2].get_text(strip=True)
            file_date = tds[3].get_text(strip=True)

            if not doc_id.startswith('FRCL'):
                continue
            if not re.match(r'\d{1,2}/\d{1,2}/\d{4}', sale_date):
                continue
            if not re.match(r'\d{1,2}/\d{1,2}/\d{4}', file_date):
                continue

            link_tag   = tds[1].find('a')
            detail_url = ''
            if link_tag and link_tag.get('href'):
                href = link_tag['href']
                if href.startswith('http'):
                    detail_url = href
                elif href.startswith('/'):
                    detail_url = BASE_URL + href
                else:
                    detail_url = BASE_URL + '/' + href

            rows.append({
                'doc_id':     doc_id,
                'sale_date':  sale_date,
                'file_date':  file_date,
                'detail_url': detail_url,
            })

        return rows

    # ── Detail page parsing ────────────────────────────────────────────────

    def _parse_detail_text(self, text: str) -> Dict:
        """Parse grantor name and property address from the detail page text."""
        result = {'first_name': '', 'last_name': '', 'address': '', 'city': '', 'zip_code': ''}

        # Grantor name (multiple patterns for different doc formats)
        name_patterns = [
            r'Grantor[:\s]+([A-Z][A-Z\s,\.]+?)(?:\n|Grantee|Trustee|Said|Dated|$)',
            r'GRANTOR[:\s]+([A-Z][A-Z\s,\.]+?)(?:\n|GRANTEE|TRUSTEE)',
            r'Mortgagor[:\s]+([A-Z][A-Z\s,\.]+?)(?:\n|Mortgagee)',
        ]
        for pat in name_patterns:
            m = re.search(pat, text, re.I)
            if m:
                raw = m.group(1).strip().strip(',').strip()
                if len(raw) > 2:
                    result['first_name'], result['last_name'] = self.parse_name(raw)
                    break

        # Address — look for street number pattern near city/TX/zip
        addr_pat = (
            r'(\d+\s+[A-Z0-9][A-Z0-9\s]+?'
            r'(?:STREET|ST|AVENUE|AVE|DRIVE|DR|ROAD|RD|LANE|LN|BOULEVARD|BLVD|'
            r'COURT|CT|WAY|PLACE|PL|CIRCLE|CIR|TRAIL|TRL|PARKWAY|PKWY)[A-Z\s\.]*?)'
            r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})'
        )
        m2 = re.search(addr_pat, text, re.I)
        if m2:
            result['address']  = m2.group(1).strip().title()
            result['city']     = m2.group(2).strip().title()
            result['zip_code'] = m2.group(3)

        return result
