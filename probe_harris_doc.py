"""
Probe v3 — CONFIRMED: ViewECdocs.aspx streams a file download (not a page).
Now: capture the download, save it, identify the file type, report size.
This proves the OCR path is viable.
"""
import os, logging
from playwright.sync_api import sync_playwright

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

        href = page.evaluate("""() => {
            var a = Array.from(document.querySelectorAll('a')).filter(x=>/FRCL/.test(x.textContent||''))[0];
            return a ? a.getAttribute('href') : null;
        }""")
        abs_url = APP_BASE + href
        log.info(f"Doc URL: {abs_url[:85]}...")

        # === Capture the download by clicking the link ===
        log.info("=== Method 1: capture download via click ===")
        try:
            with page.expect_download(timeout=20000) as dl_info:
                # open in same tab to trigger download capture
                page.evaluate("""() => {
                    var a=Array.from(document.querySelectorAll('a')).filter(x=>/FRCL/.test(x.textContent||''))[0];
                    if(a){ a.removeAttribute('target'); a.click(); }
                }""")
            dl = dl_info.value
            path = "/tmp/harris_doc_dl"
            dl.save_as(path)
            size = os.path.getsize(path)
            log.info(f">>> DOWNLOAD CAPTURED: suggested_name={dl.suggested_filename}, size={size} bytes")
            with open(path, 'rb') as f:
                header = f.read(16)
            log.info(f">>> FILE HEADER (hex): {header.hex()}")
            log.info(f">>> FILE HEADER (ascii): {header[:8]}")
            # Identify type
            if header[:4] == b'%PDF':
                log.info(">>> FILE TYPE: PDF ✓ (OCR-ready)")
            elif header[:2] in (b'II', b'MM'):
                log.info(">>> FILE TYPE: TIFF ✓ (OCR-ready)")
            elif header[:3] == b'\xff\xd8\xff':
                log.info(">>> FILE TYPE: JPEG ✓ (OCR-ready)")
            elif header[:8] == b'\x89PNG\r\n\x1a\n':
                log.info(">>> FILE TYPE: PNG ✓ (OCR-ready)")
            else:
                log.info(f">>> FILE TYPE: UNKNOWN — first bytes {header}")
        except Exception as e:
            log.info(f"download via click failed: {str(e)[:120]}")

            # Method 2: use Playwright's request context (carries session cookies)
            log.info("=== Method 2: fetch via API request (same session cookies) ===")
            try:
                api_resp = ctx.request.get(abs_url)
                log.info(f"API status: {api_resp.status}, ct={api_resp.headers.get('content-type','')}, len={api_resp.headers.get('content-length','?')}")
                body = api_resp.body()
                log.info(f"API body size: {len(body)} bytes, header hex: {body[:16].hex()}, ascii: {body[:8]}")
                if body[:4]==b'%PDF': log.info(">>> PDF via API ✓")
                elif body[:2] in (b'II',b'MM'): log.info(">>> TIFF via API ✓")
            except Exception as e2:
                log.info(f"API fetch failed: {str(e2)[:120]}")

        browser.close()

if __name__ == '__main__':
    main()
