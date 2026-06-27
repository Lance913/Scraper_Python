"""
Probe v4 — PDF confirmed. Now: download it, save to repo artifact dir,
and test BOTH text extraction (pdfplumber/pypdf) AND report page count.
This tells us: embedded text layer (fast) vs needs-OCR (slow).
Saves the PDF so we can pull it as an artifact and inspect locally.
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


def download_sample_pdfs(n=3):
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

        # Get the first N FRCL links' relative hrefs + doc ids
        links = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a'))
                .filter(x=>/FRCL/.test(x.textContent||''))
                .map(a=>({id:a.textContent.trim(), href:a.getAttribute('href')}));
        }""")
        log.info(f"Found {len(links)} doc links")

        for i, lk in enumerate(links[:n]):
            url = APP_BASE + lk['href']
            try:
                resp = ctx.request.get(url)
                body = resp.body()
                fn = os.path.join(OUT_DIR, f"{lk['id']}.pdf")
                with open(fn, 'wb') as f:
                    f.write(body)
                log.info(f"Saved {lk['id']}.pdf ({len(body)} bytes), header={body[:8]}")
                saved.append(fn)
            except Exception as e:
                log.info(f"  {lk['id']} fetch err: {str(e)[:80]}")

        browser.close()
    return saved


def test_text_extraction(pdf_path):
    log.info(f"--- Testing extraction on {os.path.basename(pdf_path)} ---")
    # Try pypdf first
    try:
        from pypdf import PdfReader
        r = PdfReader(pdf_path)
        npages = len(r.pages)
        text = ""
        for pg in r.pages:
            text += pg.extract_text() or ""
        log.info(f"  pypdf: {npages} pages, {len(text)} chars of embedded text")
        if len(text) > 50:
            log.info(f"  EMBEDDED TEXT FOUND (fast path!): {text[:300].strip()}")
            return 'text', text
        else:
            log.info(f"  No meaningful embedded text → needs OCR")
    except Exception as e:
        log.info(f"  pypdf err: {str(e)[:100]}")
    return 'ocr', None


def main():
    saved = download_sample_pdfs(3)
    log.info(f"=== Downloaded {len(saved)} PDFs, testing extraction ===")
    for p in saved:
        mode, text = test_text_extraction(p)
    log.info("=== DONE — check artifacts for the PDFs ===")

if __name__ == '__main__':
    main()
