"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

Working approach (confirmed):
  - Playwright + set native select values + click btnSearch
  - Portal returns 706 rows for June 2026 auction

Table column layout:  [checkbox][doc_id][sale_date][file_date][pages]
  tds[0] = checkbox (empty)
  tds[1] = Doc ID (FRCL-YYYY-XXXX) — has the detail link
  tds[2] = Sale Date (MM/DD/YYYY)
  tds[3] = File Date (MM/DD/YYYY)
  tds[4] = Pages

Month strategy: scrape BOTH current and next month each run.
  June 26 → scrape June (current) + July (next auction, July 7)
  Dedup key in sheets_writer prevents re-writing same records.
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

        # Scrape current month + next month (next auction)
        months_to_scrape = []
        months_to_scrape.append((target_date.year, target_date.month))
        # Add next month
        if target_date.month == 12:
            months_to_scrape.append((target_date.year + 1, 1))
        else:
            months_to_scrape.append((target_date.year, target_date.month + 1))

        all_records = []
        seen_keys   = set()
        for year, month in months_to_scrape:
            rows = self._scrape_month(year, month)
            for row in rows:
                key = f"{row.get('doc_id')}|{row.get('file_date')}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_records.append(row)

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

                self.logger.info(f"Harris: loading portal for {month_str} {year_val}...")
                page.goto(SEARCH_URL)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # Set year
                page.evaluate(f"""
                    () => {{
                        var s = document.querySelector('select[name="{YEAR_NAME}"]');
                        if (s) s.value = '{year_val}';
                        s && s.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                """)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # Set month
                page.evaluate(f"""
                    () => {{
                        var s = document.querySelector('select[name="{MONTH_NAME}"]');
                        if (s) s.value = '{month_val}';
                        s && s.dispatchEvent(new Event('change', {{bubbles:true}}));
                    }}
                """)
                page.wait_for_timeout(300)

                # Click Search
                try:
                    with page.expect_navigation(wait_until='networkidle', timeout=20_000):
                        page.click(f'input[name="{SEARCH_NAME}"]')
                    self.logger.info(f"Harris: Search clicked for {month_str} {year_val}")
                except Exception as e:
                    self.logger.warning(f"Harris: Search click: {e}")
                page.wait_for_timeout(3000)

                content = page.content()
                browser.close()

            soup  = BeautifulSoup(content, 'lxml')
            rows  = self._parse_results_table(soup)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_val}")
            if rows:
                self.logger.info(f"Harris: sample row: {rows[0]}")

            # Fetch detail for each row to get name + address
            for row in rows:
                detail = self._fetch_detail(row.get('detail_url', ''))
                records.append(self.build_record(
                    first_name=detail.get('first_name', ''),
                    last_name=detail.get('last_name', ''),
                    address=detail.get('address', ''),
                    city=detail.get('city', ''),
                    zip_code=detail.get('zip_code', ''),
                    file_date=row.get('file_date', ''),
                    sale_date=row.get('sale_date', ''),
                ))
                time.sleep(0.2)

        except Exception as exc:
            self.logger.error(f"Harris {month_str} {year_val} error: {exc}", exc_info=True)

        return records

    # ── Results table ─────────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Column layout confirmed from logs:
          tds[0] = checkbox (empty)
          tds[1] = Doc ID   (FRCL-YYYY-XXXX) — link is here
          tds[2] = Sale Date
          tds[3] = File Date
          tds[4] = Pages
        """
        rows = []

        # Find the right table — it has FRCL doc IDs and dates
        target_table = None
        for t in soup.find_all('table'):
            text = t.get_text()
            if 'FRCL' in text and re.search(r'\d{2}/\d{2}/\d{4}', text):
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

            # Only keep actual data rows
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
                detail_url = href if href.startswith('http') else BASE_URL + href

            rows.append({
                'doc_id':     doc_id,
                'sale_date':  sale_date,
                'file_date':  file_date,
                'detail_url': detail_url,
            })

        return rows

    # ── Document detail ───────────────────────────────────────────────────────

    def _fetch_detail(self, url: str) -> Dict:
        empty = {'first_name': '', 'last_name': '', 'address': '', 'city': '', 'zip_code': ''}
        if not url:
            return empty
        resp = self.get(url)
        if not resp:
            return empty
        text  = BeautifulSoup(resp.text, 'lxml').get_text(' ', strip=True)
        first, last = '', ''
        m = re.search(r'Grantor[:\s]+([A-Z][A-Z\s,\.]+?)(?:Grantee|Trustee|Said|Dated)', text, re.I)
        if m:
            first, last = self.parse_name(m.group(1))
        address, city, zip_code = '', '', ''
        m2 = re.search(
            r'(\d+\s+[A-Z0-9][A-Z0-9\s]+?(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY)[A-Z\s\.]*?)'
            r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
            text, re.I
        )
        if m2:
            address  = m2.group(1).strip().title()
            city     = m2.group(2).strip().title()
            zip_code = m2.group(3)
        return {'first_name': first, 'last_name': last,
                'address': address, 'city': city, 'state': 'TX', 'zip_code': zip_code}
