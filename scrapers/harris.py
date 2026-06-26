"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

The portal is JavaScript-rendered — HTTP POST returns an HTML shell with
no results table. Playwright must execute the JavaScript.

Telerik RadComboBox fix: call __doPostBack() directly instead of
fighting Telerik's UI. __doPostBack is ASP.NET WebForms' native form
submission function. It sets __EVENTTARGET and calls form.submit().
This bypasses Telerik's client-side event system entirely.

Flow:
  1. Playwright loads page
  2. JS: read option values from month <select>
  3. JS: sel.value = year → __doPostBack('...ddlYear', '') → page reloads
  4. JS: sel.value = month → __doPostBack('...ddlMonth', '') → results load
  5. Parse results table (Doc ID / Sale Date / File Date)
  6. Filter to target_date, fetch detail for name/address
"""

import re
import time
from datetime import date
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from .base import BaseScraper

SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
BASE_URL   = "https://www.cclerk.hctx.net"

YEAR_NAME  = 'ctl00$ContentPlaceHolder1$ddlYear'
MONTH_NAME = 'ctl00$ContentPlaceHolder1$ddlMonth'

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
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error("Playwright not installed")
            return []

        records    = []
        year_str   = str(target_date.year)
        month_str  = MONTH_LABELS[target_date.month]
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

                # ── Log what option values exist for month <select> ────────
                month_opts = page.evaluate(f"""
                    () => {{
                        var sel = document.querySelector('select[name="{MONTH_NAME}"]');
                        if (!sel) return 'month select not found';
                        return Array.from(sel.options).map(o => o.value).join(',');
                    }}
                """)
                self.logger.info(f"Harris: month option values: {month_opts}")

                # ── Select year via __doPostBack ───────────────────────────
                year_res = page.evaluate(f"""
                    () => {{
                        var sel = document.querySelector('select[name="{YEAR_NAME}"]');
                        if (!sel) return 'year select not found';
                        sel.value = '{year_str}';
                        try {{
                            __doPostBack('{YEAR_NAME}', '');
                            return 'doPostBack: year=' + sel.value;
                        }} catch(e) {{
                            return 'doPostBack error: ' + e.message;
                        }}
                    }}
                """)
                self.logger.info(f"Harris: year → {year_res}")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(2000)

                # ── Select month via __doPostBack ──────────────────────────
                month_res = page.evaluate(f"""
                    () => {{
                        var sel = document.querySelector('select[name="{MONTH_NAME}"]');
                        if (!sel) return 'month select not found';
                        // Try exact match first, then startsWith
                        var opts = Array.from(sel.options);
                        var match = opts.find(o => o.value === '{month_str}' || o.text === '{month_str}');
                        if (!match) match = opts.find(o => o.value.startsWith('{month_str[:3]}') || o.text.startsWith('{month_str[:3]}'));
                        if (match) {{
                            sel.value = match.value;
                        }} else {{
                            sel.value = '{month_str}';
                        }}
                        try {{
                            __doPostBack('{MONTH_NAME}', '');
                            return 'doPostBack: month=' + sel.value;
                        }} catch(e) {{
                            return 'doPostBack error: ' + e.message;
                        }}
                    }}
                """)
                self.logger.info(f"Harris: month → {month_res}")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(3000)

                body = page.inner_text('body')
                self.logger.info(f"Harris body: {body[:600]}")

                content = page.content()
                browser.close()

            # ── Parse results ──────────────────────────────────────────────
            soup = BeautifulSoup(content, 'lxml')
            rows = self._parse_results_table(soup)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_str}")
            for r in rows[:3]:
                self.logger.info(f"Harris: sample: {r}")

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

    # ── Results table ─────────────────────────────────────────────────────────

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows  = []
        # Try named table first
        table = (
            soup.find('table', id=re.compile(r'grd|grid|result|foreclos|frcl', re.I))
            or soup.find('table', class_=re.compile(r'grd|grid|result|data', re.I))
        )
        # Fallback: table with date + link
        if not table:
            for t in soup.find_all('table'):
                if re.search(r'\d{1,2}/\d{1,2}/\d{4}', t.get_text()) and t.find('a'):
                    table = t
                    break
        # Last resort: any table with 3+ columns
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
