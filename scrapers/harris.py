"""
Harris County Foreclosure Scraper
TreeWalker confirmed year/month are in <OPTION> elements (standard <select>).
Fix: use page.select_option() to set the dropdowns properly.
"""

import re
import time
from datetime import date
from typing import List, Dict
from bs4 import BeautifulSoup
from .base import BaseScraper

SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
BASE_URL   = "https://www.cclerk.hctx.net"

MONTH_NAMES = {
    1: 'January', 2: 'February', 3: 'March',    4: 'April',
    5: 'May',     6: 'June',     7: 'July',      8: 'August',
    9: 'September', 10: 'October', 11: 'November', 12: 'December',
}


class HarrisCountyScraper(BaseScraper):

    def __init__(self):
        super().__init__('Harris')

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping Harris County for {target_date}")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error("Playwright not installed.")
            return []

        records     = []
        year_str    = str(target_date.year)
        month_str   = MONTH_NAMES[target_date.month]
        target_file = f"{target_date.month}/{target_date.day}/{target_date.year}"

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page    = browser.new_page()
                page.set_default_timeout(30_000)

                self.logger.info("Harris: loading portal...")
                page.goto(SEARCH_URL)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # ── Inspect all <select> elements ──────────────────────────
                select_info = page.evaluate("""
                    () => {
                        return Array.from(document.querySelectorAll('select')).map((s, i) => ({
                            index: i,
                            id: s.id,
                            name: s.name,
                            options: Array.from(s.options).map(o => o.text.trim()).slice(0, 6)
                        }));
                    }
                """)
                self.logger.info(f"Harris selects: {select_info}")

                # ── Select year ────────────────────────────────────────────
                year_done = False
                for info in select_info:
                    if year_str in info['options']:
                        sel = f"select#{info['id']}" if info['id'] else f"select[name='{info['name']}']" if info['name'] else f"select:nth-of-type({info['index']+1})"
                        try:
                            page.select_option(sel, label=year_str)
                            page.wait_for_load_state('networkidle')
                            page.wait_for_timeout(1500)
                            self.logger.info(f"Harris: selected year via '{sel}'")
                            year_done = True
                            break
                        except Exception as e:
                            self.logger.warning(f"Harris: year select error on '{sel}': {e}")

                if not year_done:
                    # Fallback: try all selects
                    for sel_str in ['select']:
                        try:
                            page.select_option(sel_str, label=year_str)
                            page.wait_for_load_state('networkidle')
                            page.wait_for_timeout(1500)
                            self.logger.info("Harris: year selected via generic select")
                            year_done = True
                            break
                        except Exception:
                            pass

                # Re-inspect selects after year selection (month select may update)
                select_info2 = page.evaluate("""
                    () => {
                        return Array.from(document.querySelectorAll('select')).map((s, i) => ({
                            index: i,
                            id: s.id,
                            name: s.name,
                            options: Array.from(s.options).map(o => o.text.trim()).slice(0, 15)
                        }));
                    }
                """)
                self.logger.info(f"Harris selects after year: {select_info2}")

                # ── Select month ───────────────────────────────────────────
                month_done = False
                for info in select_info2:
                    if month_str in info['options']:
                        sel = f"select#{info['id']}" if info['id'] else f"select[name='{info['name']}']" if info['name'] else f"select:nth-of-type({info['index']+1})"
                        try:
                            page.select_option(sel, label=month_str)
                            page.wait_for_load_state('networkidle')
                            page.wait_for_timeout(1500)
                            self.logger.info(f"Harris: selected month via '{sel}'")
                            month_done = True
                            break
                        except Exception as e:
                            self.logger.warning(f"Harris: month select error on '{sel}': {e}")

                body = page.inner_text('body')
                self.logger.info(f"Harris body after selections: {body[:800]}")

                content = page.content()
                browser.close()

            soup = BeautifulSoup(content, 'lxml')
            rows = self._parse_results_table(soup)
            self.logger.info(f"Harris: {len(rows)} rows found for {month_str} {year_str}")

            for row in rows:
                if row.get('file_date') != target_file:
                    continue
                detail = self._fetch_detail(row.get('detail_url', ''))
                detail.update({
                    'county':    self.county,
                    'file_date': row['file_date'],
                    'sale_date': row.get('sale_date', ''),
                })
                records.append(self.build_record(**detail))
                time.sleep(0.4)

        except Exception as exc:
            self.logger.error(f"Harris scraper error: {exc}", exc_info=True)

        self.logger.info(f"Harris: {len(records)} records for {target_date}")
        return records

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows  = []
        table = (
            soup.find('table', id=re.compile(r'grd|grid|result', re.I))
            or soup.find('table', class_=re.compile(r'grd|grid|result', re.I))
            or soup.find('table')
        )
        if not table:
            self.logger.warning("Harris: no results table in HTML")
            return rows
        for tr in table.find_all('tr')[1:]:
            tds = tr.find_all('td')
            if len(tds) < 3:
                continue
            link_tag   = tds[0].find('a')
            detail_url = ''
            if link_tag and link_tag.get('href'):
                href = link_tag['href']
                detail_url = href if href.startswith('http') else BASE_URL + href
            rows.append({
                'doc_id':     tds[0].get_text(strip=True),
                'sale_date':  tds[1].get_text(strip=True),
                'file_date':  tds[2].get_text(strip=True),
                'detail_url': detail_url,
            })
        return rows

    def _fetch_detail(self, url: str) -> Dict:
        if not url:
            return {}
        resp = self.get(url)
        if not resp:
            return {}
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
