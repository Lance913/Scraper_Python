"""
Probe v7 — OCR a REAL NTS document from publicsearch to learn its layout.

The point of this probe: see the exact text layout of a publicsearch Notice of
Trustee Sale so we can write publicsearch_extract.py (sale date + owner + address).

v6's bug: it looked for one hard-coded doc number and, if that doc wasn't on
results page 1, fell back to clicking the FIRST doc-number cell on the page —
which grabbed an unrelated UCC filing, not an NTS.

v7 fix: reuse the scraper's own NTS detection. We instantiate the real
PublicSearchScraper, run its _parse_nts_rows() on each results page (so the probe
clicks EXACTLY the rows the scraper considers NTS), paginate until we find one,
prefer an individual/residential lead, click that doc's cell, capture the PNG
page images, OCR them, and print the layout.
"""
import logging, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright
from scrapers.publicsearch import PublicSearchScraper, is_residential_lead

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTY_SLUG = "bexar"
COUNTY_NAME = "Bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)        # repo runs on GH Actions; pin a date for reproducibility
WINDOW_DAYS = 120                # wide window so we catch some NTS volume
MAX_PAGES = 6


def is_doc_image(url: str) -> bool:
    return ('/files/documents/' in url and '/images/' in url and '.png' in url)


def pick_nts_row(scraper, page):
    """Run the scraper's own NTS parser on the current results page.

    Returns the NTS row to click — preferring an individual/residential lead
    (is_residential_lead True) over a builder/HOA/fund, since the whole point is
    to see an individual homeowner NTS layout. Returns None if no NTS rows here.
    """
    rows = scraper._parse_nts_rows(page.content())
    rows = [r for r in rows if r.get('doc_number')]
    if not rows:
        return None
    residential = [r for r in rows if is_residential_lead(r.get('grantor', ''))]
    chosen = (residential or rows)[0]
    log.info(
        f"Chosen NTS row: grantor={chosen.get('grantor')!r} "
        f"doc={chosen.get('doc_number')!r} "
        f"({'individual' if residential else 'entity (no individual on this page)'})"
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
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page.set_default_timeout(30000)
        page.on('response', lambda r: captured.append(r.url) if is_doc_image(r.url) else None)

        log.info(f"Search {COUNTY_NAME} {start_fmt}->{end_fmt} ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(BASE + '/search/advanced')
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(1200)
        for s, e in [('input[id*="start" i]', 'input[id*="end" i]')]:
            if page.locator(s).count() > 0:
                page.fill(s, start_fmt); page.fill(e, end_fmt); break
        for bsel in ['button[type="submit"]', 'button:has-text("Search")']:
            b = page.locator(bsel).first
            if b.count() > 0:
                b.click(); break
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)

        # Paginate until we find an NTS row the scraper recognizes.
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
        for sel in [
            f'td:text-is("{doc_num}")',
            f'td:has-text("{doc_num}")',
            f'[class*="docNumber" i]:has-text("{doc_num}")',
        ]:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.scroll_into_view_if_needed(); el.click(); clicked = True; break
        if not clicked:
            log.info(f"Could not click doc cell {doc_num!r}"); browser.close(); return

        page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)
        log.info(f"Doc page: {page.url}")

        # The viewer may only load page-1 image initially; nudge it to load the rest.
        try:
            for _ in range(3):
                nxt = page.locator(
                    'button:has-text("Next in Book"), [aria-label*="next" i]'
                ).first
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
                log.info(f"  saved page {len(saved)}: {len(body)} bytes  hdr={body[:4]!r}")
            except Exception as e:
                log.info(f"  dl err {str(e)[:60]}")

        import pytesseract
        from PIL import Image
        log.info(f"===== NTS DOCUMENT OCR ({COUNTY_NAME}, grantor={chosen.get('grantor')!r}) =====")
        for fn in saved[:3]:
            txt = pytesseract.image_to_string(Image.open(fn))
            log.info(f"----- {fn} ({len(txt)} chars) -----")
            for ln in txt.split('\n'):
                if ln.strip():
                    log.info(f"  | {ln.strip()}")

        browser.close()


if __name__ == '__main__':
    main()
