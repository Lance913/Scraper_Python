"""
Probe v5 — PDFs are scanned images. Run OCR on the first 2 sample docs,
page 1 (where grantor + property address live on a Notice of Trustee Sale),
and PRINT the OCR text so we can see the exact format and build the parser.
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
OUT_DIR = "probe_artifacts"


def get_pdfs(n=2):
    os.makedirs(OUT_DIR, exist_ok=True)
    saved = []
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
        for lk in links[:n]:
            try:
                body = ctx.request.get(APP_BASE + lk['href']).body()
                fn = os.path.join(OUT_DIR, f"{lk['id']}.pdf")
                with open(fn,'wb') as f: f.write(body)
                saved.append(fn); log.info(f"Saved {lk['id']}.pdf ({len(body)}b)")
            except Exception as e:
                log.info(f"err {lk['id']}: {str(e)[:60]}")
        browser.close()
    return saved


def ocr_page1(pdf_path):
    from pdf2image import convert_from_path
    import pytesseract
    log.info(f"=== OCR {os.path.basename(pdf_path)} page 1 ===")
    try:
        # Only page 1, decent DPI for accuracy
        imgs = convert_from_path(pdf_path, dpi=200, first_page=1, last_page=1)
        if not imgs:
            log.info("  no image rendered"); return
        text = pytesseract.image_to_string(imgs[0])
        log.info(f"  OCR got {len(text)} chars")
        # Print the full page 1 text, line by line, so we see the format
        log.info("  ===== BEGIN OCR TEXT =====")
        for line in text.split('\n'):
            if line.strip():
                log.info(f"  | {line.strip()}")
        log.info("  ===== END OCR TEXT =====")
    except Exception as e:
        log.info(f"  OCR err: {str(e)[:120]}")


def main():
    pdfs = get_pdfs(2)
    for p in pdfs:
        ocr_page1(p)
    log.info("=== DONE ===")

if __name__ == '__main__':
    main()
