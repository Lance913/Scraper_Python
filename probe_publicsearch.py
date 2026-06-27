"""
Probe v10 — search the FORECLOSURES department and OCR a real Notice of Trustee Sale.

v9 introspection revealed the advanced-search form has a Department selector
(button id='department', listbox id='department-listbox') whose options include
"Foreclosures". The current scraper never sets it, so it searches the default
"Land Records" department (department=RP) and only catches NTS docs by luck in a
capped ~50-row sample.

v10:
  1. Open Department -> select "Foreclosures".
  2. Fill the recorded date range, search.
  3. Log the results URL (capture the dept query param), total rows, doc types,
     and pagination behavior.
  4. Use the real scraper's _parse_nts_rows() to pick an NTS row (prefer an
     individual/residential lead), click it, capture the PNG page images, OCR
     them, and print the REAL Notice of Trustee Sale layout.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright
from scrapers.publicsearch import PublicSearchScraper, is_residential_lead

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTY_SLUG = "bexar"
COUNTY_NAME = "Bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 120
MAX_PAGES = 6


def is_doc_image(url):
    return ('/files/documents/' in url and '/images/' in url and '.png' in url)


def select_foreclosures_department(page):
    """Open the Department combobox and choose 'Foreclosures'. Returns True/False."""
    try:
        btn = page.locator('button#department, #department').first
        if btn.count() == 0:
            log.info("department button not found")
            return False
        btn.click()
        page.wait_for_timeout(800)
        for sel in [
            '#department-listbox [role="option"]:has-text("Foreclosures")',
            '#department-listbox li:has-text("Foreclosures")',
            '#department-listbox >> text="Foreclosures"',
            '[role="option"]:has-text("Foreclosures")',
            'li:has-text("Foreclosures")',
        ]:
            opt = page.locator(sel).first
            if opt.count() > 0 and opt.is_visible():
                opt.click()
                page.wait_for_timeout(600)
                cur = page.locator('button#department, #department').first.inner_text()
                log.info(f"department now: {cur!r}")
                return 'foreclos' in cur.lower()
        log.info("Foreclosures option not found in listbox")
    except Exception as e:
        log.info(f"dept select err: {str(e)[:100]}")
    return False


def pick_nts_row(scraper, page):
    rows = [r for r in scraper._parse_nts_rows(page.content()) if r.get('doc_number')]
    if not rows:
        return None
    residential = [r for r in rows if is_residential_lead(r.get('grantor', ''))]
    chosen = (residential or rows)[0]
    log.info(
        f"Chosen NTS row: grantor={chosen.get('grantor')!r} doc={chosen.get('doc_number')!r} "
        f"({'individual' if residential else 'entity only on this page'})"
    )
    return chosen


def main():
    captured = []
    scraper = PublicSearchScraper(COUNTY_SLUG, COUNTY_NAME)
    start_fmt = (TODAY - timedelta(days=WINDOW_DAYS)).strftime('%m/%d/%Y')
    end_fmt = TODAY.strftime('%m/%d/%Y')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=['--disable-blink-features=AutomationControlled']
        )
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: captured.append(r.url) if is_doc_image(r.url) else None)

        log.info(f"Loading advanced search ({COUNTY_NAME}) ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(BASE + '/search/advanced')
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(1500)

        ok = select_foreclosures_department(page)
        log.info(f"Foreclosures department selected: {ok}")

        if page.locator('#recordedDateRange-start').count() > 0:
            page.fill('#recordedDateRange-start', start_fmt)
            page.fill('#recordedDateRange-end', end_fmt)
            log.info(f"date range {start_fmt}->{end_fmt}")

        for bsel in ['button[type="submit"]', 'button:has-text("Search")']:
            b = page.locator(bsel).first
            if b.count() > 0:
                b.click(); break
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)

        log.info(f"results URL: {page.url}")
        log.info(f"total table rows page 1: {page.locator('table tr').count()}")

        # Dump the Foreclosures results table structure: headers + first rows.
        tables = page.evaluate("""() => Array.from(document.querySelectorAll('table')).map(t => ({
            headers: Array.from(t.querySelectorAll('th')).map(h => (h.textContent||'').trim()),
            rows: Array.from(t.querySelectorAll('tr')).slice(1, 4).map(
                tr => Array.from(tr.querySelectorAll('td')).map(td => (td.textContent||'').trim().slice(0, 45))
            ),
        }))""")
        log.info("===== RESULTS TABLE STRUCTURE =====")
        for ti, t in enumerate(tables):
            log.info(f"table[{ti}] headers: {t['headers']}")
            for ri, r in enumerate(t['rows']):
                log.info(f"  row[{ri}]: {r}")

        chosen = None
        for page_num in range(1, MAX_PAGES + 1):
            log.info(f"--- results page {page_num} ---")
            chosen = pick_nts_row(scraper, page)
            if chosen:
                break
            if not scraper._next_page(page):
                log.info("No more results pages.")
                break

        if not chosen:
            log.info("No NTS row found to OCR."); browser.close(); return

        doc_num = chosen['doc_number']
        log.info(f"Clicking NTS doc cell {doc_num!r} ...")
        captured.clear()
        clicked = False
        for sel in [f'td:text-is("{doc_num}")', f'td:has-text("{doc_num}")']:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.scroll_into_view_if_needed(); el.click(); clicked = True; break
        if not clicked:
            log.info(f"Could not click doc cell {doc_num!r}"); browser.close(); return

        page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)
        log.info(f"Doc page: {page.url}")
        try:
            for _ in range(4):
                nxt = page.locator('button:has-text("Next in Book"), [aria-label*="next" i]').first
                if nxt.count() > 0 and nxt.is_visible():
                    nxt.click(); page.wait_for_timeout(2000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        log.info(f"Captured {len(captured)} image URL(s)")
        os.makedirs('/tmp/ps', exist_ok=True)
        saved, seen = [], set()
        for u in captured:
            k = u.split('?')[0]
            if k in seen:
                continue
            seen.add(k)
            try:
                body = ctx.request.get(u).body()
                fn = f"/tmp/ps/{len(saved)}.png"
                with open(fn, 'wb') as f:
                    f.write(body)
                saved.append(fn)
                log.info(f"  saved page {len(saved)}: {len(body)} bytes hdr={body[:4]!r}")
            except Exception as e:
                log.info(f"  dl err {str(e)[:60]}")

        import pytesseract
        from PIL import Image
        log.info(f"===== REAL NTS OCR ({COUNTY_NAME}, grantor={chosen.get('grantor')!r}) =====")
        for fn in saved[:3]:
            txt = pytesseract.image_to_string(Image.open(fn))
            log.info(f"----- {fn} ({len(txt)} chars) -----")
            for ln in txt.split('\n'):
                if ln.strip():
                    log.info(f"  | {ln.strip()}")

        browser.close()


if __name__ == '__main__':
    main()
