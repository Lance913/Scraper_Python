"""
PublicSearch.us Scraper (Neumo platform)
Counties: Bexar, Dallas, Tarrant, Denton, Johnson

Key insight: Quick Search accepts doc type as a search term.
Searching "NTS" returns only Notice of Trustee Sale records.
Date range dropdown has preset options — we click "Last 1 Month".

Flow:
  1. Load Quick Search
  2. Type "NTS" in search box
  3. Click "Last 1 Month" date range
  4. Click Search
  5. Parse results table (Grantor, DOC TYPE, Recorded Date, Property Address)
  6. Click each document to extract Sale Date from the NTS text
"""

import re
from datetime import date
from typing import List, Dict, Optional, Tuple
from bs4 import BeautifulSoup
from .base import BaseScraper


class PublicSearchScraper(BaseScraper):

    def __init__(self, county_slug: str, county_name: str):
        super().__init__(county_name)
        self.slug     = county_slug
        self.base_url = f"https://{county_slug}.tx.publicsearch.us"

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping {self.county} County for {target_date}")
        records = self._playwright_scrape()
        if records is None:
            records = []
        self.logger.info(f"{self.county}: {len(records)} NTS records")
        return records

    def _playwright_scrape(self) -> Optional[List[Dict]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ))
                page = context.new_page()
                page.set_default_timeout(30_000)

                # ── Load Quick Search ──────────────────────────────────────
                self.logger.info(f"{self.county}: loading quick search...")
                page.goto(self.base_url)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # ── Type "NTS" in search box ───────────────────────────────
                # Placeholder: "Search for grantor/grantee, subdivision, doc type, or doc#"
                search_typed = False
                for sel in [
                    'input[placeholder*="grantor" i]',
                    'input[placeholder*="search" i]',
                    'input[type="text"]:visible',
                    'input[type="search"]',
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0 and el.is_visible():
                            el.fill('NTS')
                            self.logger.info(f"{self.county}: typed NTS via '{sel}'")
                            search_typed = True
                            break
                    except Exception:
                        pass

                if not search_typed:
                    self.logger.warning(f"{self.county}: could not find search input")

                # ── Select "Last 1 Month" date range ──────────────────────
                date_selected = False
                for sel in [
                    'text=Last 1 Month',
                    'text=Last 30 Days',
                    'text=Last 3 Months',  # fallback
                    '[class*="dateRange" i]:has-text("Month")',
                ]:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            el.click()
                            page.wait_for_timeout(500)
                            self.logger.info(f"{self.county}: selected date range via '{sel}'")
                            date_selected = True
                            break
                    except Exception:
                        pass

                if not date_selected:
                    self.logger.warning(f"{self.county}: could not set date range")

                # ── Click Search ───────────────────────────────────────────
                for btn_sel in [
                    'button:has-text("Search")',
                    'img[alt*="Search" i]',
                    'button[type="submit"]',
                    '[class*="search-btn" i]',
                    '[class*="searchBtn" i]',
                ]:
                    try:
                        el = page.locator(btn_sel).first
                        if el.count() > 0:
                            el.click()
                            self.logger.info(f"{self.county}: clicked Search via '{btn_sel}'")
                            break
                    except Exception:
                        pass

                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(4000)

                body = page.inner_text('body')
                self.logger.info(f"{self.county} results: {body[:600]}")

                html_content = page.content()

                # ── Parse results table ────────────────────────────────────
                rows = self._extract_rows(html_content)
                self.logger.info(f"{self.county}: found {len(rows)} rows")

                # ── Fetch each document for sale date ──────────────────────
                records = []
                for row in rows:
                    rec = self._enrich_with_sale_date(page, row)
                    records.append(rec)

                browser.close()
                return records

        except Exception as exc:
            self.logger.error(f"{self.county}: error: {exc}", exc_info=True)
            return None

    def _extract_rows(self, html: str) -> List[Dict]:
        soup = BeautifulSoup(html, 'lxml')
        rows = []
        all_doc_types = set()

        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers:
                continue

            h  = {v: i for i, v in enumerate(headers)}
            gi = h.get('grantor', -1)
            di = h.get('doc type', -1)
            ri = h.get('recorded date', -1)
            ai = h.get('property address', h.get('legal description', h.get('town', -1)))
            ni = h.get('doc number', h.get('inst number', -1))

            for tr in table.find_all('tr')[1:]:
                cells   = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
                tds_raw = tr.find_all('td')
                if not cells or not any(c.strip() for c in cells):
                    continue

                def cell(i):
                    return cells[i].strip() if 0 <= i < len(cells) else ''

                doc_type = cell(di)
                all_doc_types.add(doc_type)
                grantor  = cell(gi)
                rec_date = cell(ri)
                addr_raw = cell(ai)

                if not grantor:
                    continue

                # Get doc link
                doc_link = ''
                if 0 <= ni < len(tds_raw):
                    a = tds_raw[ni].find('a')
                    if a and a.get('href'):
                        href = a['href']
                        doc_link = href if href.startswith('http') else self.base_url + href

                first, last = self.parse_name(grantor)
                address, city, zip_c = self._split_address(addr_raw)

                rows.append({
                    'first_name': first, 'last_name':  last,
                    'address':    address, 'city': city, 'zip_code': zip_c,
                    'file_date':  self._fmt(rec_date),
                    'doc_type':   doc_type, 'doc_link': doc_link,
                })

        self.logger.info(f"{self.county}: doc types in results: {all_doc_types}")
        return rows

    def _enrich_with_sale_date(self, page, row: Dict) -> Dict:
        sale_date = ''
        address   = row.get('address', '')
        city      = row.get('city', '')
        zip_c     = row.get('zip_code', '')

        if row.get('doc_link'):
            try:
                page.goto(row['doc_link'])
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                text = BeautifulSoup(page.content(), 'lxml').get_text(' ', strip=True)
                self.logger.info(f"{self.county}: doc detail sample: {text[:300]}")

                for pat in [
                    r'Sale\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'Date\s+of\s+Sale[:\s]+(\d{1,2}/\d{1,2}/\d{4})',
                    r'first\s+Tuesday.*?(\w+\s+\d{1,2},\s+\d{4})',
                    r'sold\s+on\s+(\w+\s+\d{1,2},\s+\d{4})',
                ]:
                    m = re.search(pat, text, re.I)
                    if m:
                        sale_date = m.group(1).strip()
                        self.logger.info(f"{self.county}: sale date: {sale_date}")
                        break

                # Better address from document text
                if not address:
                    m2 = re.search(
                        r'(\d+\s+[A-Z0-9][A-Z0-9\s]+?'
                        r'(?:ST|AVE|DR|RD|LN|BLVD|CT|WAY|PL|CIR|TRAIL|PKWY|LOOP|HWY)[A-Z\s\.]*?)'
                        r',?\s+([A-Z][A-Z\s]+?),?\s+TX\s*(\d{5})',
                        text, re.I
                    )
                    if m2:
                        address = m2.group(1).strip().title()
                        city    = m2.group(2).strip().title()
                        zip_c   = m2.group(3)

            except Exception as e:
                self.logger.warning(f"{self.county}: detail error: {e}")

        return self.build_record(
            first_name=row.get('first_name', ''), last_name=row.get('last_name', ''),
            address=address, city=city, zip_code=zip_c,
            file_date=row.get('file_date', ''), sale_date=self._fmt(sale_date),
        )

    @staticmethod
    def _split_address(raw: str) -> Tuple[str, str, str]:
        if not raw or raw in ('N/A', ''):
            return '', '', ''
        parts  = [p.strip() for p in raw.split(',')]
        street = parts[0].title() if parts else ''
        city, zip_c = '', ''
        for part in parts[1:]:
            m = re.search(r'\b(\d{5})\b', part)
            if m:
                zip_c = m.group(1)
                continue
            if re.match(r'^(TX|TEXAS)$', part.strip(), re.I):
                continue
            if not city and part.strip():
                city = part.strip().title()
        return street, city, zip_c

    @staticmethod
    def _fmt(raw: str) -> str:
        if not raw:
            return ''
        raw = str(raw).strip()
        if re.match(r'^\d{1,2}/\d{1,2}/\d{4}$', raw):
            return raw
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', raw)
        if m:
            return f"{m.group(2)}/{m.group(3)}/{m.group(1)}"
        return raw
