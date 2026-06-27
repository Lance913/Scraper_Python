"""
Probe v6 (FINAL VALIDATION) — run the production extraction pipeline on 5 real
Harris docs, OCR all 3 pages each, show the extracted owner+address records.
This proves what % of records get full data vs partial vs doc_id-fallback.
"""
import os, sys, logging
sys.path.insert(0, 'scrapers')
from playwright.sync_api import sync_playwright
from harris_extract import extract_from_pdf_bytes

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PROBE] %(message)s')
log = logging.getLogger()

SEARCH_URL  = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
APP_BASE    = "https://www.cclerk.hctx.net/applications/websearch/"
YEAR_NAME   = 'ctl00$ContentPlaceHolder1$ddlYear'
MONTH_NAME  = 'ctl00$ContentPlaceHolder1$ddlMonth'
SEARCH_NAME = 'ctl00$ContentPlaceHolder1$btnSearch'


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        log.info("Loading portal...")
        page.goto(SEARCH_URL); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
        page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{YEAR_NAME}"]');
            if(s){{s.value='2026';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{MONTH_NAME}"]');
            if(s){{s.value='7';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
        page.wait_for_timeout(300)
        try:
            with page.expect_navigation(wait_until='networkidle', timeout=20000):
                page.click(f'input[name="{SEARCH_NAME}"]')
        except Exception as e:
            log.warning(f"nav: {e}")
        page.wait_for_timeout(2500)
        links = page.evaluate("""() => Array.from(document.querySelectorAll('a'))
            .filter(x=>/FRCL/.test(x.textContent||''))
            .map(a=>({id:a.textContent.trim(), href:a.getAttribute('href')}));""")
        log.info(f"Testing extraction on 5 of {len(links)} docs...")

        full=partial=none=0
        for lk in links[:5]:
            try:
                body = ctx.request.get(APP_BASE + lk['href']).body()
                rec = extract_from_pdf_bytes(body)
                name = f"{rec['first_name']} {rec['last_name']}".strip()
                addr = f"{rec['address']}, {rec['city']}, {rec['state']} {rec['zip_code']}".strip(' ,')
                has_name = bool(rec['first_name'])
                has_addr = bool(rec['address'])
                tier = "FULL" if (has_name and has_addr) else ("PARTIAL" if (has_name or has_addr) else "NONE→docid")
                if tier=="FULL": full+=1
                elif tier.startswith("PARTIAL"): partial+=1
                else: none+=1
                log.info(f"  [{lk['id']}] {tier}: name={name!r} addr={addr!r}")
            except Exception as e:
                log.info(f"  [{lk['id']}] ERROR: {str(e)[:80]}")
        log.info(f"=== RESULTS: {full} full, {partial} partial, {none} fallback (of 5) ===")
        browser.close()

if __name__ == '__main__':
    main()
