"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

The ViewECdocs.aspx document viewer lives in a separate IIS app from the
search portal — session tokens do not carry across, so detail pages always
return 404 regardless of whether we use HTTP or Playwright. Names/addresses
are only inside scanned document images (not extractable via text).

DECISION: Store doc_id (FRCL-XXXX-XXXX) in the sheet so George's team can
look up names/addresses manually on the Harris portal if needed.

Table column layout: [checkbox][doc_id][sale_date][file_date][pages]
Month strategy: scrape current + next month each daily run.
Dedup: handled by sheets_writer using county+doc_id.
"""

import re
from datetime import date
from typing import List, Dict
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

        months_to_scrape = [(target_date.year, target_date.month)]
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
                    self.logger.warning(f"Harris: Search click error: {e}")
                page.wait_for_timeout(3000)

                content = page.content()
                browser.close()

            soup  = BeautifulSoup(content, 'lxml')
            rows  = self._parse_results_table(soup)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_val}")
            if rows:
                self.logger.info(f"Harris: sample: {rows[0]}")

            # No detail page fetch — ViewECdocs lives in a separate IIS app;
            # its session tokens are not shareable. Names/addresses unavailable.
            for row in rows:
                records.append(self.build_record(
                    doc_id=    row.get('doc_id', ''),
                    file_date= row.get('file_date', ''),
                    sale_date= row.get('sale_date', ''),
                ))

        except Exception as exc:
            self.logger.error(f"Harris {month_str} {year_val} error: {exc}", exc_info=True)

        return records

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        """
        Confirmed column layout:
          tds[0] = checkbox (empty)
          tds[1] = Doc ID (FRCL-YYYY-XXXX)
          tds[2] = Sale Date (MM/DD/YYYY)
          tds[3] = File Date (MM/DD/YYYY)
          tds[4] = Pages
        """
        rows = []

        target_table = None
        for t in soup.find_all('table'):
            if 'FRCL' in t.get_text() and re.search(r'\d{2}/\d{2}/\d{4}', t.get_text()):
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

            rows.append({
                'doc_id':    doc_id,
                'sale_date': sale_date,
                'file_date': file_date,
            })

        return rows
