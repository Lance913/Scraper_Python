"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx
"""

import re
import time
from datetime import date
from typing import List, Dict, Optional
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

                # Debug: log all visible text to find element names
                body_text = page.inner_text('body')
                self.logger.info(f"Harris page text sample: {body_text[:500]}")

                # ── Click year ─────────────────────────────────────────────
                # Try multiple selector patterns
                year_clicked = False
                for sel in [
                    f"text={year_str}",
                    f"a:text-is('{year_str}')",
                    f"li:text-is('{year_str}')",
                    f"span:text-is('{year_str}')",
                    f"[id*='Year']:has-text('{year_str}')",
                ]:
                    el = page.query_selector(sel)
                    if el:
                        el.click()
                        page.wait_for_load_state('networkidle')
                        page.wait_for_timeout(1500)
                        self.logger.info(f"Harris: clicked year via '{sel}'")
                        year_clicked = True
                        break

                if not year_clicked:
                    self.logger.warning(f"Harris: could not click year {year_str}")

                # Debug: log page text after year click
                body_text2 = page.inner_text('body')
                self.logger.info(f"Harris page after year click: {body_text2[:500]}")

                # ── Click month ────────────────────────────────────────────
                month_clicked = False
                for sel in [
                    f"text={month_str}",
                    f"a:text-is('{month_str}')",
                    f"li:text-is('{month_str}')",
                    f"span:text-is('{month_str}')",
                    f"[id*='Month']:has-text('{month_str}')",
                    # Try selecting if it's a <select> dropdown
                ]:
                    el = page.query_selector(sel)
                    if el:
                        el.click()
                        page.wait_for_load_state('networkidle')
                        page.wait_for_timeout(1500)
                        self.logger.info(f"Harris: clicked month via '{sel}'")
                        month_clicked = True
                        break

                # Fallback: try <select> dropdown for month
                if not month_clicked:
                    for sel_id in ['select', '[id*="Month"]', '[id*="month"]']:
                        try:
                            page.select_option(sel_id, label=month_str)
                            page.wait_for_load_state('networkidle')
                            self.logger.info(f"Harris: selected month via select_option '{sel_id}'")
                            month_clicked = True
                            break
                        except Exception:
                            pass

                if not month_clicked:
                    self.logger.warning(f"Harris: could not click month {month_str}")

                content = page.content()
                browser.close()

            # ── Parse results ──────────────────────────────────────────────
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows  = []
        table = (
            soup.find('table', id=re.compile(r'grd|grid|result', re.I))
            or soup.find('table', class_=re.compile(r'grd|grid|result', re.I))
            or soup.find('table')
        )
        if not table:
            self.logger.warning("Harris: no results table found in HTML")
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
        text   = BeautifulSoup(resp.text, 'lxml').get_text(' ', strip=True)
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
