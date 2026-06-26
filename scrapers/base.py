import requests
import logging
import re
import time
from datetime import date
from typing import List, Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


class BaseScraper:
    """Shared utilities for all county scrapers."""

    def __init__(self, county_name: str):
        self.county = county_name
        self.logger = logging.getLogger(county_name)
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def scrape(self, target_date: date) -> List[Dict]:
        raise NotImplementedError("Each county scraper must implement scrape()")

    # ── Name helpers ─────────────────────────────────────────────────────────

    def parse_name(self, full_name: str):
        """
        Split 'JOHN A DOE' → ('John', 'A Doe').
        Strips 'ET AL', 'ET UX', '& JANE', etc.
        Returns (first_name, last_name).
        """
        full_name = full_name.strip().upper()
        # Remove noise after AND / ET AL / ET UX / &
        full_name = re.sub(
            r'\s+(ET\s+AL|ET\s+UX|AND\s+|&\s+).*$', '', full_name, flags=re.I
        ).strip()
        # Remove trailing punctuation
        full_name = full_name.strip(',.;')
        parts = full_name.split()
        if len(parts) >= 2:
            return parts[0].title(), ' '.join(parts[1:]).title()
        elif len(parts) == 1:
            return '', parts[0].title()
        return '', full_name.title()

    # ── Address helpers ───────────────────────────────────────────────────────

    def parse_address(self, raw: str):
        """
        Try to pull street / city / zip from a raw address string.
        Returns (address, city, zip_code).
        """
        raw = raw.strip()
        # Pattern: "123 Main St, Houston, TX 77001"
        m = re.search(
            r'^([\d]+[^,]+),\s*([A-Za-z\s]+),\s*TX\s*(\d{5})',
            raw, re.I
        )
        if m:
            return m.group(1).strip(), m.group(2).strip().title(), m.group(3)

        # Fallback: return full string as address
        return raw, '', ''

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def get(self, url: str, **kwargs) -> Optional[requests.Response]:
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=30, **kwargs)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                self.logger.warning(f"GET {url} attempt {attempt+1} failed: {e}")
                time.sleep(2 ** attempt)
        return None

    def post(self, url: str, **kwargs) -> Optional[requests.Response]:
        for attempt in range(3):
            try:
                r = self.session.post(url, timeout=30, **kwargs)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                self.logger.warning(f"POST {url} attempt {attempt+1} failed: {e}")
                time.sleep(2 ** attempt)
        return None

    def build_record(self, **kwargs) -> Dict:
        """Normalise field names into the canonical output dict."""
        return {
            'first_name':  kwargs.get('first_name', ''),
            'last_name':   kwargs.get('last_name', ''),
            'address':     kwargs.get('address', ''),
            'city':        kwargs.get('city', ''),
            'state':       kwargs.get('state', 'TX'),
            'zip_code':    kwargs.get('zip_code', ''),
            'county':      self.county,
            'file_date':   kwargs.get('file_date', ''),
            'sale_date':   kwargs.get('sale_date', ''),
        }
