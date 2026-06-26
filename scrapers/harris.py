"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

The portal uses ASP.NET WebForms with __VIEWSTATE. We:
  1. GET the page to capture ViewState tokens
  2. POST with the current year/month to retrieve that month's filings
  3. Filter results to today's file date
  4. Fetch each document detail page to extract grantor name + property address
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
    1: 'January', 2: 'February', 3: 'March', 4: 'April',
    5: 'May', 6: 'June', 7: 'July', 8: 'August',
    9: 'September', 10: 'October', 11: 'November', 12: 'December',
}


class HarrisCountyScraper(BaseScraper):

    def __init__(self):
        super().__init__('Harris')

    # ── Public entry point ────────────────────────────────────────────────────

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping Harris County for {target_date}")
        records = []

        # Step 1: load page and grab ASP.NET tokens
        resp = self.get(SEARCH_URL)
        if not resp:
            self.logger.error("Could not load Harris County search page")
            return records

        soup = BeautifulSoup(resp.text, 'lxml')
        vs_data = self._extract_viewstate(soup)

        # Step 2: POST with selected year/month
        year  = str(target_date.year)
        month = MONTH_NAMES[target_date.month]

        post_data = {
            **vs_data,
            '__EVENTTARGET':   '',
            '__EVENTARGUMENT': '',
            # Adjust these control IDs if the portal changes its naming
            'ctl00$ContentPlaceHolder1$ddlYear':  year,
            'ctl00$ContentPlaceHolder1$ddlMonth': month,
            'ctl00$ContentPlaceHolder1$btnSearch': 'Search',
        }

        resp2 = self.post(SEARCH_URL, data=post_data)
        if not resp2:
            self.logger.error("Harris County POST failed")
            return records

        soup2 = BeautifulSoup(resp2.text, 'lxml')
        rows  = self._parse_results_table(soup2)
        self.logger.info(f"Harris: found {len(rows)} rows for {month} {year}")

        # Step 3: filter to today, fetch detail for each match
        target_str = target_date.strftime('%-m/%-d/%Y')  # e.g. "6/25/2026"
        for row in rows:
            if row.get('file_date') != target_str:
                continue
            detail = self._fetch_detail(row.get('detail_url', ''))
            if detail:
                records.append(self.build_record(**detail, **row))
            time.sleep(0.5)

        self.logger.info(f"Harris: {len(records)} new records for {target_date}")
        return records

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_viewstate(self, soup: BeautifulSoup) -> Dict:
        def val(field_id):
            tag = soup.find('input', {'id': field_id})
            return tag['value'] if tag else ''
        return {
            '__VIEWSTATE':          val('__VIEWSTATE'),
            '__VIEWSTATEGENERATOR': val('__VIEWSTATEGENERATOR'),
            '__EVENTVALIDATION':    val('__EVENTVALIDATION'),
        }

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows = []
        # Harris renders results in a GridView table
        table = (
            soup.find('table', id=re.compile(r'grd', re.I))
            or soup.find('table', class_=re.compile(r'grid|result', re.I))
            or soup.find('table')
        )
        if not table:
            return rows

        for tr in table.find_all('tr')[1:]:  # skip header
            tds = tr.find_all('td')
            if len(tds) < 3:
                continue

            link_tag   = tds[0].find('a')
            doc_id     = tds[0].get_text(strip=True)
            sale_date  = tds[1].get_text(strip=True)
            file_date  = tds[2].get_text(strip=True)
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

    def _fetch_detail(self, url: str) -> Dict:
        if not url:
            return {}

        resp = self.get(url)
        if not resp:
            return {}

        soup = BeautifulSoup(resp.text, 'lxml')
        text = soup.get_text(' ', strip=True)

        # --- Grantor / owner name ---
        first, last = '', ''
        m = re.search(r'Grantor[:\s]+([A-Z][A-Z\s,\.]+?)(?:Grantee|Trustee|Said|Dated)', text, re.I)
        if m:
            raw_name = m.group(1).strip().strip(',')
            first, last = self.parse_name(raw_name)

        # --- Property address ---
        address, city, zip_code = '', '', ''
        # Try "123 MAIN ST, HOUSTON, TX 77001" pattern
        m2 = re.search(
            r'(\d+\s+[A-Z0-9][A-Z0-9\s]+?(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY)[A-Z\s\.]*)'
            r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
            text, re.I
        )
        if m2:
            address  = m2.group(1).strip().title()
            city     = m2.group(2).strip().title()
            zip_code = m2.group(3)

        return {
            'first_name': first,
            'last_name':  last,
            'address':    address,
            'city':       city,
            'state':      'TX',
            'zip_code':   zip_code,
        }
