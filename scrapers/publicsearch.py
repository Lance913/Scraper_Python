"""
PublicSearch.us Scraper (shared by Bexar, Dallas, Tarrant)

All three counties use the Tyler Technologies publicsearch.us portal.
The platform exposes a REST API that we call directly — no browser needed.

API endpoint pattern:
  POST https://{county}.tx.publicsearch.us/results
  Content-Type: application/json
  Body: { searchType, docType, dateRangeType, startDate, endDate, ... }

Document type for foreclosure notices in TX = "NTS"
  (Notice of Substitute Trustee's Sale / Notice of Trustee Sale)

If the API returns a 404 or unexpected format, the scraper falls back to
HTML parsing of the search results page.  See _html_fallback().
"""

import json
import re
import time
from datetime import date
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from .base import BaseScraper


class PublicSearchScraper(BaseScraper):
    """
    county_slug : e.g. 'bexar', 'dallas', 'tarrant'
    county_name : display name e.g. 'Bexar'
    doc_types   : list of document type codes to search (default ['NTS'])
    """

    def __init__(self, county_slug: str, county_name: str, doc_types=None):
        super().__init__(county_name)
        self.slug      = county_slug
        self.base_url  = f"https://{county_slug}.tx.publicsearch.us"
        self.doc_types = doc_types or ['NTS', 'FRCL', 'NOTSALE']

        # API call headers
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept':       'application/json',
            'Referer':      self.base_url + '/',
            'Origin':       self.base_url,
        })

    # ── Public entry point ────────────────────────────────────────────────────

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        date_str = target_date.strftime('%Y-%m-%d')

        # Try REST API first
        records = self._api_search(date_str)
        if records is None:
            self.logger.warning(f"{self.county}: API failed, trying HTML fallback")
            records = self._html_fallback(date_str)

        self.logger.info(f"{self.county}: {len(records)} records found")
        return records

    # ── REST API approach ─────────────────────────────────────────────────────

    def _api_search(self, date_str: str) -> Optional[List[Dict]]:
        """
        POST to the /results endpoint with a JSON payload.
        Returns a list of records, or None if the call fails.
        """
        payload = {
            "searchType":    "Quick",
            "resultType":    "Document",
            "docGroup":      "Real Property",
            "docType":       self.doc_types,
            "keywords":      "",
            "dateRangeType": "Filing",
            "startDate":     date_str,
            "endDate":       date_str,
            "page":          0,
            "pageSize":      200,
        }

        resp = self.post(
            self.base_url + '/results',
            data=json.dumps(payload),
        )

        if not resp:
            return None

        try:
            data = resp.json()
        except json.JSONDecodeError:
            self.logger.warning(f"{self.county}: API response not JSON")
            return None

        # Different API versions use different top-level keys
        results_list = (
            data.get('results')
            or data.get('instruments')
            or data.get('data')
            or (data if isinstance(data, list) else None)
        )

        if results_list is None:
            self.logger.warning(f"{self.county}: Unexpected API shape: {list(data.keys())}")
            return None

        records = []
        for item in results_list:
            rec = self._parse_api_item(item, date_str)
            if rec:
                records.append(rec)
        return records

    def _parse_api_item(self, item: Dict, date_str: str) -> Optional[Dict]:
        """
        Normalise a single API result object into our canonical record dict.
        Field names vary between API versions — we try all known variants.
        """
        # --- Grantor (owner) name ---
        raw_name = ''
        grantors = item.get('grantors') or item.get('grantor') or []
        if isinstance(grantors, list) and grantors:
            raw_name = grantors[0].get('name', '') if isinstance(grantors[0], dict) else str(grantors[0])
        elif isinstance(grantors, str):
            raw_name = grantors
        if not raw_name:
            raw_name = item.get('grantorName', '') or item.get('grantor_name', '')

        first, last = self.parse_name(raw_name) if raw_name else ('', '')

        # --- Address ---
        raw_addr = (
            item.get('siteAddress')
            or item.get('propertyAddress')
            or item.get('address')
            or item.get('legalDescription', '')
        )
        address, city, zip_code = self.parse_address(raw_addr) if raw_addr else ('', '', '')

        # --- Dates ---
        file_date = (
            item.get('fileDate')
            or item.get('instrumentDate')
            or item.get('recordingDate')
            or date_str
        )
        sale_date = (
            item.get('saleDate')
            or item.get('sale_date')
            or ''
        )

        # Reformat dates to MM/DD/YYYY for consistency
        file_date = self._fmt_date(file_date)
        sale_date = self._fmt_date(sale_date)

        return self.build_record(
            first_name=first,
            last_name=last,
            address=address,
            city=city,
            state='TX',
            zip_code=zip_code,
            file_date=file_date,
            sale_date=sale_date,
        )

    # ── HTML fallback ─────────────────────────────────────────────────────────

    def _html_fallback(self, date_str: str) -> List[Dict]:
        """
        If the API call fails, scrape the search-results HTML page directly.
        The portal renders results in a table or repeating div structure.
        """
        url = (
            f"{self.base_url}/results?"
            f"searchType=Quick&dateRangeType=Filing"
            f"&startDate={date_str}&endDate={date_str}"
            f"&docType=NTS"
        )

        resp = self.get(url, headers={'Accept': 'text/html'})
        if not resp:
            return []

        soup  = BeautifulSoup(resp.text, 'lxml')
        rows  = []

        # Try table rows
        table = soup.find('table')
        if table:
            for tr in table.find_all('tr')[1:]:
                tds = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(tds) >= 4:
                    rows.append(tds)

        # Try card/div layout
        if not rows:
            for card in soup.find_all(class_=re.compile(r'result|instrument|row', re.I)):
                text = card.get_text(' ', strip=True)
                rows.append([text])

        records = []
        for row in rows:
            rec = self._parse_html_row(row, date_str)
            if rec:
                records.append(rec)
        return records

    def _parse_html_row(self, row, date_str: str) -> Optional[Dict]:
        """Best-effort parsing of an HTML result row."""
        text = ' '.join(str(c) for c in row)

        # Name
        m_name = re.search(r'Grantor[:\s]+([A-Z][A-Z\s,\.]+?)(?:Grantee|Doc|Date)', text, re.I)
        first, last = ('', '')
        if m_name:
            first, last = self.parse_name(m_name.group(1))

        # Address
        m_addr = re.search(
            r'(\d+\s+[A-Z][A-Z0-9\s]+?(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY)[A-Z\s\.]*)'
            r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
            text, re.I
        )
        address, city, zip_code = ('', '', '')
        if m_addr:
            address  = m_addr.group(1).strip().title()
            city     = m_addr.group(2).strip().title()
            zip_code = m_addr.group(3)

        # Sale date
        m_sale = re.search(r'(?:Sale|Auction)\s+Date[:\s]+([\d/\-]+)', text, re.I)
        sale_date = self._fmt_date(m_sale.group(1)) if m_sale else ''

        return self.build_record(
            first_name=first,
            last_name=last,
            address=address,
            city=city,
            state='TX',
            zip_code=zip_code,
            file_date=self._fmt_date(date_str),
            sale_date=sale_date,
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_date(raw: str) -> str:
        """Convert YYYY-MM-DD or other formats to MM/DD/YYYY."""
        if not raw:
            return ''
        raw = raw.strip()
        # Already MM/DD/YYYY
        if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', raw):
            return raw
        # YYYY-MM-DD
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
        if m:
            return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
        return raw
