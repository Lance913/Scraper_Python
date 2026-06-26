"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

HTTP POST approach — bypasses Telerik RadComboBox UI entirely.
  GET page → capture ViewState → single POST with year+month → parse table.

UpdatePanel detection: check if response starts with digit (pipe-delimited format)
vs full HTML (starts with <).
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
        records   = []
        year_str  = str(target_date.year)
        month_str = MONTH_LABELS[target_date.month]
        target_file = f"{target_date.month}/{target_date.day}/{target_date.year}"

        try:
            # ── GET initial page ───────────────────────────────────────────
            resp = self.get(SEARCH_URL)
            if not resp:
                self.logger.error("Harris: could not load portal")
                return []

            soup, form_data = self._parse_form(resp.text)
            self.logger.info(f"Harris: loaded portal (ViewState len={len(form_data.get('__VIEWSTATE',''))})")
            self.logger.info(f"Harris: form fields: {[k for k in form_data if not k.startswith('__')]}")

            # ── Step 1: select year ────────────────────────────────────────
            # Some ASP.NET sites need the year change before month can be selected
            form_data['__EVENTTARGET']   = 'ctl00$ContentPlaceHolder1$ddlYear'
            form_data['__EVENTARGUMENT'] = ''
            form_data['ctl00$ContentPlaceHolder1$ddlYear']  = year_str
            form_data['ctl00$ContentPlaceHolder1$ddlMonth'] = 'Select -'

            resp2 = self.post(SEARCH_URL, data=form_data)
            if not resp2:
                self.logger.error("Harris: year POST failed")
                return []

            html2 = self._extract_html(resp2.text)
            self.logger.info(f"Harris: year POST → {len(html2)} chars; first 300: {repr(html2[:300])}")

            soup2, form2 = self._parse_form(html2)

            # ── Step 2: select month ───────────────────────────────────────
            form2['__EVENTTARGET']   = 'ctl00$ContentPlaceHolder1$ddlMonth'
            form2['__EVENTARGUMENT'] = ''
            form2['ctl00$ContentPlaceHolder1$ddlYear']  = year_str
            form2['ctl00$ContentPlaceHolder1$ddlMonth'] = month_str

            resp3 = self.post(SEARCH_URL, data=form2)
            if not resp3:
                self.logger.error("Harris: month POST failed")
                return []

            html3 = self._extract_html(resp3.text)
            self.logger.info(f"Harris: month POST → {len(html3)} chars; first 600: {repr(html3[:600])}")

            soup3 = BeautifulSoup(html3, 'lxml')

            # Log ALL tables found
            all_tables = soup3.find_all('table')
            self.logger.info(f"Harris: {len(all_tables)} tables in response")
            for i, t in enumerate(all_tables[:5]):
                rows = t.find_all('tr')
                self.logger.info(
                    f"Harris: table[{i}] id={t.get('id')} "
                    f"class={t.get('class')} rows={len(rows)} "
                    f"text[:80]={t.get_text(' ', strip=True)[:80]}"
                )

            rows = self._parse_results_table(soup3)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_str}")
            for r in rows[:3]:
                self.logger.info(f"Harris: sample row: {r}")

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
            self.logger.error(f"Harris error: {exc}", exc_info=True)

        self.logger.info(f"Harris: {len(records)} records for {target_date}")
        return records

    # ── Form handling ─────────────────────────────────────────────────────────

    def _parse_form(self, html: str) -> Tuple[BeautifulSoup, dict]:
        soup = BeautifulSoup(html, 'lxml')
        data = {}
        for inp in soup.find_all('input'):
            name  = inp.get('name', '')
            value = inp.get('value', '')
            if name:
                data[name] = value
        for sel in soup.find_all('select'):
            name = sel.get('name', '')
            if name:
                selected = sel.find('option', selected=True)
                data[name] = selected['value'] if selected and selected.get('value') else ''
        return soup, data

    def _extract_html(self, raw: str) -> str:
        """
        Handle both full HTML responses and ASP.NET UpdatePanel partial-update responses.
        UpdatePanel format: length|type|id|content| (starts with a digit)
        """
        stripped = raw.strip()

        # If it looks like HTML, return as-is
        if stripped.startswith('<') or stripped.startswith('<!'):
            return raw

        # Otherwise attempt UpdatePanel parsing
        # Format: len|type|id|content| for each segment
        best = ''
        i = 0
        while i < len(raw):
            pipe1 = raw.find('|', i)
            if pipe1 == -1:
                break
            length_str = raw[i:pipe1]
            try:
                length = int(length_str)
            except ValueError:
                break
            pipe2 = raw.find('|', pipe1 + 1)
            if pipe2 == -1:
                break
            block_type = raw[pipe1 + 1:pipe2]
            pipe3 = raw.find('|', pipe2 + 1)
            if pipe3 == -1:
                break
            content_start = pipe3 + 1
            content = raw[content_start:content_start + length]
            if block_type in ('updatePanel', 'pageContent') and len(content) > len(best):
                best = content
            i = content_start + length + 1  # skip trailing |
            if i >= len(raw):
                break

        self.logger.info(f"Harris: _extract_html: format={'updatePanel' if best else 'fullHTML'}, extracted={len(best or raw)} chars")
        return best if best else raw

    # ── Results table ─────────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows = []
        # Try ID patterns first
        table = (
            soup.find('table', id=re.compile(r'grd|grid|result|foreclos|frcl', re.I))
            or soup.find('table', class_=re.compile(r'grd|grid|result|data', re.I))
        )
        # Fallback: any table with a date AND a link
        if not table:
            for t in soup.find_all('table'):
                text = t.get_text()
                if re.search(r'\d{1,2}/\d{1,2}/\d{4}', text) and t.find('a'):
                    table = t
                    break
        # Last resort: any table with 3+ columns
        if not table:
            for t in soup.find_all('table'):
                trs = t.find_all('tr')
                if trs and len(trs[0].find_all(['th', 'td'])) >= 3:
                    table = t
                    self.logger.info("Harris: using last-resort table")
                    break

        if not table:
            self.logger.warning("Harris: no results table found")
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

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _norm_date(d: str) -> str:
        m = re.match(r'^0?(\d{1,2})/0?(\d{1,2})/(\d{4})$', d.strip())
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else d
