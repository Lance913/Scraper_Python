"""
Harris County Foreclosure Scraper — with OCR document extraction.

Portal: https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx
Documents: ViewECdocs.aspx streams a scanned PDF (no text layer). We download
each doc via the in-session request API, OCR all 3 pages, and parse owner name
+ property address. Falls back to doc_id when a scan is too degraded to read.

Month strategy: scrape ALL upcoming auction months the portal offers, starting
from the current month forward (Harris posts auctions for the first Tuesday of
each month; future months become available as filings come in). We probe each
month from current through current+5 and keep any that return rows with a
sale_date >= today.
"""

import re
from datetime import date, datetime
from typing import List, Dict
from bs4 import BeautifulSoup
from .base import BaseScraper
from .harris_extract import extract_from_pdf_bytes

SEARCH_URL  = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
APP_BASE    = "https://www.cclerk.hctx.net/applications/websearch/"
YEAR_NAME   = 'ctl00$ContentPlaceHolder1$ddlYear'
MONTH_NAME  = 'ctl00$ContentPlaceHolder1$ddlMonth'
SEARCH_NAME = 'ctl00$ContentPlaceHolder1$btnSearch'

MONTH_LABELS = {
    1:'January',2:'February',3:'March',4:'April',5:'May',6:'June',
    7:'July',8:'August',9:'September',10:'October',11:'November',12:'December',
}

# How many months ahead to look for upcoming auctions (current + N).
MONTHS_AHEAD = 4


class HarrisCountyScraper(BaseScraper):

    def __init__(self):
        super().__init__('Harris')

    def scrape(self, target_date: date) -> List[Dict]:
        self.logger.info(f"Scraping Harris County for {target_date}")

        # Build the list of (year, month) from current month through current+MONTHS_AHEAD.
        months = []
        for offset in range(0, MONTHS_AHEAD + 1):
            m = target_date.month + offset
            y = target_date.year
            while m > 12:
                m -= 12
                y += 1
            months.append((y, m))

        all_records = []
        for year, month in months:
            recs = self._scrape_month(year, month, target_date)
            all_records.extend(recs)

        self.logger.info(f"Harris: {len(all_records)} total upcoming records")
        return all_records

    def _scrape_month(self, year: int, month: int, target_date: date) -> List[Dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.error("Playwright not installed")
            return []

        year_val  = str(year)
        month_val = str(month)
        month_str = MONTH_LABELS[month]
        records   = []

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled'])
                ctx = browser.new_context(accept_downloads=True)
                page = ctx.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page.set_default_timeout(30_000)

                self.logger.info(f"Harris: loading portal for {month_str} {year_val}...")
                page.goto(SEARCH_URL)
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)

                page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{YEAR_NAME}"]');
                    if(s){{s.value='{year_val}';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1000)
                page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{MONTH_NAME}"]');
                    if(s){{s.value='{month_val}';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
                page.wait_for_timeout(300)

                try:
                    with page.expect_navigation(wait_until='networkidle', timeout=20_000):
                        page.click(f'input[name="{SEARCH_NAME}"]')
                    self.logger.info(f"Harris: Search clicked for {month_str} {year_val}")
                except Exception as e:
                    self.logger.warning(f"Harris: search click: {e}")
                page.wait_for_timeout(3000)

                content = page.content()
                rows = self._parse_results_table(BeautifulSoup(content, 'lxml'))

                # Collect this month's relative doc hrefs in the SAME order as rows.
                hrefs = page.evaluate("""() => Array.from(document.querySelectorAll('a'))
                    .filter(x=>/FRCL/.test(x.textContent||''))
                    .map(a=>({id:a.textContent.trim(), href:a.getAttribute('href')}));""")
                href_by_id = {h['id']: h['href'] for h in hrefs}

                # Filter to UPCOMING auctions only (sale_date >= today).
                upcoming = []
                for r in rows:
                    sd = self._parse_date(r.get('sale_date',''))
                    if sd and sd >= target_date:
                        upcoming.append(r)
                self.logger.info(
                    f"Harris: {len(rows)} rows for {month_str} {year_val}, "
                    f"{len(upcoming)} upcoming")

                if not upcoming:
                    browser.close()
                    return records

                # OCR each upcoming doc.
                full = part = none = 0
                for r in upcoming:
                    doc_id = r['doc_id']
                    href = href_by_id.get(doc_id)
                    rec_fields = {
                        'first_name':'','last_name':'','address':'',
                        'city':'','state':'TX','zip_code':'',
                    }
                    if href:
                        try:
                            body = ctx.request.get(APP_BASE + href).body()
                            ex = extract_from_pdf_bytes(body)
                            rec_fields.update({
                                'first_name': ex['first_name'],
                                'last_name':  ex['last_name'],
                                'address':    ex['address'],
                                'city':       ex['city'],
                                'state':      ex['state'] or 'TX',
                                'zip_code':   ex['zip_code'],
                            })
                            if ex['first_name'] and ex['address']: full += 1
                            elif ex['first_name'] or ex['address']: part += 1
                            else: none += 1
                        except Exception as e:
                            self.logger.warning(f"Harris: OCR {doc_id}: {str(e)[:80]}")
                            none += 1

                    records.append(self.build_record(
                        first_name=rec_fields['first_name'],
                        last_name= rec_fields['last_name'],
                        address=   rec_fields['address'],
                        city=      rec_fields['city'],
                        state=     rec_fields['state'],
                        zip_code=  rec_fields['zip_code'],
                        file_date= r.get('file_date',''),
                        sale_date= r.get('sale_date',''),
                        doc_id=    doc_id,
                    ))

                self.logger.info(
                    f"Harris {month_str}: {full} full, {part} partial, {none} docid-only")
                browser.close()

        except Exception as exc:
            self.logger.error(f"Harris {month_str} {year_val}: {exc}", exc_info=True)

        return records

    def _parse_results_table(self, soup: BeautifulSoup) -> List[Dict]:
        rows = []
        target = None
        for t in soup.find_all('table'):
            if 'FRCL' in t.get_text() and re.search(r'\d{2}/\d{2}/\d{4}', t.get_text()):
                target = t
                break
        if not target:
            return rows
        for tr in target.find_all('tr'):
            tds = tr.find_all('td')
            if len(tds) < 4:
                continue
            doc_id    = tds[1].get_text(strip=True)
            sale_date = tds[2].get_text(strip=True)
            file_date = tds[3].get_text(strip=True)
            if not doc_id.startswith('FRCL'):
                continue
            if not re.match(r'\d{1,2}/\d{1,2}/\d{4}', sale_date):
                continue
            rows.append({'doc_id':doc_id,'sale_date':sale_date,'file_date':file_date})
        return rows

    @staticmethod
    def _parse_date(s: str):
        s = s.strip()
        for fmt in ('%m/%d/%Y', '%m/%d/%y'):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None
