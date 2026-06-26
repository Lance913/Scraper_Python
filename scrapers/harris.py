"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

The portal is ASP.NET WebForms with Telerik RadComboBox dropdowns.
Playwright + select_option / TreeWalker / $find all fail because
they don't fire the ASP.NET postback mechanism.

Solution: pure HTTP POST approach.
  1. GET the page → scrape ViewState / EventValidation
  2. POST with year selected → get updated form state
  3. POST with month selected → get results table with Doc ID / Sale Date / File Date
  4. Parse the table and filter to target_date

Handles both full-page postback responses (plain HTML) and
ASP.NET UpdatePanel partial-update responses (pipe-delimited).
"""

import re
import time
from datetime import date
from typing import List, Dict, Tuple, Optional
from bs4 import BeautifulSoup
from .base import BaseScraper

SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
BASE_URL   = "https://www.cclerk.hctx.net"

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
        records = []
        year_str  = str(target_date.year)
        month_str = MONTH_LABELS[target_date.month]

        # Normalize target date to "M/D/YYYY" (no leading zeros) for comparison
        target_file = f"{target_date.month}/{target_date.day}/{target_date.year}"

        try:
            # ── Step 1: GET initial page ───────────────────────────────────
            resp = self.get(SEARCH_URL)
            if not resp:
                self.logger.error("Harris: could not load portal")
                return []

            soup, form_data = self._parse_form(resp.text)
            self.logger.info(f"Harris: loaded portal, ViewState len={len(form_data.get('__VIEWSTATE',''))}")

            # ── Step 2: POST to select year ────────────────────────────────
            form_data['__EVENTTARGET']   = 'ctl00$ContentPlaceHolder1$ddlYear'
            form_data['__EVENTARGUMENT'] = ''
            form_data['ctl00$ContentPlaceHolder1$ddlYear']  = year_str
            form_data['ctl00$ContentPlaceHolder1$ddlMonth'] = 'Select -'

            resp2 = self.post(SEARCH_URL, data=form_data)
            if not resp2:
                self.logger.error("Harris: year POST failed")
                return []

            html2 = self._extract_html(resp2.text)
            soup2, form_data2 = self._parse_form(html2)
            self.logger.info(f"Harris: year={year_str} selected, response len={len(html2)}")

            # ── Step 3: POST to select month ───────────────────────────────
            form_data2['__EVENTTARGET']   = 'ctl00$ContentPlaceHolder1$ddlMonth'
            form_data2['__EVENTARGUMENT'] = ''
            form_data2['ctl00$ContentPlaceHolder1$ddlYear']  = year_str
            form_data2['ctl00$ContentPlaceHolder1$ddlMonth'] = month_str

            resp3 = self.post(SEARCH_URL, data=form_data2)
            if not resp3:
                self.logger.error("Harris: month POST failed")
                return []

            html3 = self._extract_html(resp3.text)
            soup3, _ = self._parse_form(html3)
            self.logger.info(f"Harris: month={month_str} selected, response len={len(html3)}")

            # ── Step 4: Parse results table ────────────────────────────────
            rows = self._parse_results_table(soup3)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_str}")

            # Show first 3 rows so we know what we're getting
            for r in rows[:3]:
                self.logger.info(f"Harris: row sample: {r}")

            # ── Step 5: Filter to target date and enrich ───────────────────
            for row in rows:
                if self._norm_date(row.get('file_date', '')) != target_file:
                    continue
                detail = self._fetch_detail(row.get('detail_url', ''))
                detail.update({
                    'file_date': row['file_date'],
                    'sale_date': row.get('sale_date', ''),
                })
                records.append(self.build_record(**detail))
                time.sleep(0.3)

        except Exception as exc:
            self.logger.error(f"Harris scraper error: {exc}", exc_info=True)

        self.logger.info(f"Harris: {len(records)} records for {target_date}")
        return records

    # ── ASP.NET form handling ─────────────────────────────────────────────────

    def _parse_form(self, html: str) -> Tuple[BeautifulSoup, dict]:
        """Extract all hidden form inputs into a dict."""
        soup = BeautifulSoup(html, 'lxml')
        data = {}
        for inp in soup.find_all('input'):
            name  = inp.get('name', '')
            value = inp.get('value', '')
            if name:
                data[name] = value
        # Also capture select values
        for sel in soup.find_all('select'):
            name = sel.get('name', '')
            if name:
                selected = sel.find('option', selected=True)
                data[name] = selected['value'] if selected and selected.get('value') else ''
        return soup, data

    def _extract_html(self, raw: str) -> str:
        """
        ASP.NET UpdatePanel responses look like:
          3|updatePanel|panelId|<html content>|...
        Full-page postbacks are plain HTML.
        Extract the HTML portion from either format.
        """
        if raw.startswith('<?xml') or raw.strip().startswith('<!DOCTYPE') or raw.strip().startswith('<html'):
            return raw  # already full HTML

        # UpdatePanel format: segments separated by | with length prefix
        # Pattern: length|type|id|content|
        try:
            # Find the largest updatePanel block which contains the main content
            best = ''
            i = 0
            while i < len(raw):
                pipe1 = raw.find('|', i)
                if pipe1 == -1:
                    break
                try:
                    length = int(raw[i:pipe1])
                except ValueError:
                    break
                pipe2 = raw.find('|', pipe1 + 1)
                if pipe2 == -1:
                    break
                block_type = raw[pipe1 + 1:pipe2]
                pipe3 = raw.find('|', pipe2 + 1)
                if pipe3 == -1:
                    break
                # block_id = raw[pipe2 + 1:pipe3]
                content_start = pipe3 + 1
                content = raw[content_start:content_start + length]
                if block_type == 'updatePanel' and len(content) > len(best):
                    best = content
                i = content_start + length + 1  # skip trailing |

            return best if best else raw
        except Exception:
            return raw

    # ── Results table ─────────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows  = []
        table = (
            soup.find('table', id=re.compile(r'grd|grid|result|foreclos', re.I))
            or soup.find('table', class_=re.compile(r'grd|grid|result', re.I))
            or self._find_data_table(soup)
        )
        if not table:
            self.logger.warning("Harris: no results table found in response")
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

    def _find_data_table(self, soup: BeautifulSoup) -> Optional[object]:
        """Fallback: find any table that has a date-like value in its cells."""
        for table in soup.find_all('table'):
            text = table.get_text()
            if re.search(r'\d{1,2}/\d{1,2}/\d{4}', text):
                return table
        return None

    # ── Document detail ───────────────────────────────────────────────────────

    def _fetch_detail(self, url: str) -> Dict:
        if not url:
            return {'first_name': '', 'last_name': '', 'address': '', 'city': '', 'zip_code': ''}
        resp = self.get(url)
        if not resp:
            return {'first_name': '', 'last_name': '', 'address': '', 'city': '', 'zip_code': ''}
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

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_date(d: str) -> str:
        """Normalize '06/26/2026' → '6/26/2026' for comparison."""
        m = re.match(r'^0?(\d{1,2})/0?(\d{1,2})/(\d{4})$', d.strip())
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else d
