"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

Telerik RadComboBox stores selection in a hidden *_ClientState JSON field,
not in the native <select> value.  We set BOTH the native value AND the
ClientState, then click the "Search" submit button (btnSearch) to load
the results table.

Flow:
  1. Load page
  2. Log all hidden inputs so we can see ClientState field names
  3. Set year native select + ClientState + dispatch change
  4. Wait for month dropdown to repopulate (networkidle)
  5. Set month native select + ClientState + dispatch change
  6. Click btnSearch → results load
  7. Parse results table (Doc ID / Sale Date / File Date)
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

# Telerik ClientState field names (underscores, not dollar signs)
YEAR_STATE_NAME  = 'ctl00_ContentPlaceHolder1_ddlYear_ClientState'
MONTH_STATE_NAME = 'ctl00_ContentPlaceHolder1_ddlMonth_ClientState'

MONTH_LABELS = {
    1: 'January', 2: 'February', 3: 'March',    4: 'April',
    5: 'May',     6: 'June',     7: 'July',      8: 'August',
    9: 'September', 10: 'October', 11: 'November', 12: 'December',
}


def _telerik_state(value: str, text: str) -> str:
    """Build Telerik RadComboBox ClientState JSON."""
    import json
    return json.dumps({
        "logEntries": [], "value": value, "text": text,
        "enabled": True, "checkedIndices": [], "checkedItemsTextOverflows": False
    }, separators=(',', ':'))


class HarrisCountyScraper(BaseScraper):

    def __init__(self):
        super().__init__('Harris')

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping Harris County for {target_date}")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error("Playwright not installed")
            return []

        records     = []
        year_val    = str(target_date.year)
        month_val   = str(target_date.month)
        month_str   = MONTH_LABELS[target_date.month]
        target_file = f"{target_date.month}/{target_date.day}/{target_date.year}"

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

                self.logger.info("Harris: loading portal...")
                page.goto(SEARCH_URL)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # ── Log ALL hidden inputs so we can see ClientState fields ─
                all_inputs = page.evaluate("""
                    () => Array.from(document.querySelectorAll('input'))
                         .filter(i => i.name)
                         .map(i => i.name + '|' + i.type)
                         .join(', ')
                """)
                self.logger.info(f"Harris: all inputs: {all_inputs}")

                # ── Year: set native select + ClientState + change event ───
                year_state = _telerik_state(year_val, year_val)
                yr = page.evaluate(f"""
                    () => {{
                        // Native select
                        var sel = document.querySelector('select[name="{YEAR_NAME}"]');
                        if (sel) sel.value = '{year_val}';

                        // Telerik ClientState
                        var cs = document.querySelector(
                            'input[name="{YEAR_STATE_NAME}"], ' +
                            'input[id="{YEAR_STATE_NAME}"]'
                        );
                        if (cs) cs.value = {repr(year_state)};

                        // Fire native change event so ASP.NET picks it up
                        if (sel) sel.dispatchEvent(new Event('change', {{bubbles:true}}));

                        return 'year: sel=' + (sel ? sel.value : 'none') +
                               ' cs=' + (cs ? 'found' : 'not found');
                    }}
                """)
                self.logger.info(f"Harris: year set → {yr}")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)

                # ── Month: set native select + ClientState + change event ──
                month_state = _telerik_state(month_val, month_str)
                mo = page.evaluate(f"""
                    () => {{
                        var sel = document.querySelector('select[name="{MONTH_NAME}"]');
                        if (sel) sel.value = '{month_val}';

                        var cs = document.querySelector(
                            'input[name="{MONTH_STATE_NAME}"], ' +
                            'input[id="{MONTH_STATE_NAME}"]'
                        );
                        if (cs) cs.value = {repr(month_state)};

                        if (sel) sel.dispatchEvent(new Event('change', {{bubbles:true}}));

                        return 'month: sel=' + (sel ? sel.value : 'none') +
                               ' cs=' + (cs ? 'found' : 'not found');
                    }}
                """)
                self.logger.info(f"Harris: month set → {mo}")
                page.wait_for_timeout(500)

                # ── Click Search button ────────────────────────────────────
                try:
                    with page.expect_navigation(wait_until='networkidle', timeout=20_000):
                        page.click(f'input[name="{SEARCH_NAME}"], button[name="{SEARCH_NAME}"]')
                    self.logger.info("Harris: Search button clicked")
                except Exception as e:
                    self.logger.warning(f"Harris: Search click: {e}")
                page.wait_for_timeout(4000)

                body = page.inner_text('body')
                self.logger.info(f"Harris body: {body[:600]}")

                content = page.content()
                browser.close()

            # ── Parse results ──────────────────────────────────────────────
            soup = BeautifulSoup(content, 'lxml')
            rows = self._parse_results_table(soup)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_val}")
            for r in rows[:3]:
                self.logger.info(f"Harris: sample: {r}")

            for row in rows:
                if self._norm_date(row.get('file_date', '')) != target_file:
                    continue
                detail = self._fetch_detail(row.get('detail_url', ''))
                detail.update({'file_date': row['file_date'], 'sale_date': row.get('sale_date', '')})
                records.append(self.build_record(**detail))
                time.sleep(0.3)

        except Exception as exc:
            self.logger.error(f"Harris error: {exc}", exc_info=True)

        self.logger.info(f"Harris: {len(records)} records for {target_date}")
        return records

    # ── Results table ─────────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows  = []
        table = (
            soup.find('table', id=re.compile(r'grd|grid|result|foreclos|frcl', re.I))
            or soup.find('table', class_=re.compile(r'grd|grid|result|data', re.I))
        )
        if not table:
            for t in soup.find_all('table'):
                if re.search(r'\d{1,2}/\d{1,2}/\d{4}', t.get_text()) and t.find('a'):
                    table = t
                    break
        if not table:
            for t in soup.find_all('table'):
                trs = t.find_all('tr')
                if trs and len(trs[0].find_all(['th', 'td'])) >= 3:
                    table = t
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

    @staticmethod
    def _norm_date(d: str) -> str:
        m = re.match(r'^0?(\d{1,2})/0?(\d{1,2})/(\d{4})$', d.strip())
        return f"{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else d
