"""
Temp diagnostic: dump raw OCR text for real Harris docs that currently
extract as NONE (no name, no address) — so we can see why the regex-based
extractor in scrapers/harris_extract.py misses them, using real documents
instead of guessing. Reuses the exact production scrape/OCR code paths.
Deleted after use.
"""
import sys
from datetime import date

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from scrapers.base import launch_chromium
from scrapers.harris import (
    HarrisCountyScraper, SEARCH_URL, APP_BASE, YEAR_NAME, MONTH_NAME, SEARCH_NAME,
)
from scrapers.harris_extract import ocr_pdf_bytes, extract_from_text

# Target a month already known (from production logs) to have plenty of
# NONE-tier docs, so we don't burn runtime searching for them.
YEAR, MONTH = 2026, 10
MAX_SAMPLES = 6

scraper = HarrisCountyScraper()
target_date = date.today()

with sync_playwright() as pw:
    browser = launch_chromium(pw)
    ctx = browser.new_context(accept_downloads=True)
    page = ctx.new_page()
    page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page.set_default_timeout(30_000)

    page.goto(SEARCH_URL)
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1000)
    page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{YEAR_NAME}"]');
        if(s){{s.value='{YEAR}';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1000)
    page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{MONTH_NAME}"]');
        if(s){{s.value='{MONTH}';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
    page.wait_for_timeout(300)
    with page.expect_navigation(wait_until='networkidle', timeout=20_000):
        page.click(f'input[name="{SEARCH_NAME}"]')
    page.wait_for_timeout(3000)

    content = page.content()
    rows = scraper._parse_results_table(BeautifulSoup(content, 'lxml'))
    hrefs = page.evaluate("""() => Array.from(document.querySelectorAll('a'))
        .filter(x=>/FRCL/.test(x.textContent||''))
        .map(a=>({id:a.textContent.trim(), href:a.getAttribute('href')}));""")
    href_by_id = {h['id']: h['href'] for h in hrefs}
    print(f"Found {len(rows)} rows for {MONTH}/{YEAR}", file=sys.stderr)

    none_count = 0
    for r in rows:
        if none_count >= MAX_SAMPLES:
            break
        doc_id = r['doc_id']
        href = href_by_id.get(doc_id)
        if not href:
            continue
        try:
            body = ctx.request.get(APP_BASE + href).body()
            text = ocr_pdf_bytes(body, max_pages=3)
        except Exception as e:
            print(f"doc {doc_id}: fetch/OCR error: {e}", file=sys.stderr)
            continue
        ex = extract_from_text(text)
        tier = 'FULL' if (ex['first_name'] and ex['address']) else \
               ('PARTIAL' if (ex['first_name'] or ex['address']) else 'NONE')
        if tier != 'NONE':
            continue
        none_count += 1
        print(f"\n{'='*80}")
        print(f"DOC {doc_id}  (NONE — no name, no address extracted)")
        print(f"{'='*80}")
        print(text[:4000])

    browser.close()
    print(f"\nDumped {none_count} NONE-tier samples.", file=sys.stderr)
