"""
Temp diagnostic: re-run the real Harris scrape/OCR path for one month and
report full/partial/none tallies, to compare against production's own
logged numbers for the same month before this fix
(e.g. "Harris October: 0 full, 14 partial, 24 docid-only" from 2026-09-15).
Read-only against the county portal, no Sheets writes. Deleted after use.
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

YEAR, MONTH = 2026, 10

scraper = HarrisCountyScraper()

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
    print(f"Found {len(rows)} rows for {MONTH}/{YEAR}")

    full = part = none = 0
    recovered = []
    for r in rows:
        doc_id = r['doc_id']
        href = href_by_id.get(doc_id)
        if not href:
            none += 1
            continue
        try:
            body = ctx.request.get(APP_BASE + href).body()
            text = ocr_pdf_bytes(body, max_pages=3)
        except Exception as e:
            print(f"doc {doc_id}: fetch/OCR error: {e}", file=sys.stderr)
            none += 1
            continue
        ex = extract_from_text(text)
        if ex['first_name'] and ex['address']:
            full += 1
        elif ex['first_name'] or ex['address']:
            part += 1
            if ex['address']:
                recovered.append((doc_id, ex['address'], ex['city'], ex['zip_code']))
        else:
            none += 1

    browser.close()
    print(f"\nAFTER FIX -- {MONTH}/{YEAR}: {full} full, {part} partial, {none} docid-only "
          f"(of {len(rows)} total)")
    print(f"\nAddresses recovered by the new fallback (partial-tier, address present):")
    for doc_id, addr, city, zipc in recovered:
        print(f"  {doc_id}: {addr!r}, {city!r}, TX {zipc!r}")
