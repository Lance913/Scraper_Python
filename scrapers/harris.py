"""
Harris County Foreclosure Scraper
Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx

The portal uses Telerik RadComboBox dropdowns.
select_option() sets the hidden native <select> but does NOT fire
Telerik's event system — so the ASP.NET postback never triggers.

Fix: use Telerik's JavaScript client API:
  $find('controlClientID').findItemByText('June').select()
This selects the item through Telerik's own system, which fires
the postback and loads the results table.
"""

import re
import time
from datetime import date
from typing import List, Dict
from bs4 import BeautifulSoup
from .base import BaseScraper

SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
BASE_URL   = "https://www.cclerk.hctx.net"

# Telerik RadComboBox client IDs (confirmed from page source)
YEAR_CLIENT_ID  = 'ctl00_ContentPlaceHolder1_ddlYear'
MONTH_CLIENT_ID = 'ctl00_ContentPlaceHolder1_ddlMonth'

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
        # Harris table may show '06/26/2026' or '6/26/2026' — normalize by stripping leading zeros
        def _norm_date(d: str) -> str:
            m = re.match(r'^0?(\d{1,2})/0?(\d{1,2})/(\d{4})$', d.strip())
            return f'{m.group(1)}/{m.group(2)}/{m.group(3)}' if m else d
        target_file = _norm_date(f'{target_date.month}/{target_date.day}/{target_date.year}')

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page    = browser.new_page()
                page.set_default_timeout(30_000)

                self.logger.info("Harris: loading portal...")
                page.goto(SEARCH_URL)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                # ── Select year via Telerik JavaScript API ─────────────────
                year_result = page.evaluate(f"""
                    () => {{
                        try {{
                            // Telerik's $find() API
                            var rcb = $find('{YEAR_CLIENT_ID}');
                            if (rcb) {{
                                var item = rcb.findItemByText('{year_str}');
                                if (item) {{
                                    item.select();
                                    return 'Telerik API: selected ' + item.get_text();
                                }}
                                return 'Telerik item not found for: {year_str}';
                            }}
                        }} catch(e) {{
                            return 'Telerik error: ' + e.message;
                        }}

                        // Fallback: click via TreeWalker text node
                        var walker = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT
                        );
                        var node;
                        while (node = walker.nextNode()) {{
                            if (node.textContent.trim() === '{year_str}') {{
                                node.parentElement.click();
                                return 'TreeWalker click: ' + node.parentElement.tagName;
                            }}
                        }}
                        return 'not found';
                    }}
                """)
                self.logger.info(f"Harris: year → {year_result}")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(2000)

                # ── Select month via Telerik JavaScript API ────────────────
                month_result = page.evaluate(f"""
                    () => {{
                        try {{
                            var rcb = $find('{MONTH_CLIENT_ID}');
                            if (rcb) {{
                                var item = rcb.findItemByText('{month_str}');
                                if (item) {{
                                    item.select();
                                    return 'Telerik API: selected ' + item.get_text();
                                }}
                                return 'Telerik item not found for: {month_str}';
                            }}
                        }} catch(e) {{
                            return 'Telerik error: ' + e.message;
                        }}

                        // Fallback: TreeWalker click
                        var walker = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT
                        );
                        var node;
                        while (node = walker.nextNode()) {{
                            if (node.textContent.trim() === '{month_str}') {{
                                node.parentElement.click();
                                return 'TreeWalker click: ' + node.parentElement.tagName;
                            }}
                        }}
                        return 'not found';
                    }}
                """)
                self.logger.info(f"Harris: month → {month_result}")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(3000)

                body = page.inner_text('body')
                self.logger.info(f"Harris body after selections: {body[:800]}")

                content = page.content()
                browser.close()

            soup = BeautifulSoup(content, 'lxml')
            rows = self._parse_results_table(soup)
            self.logger.info(f"Harris: {len(rows)} rows for {month_str} {year_str}")

            for row in rows:
                if _norm_date(row.get('file_date', '')) != target_file:
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
