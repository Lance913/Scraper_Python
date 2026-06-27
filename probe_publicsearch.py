"""
Probe v6 — OCR an actual NTS document from publicsearch to see its layout.
We know these NTS doc numbers exist (from v5):
  Bexar: 20260072427 (ALMANZA JUAN DIEGO - individual!)
         20260072314 (LENNAR), 20260072451 (PURCHASING FUND)
  Denton: 2026-34546 (HORTON)
We search Bexar, click the ALMANZA doc, OCR all pages, print the format.
"""
import re, logging, os
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

BASE = "https://bexar.tx.publicsearch.us"
TARGET_DOC = "20260072427"  # ALMANZA JUAN DIEGO - the individual NTS


def main():
    captured = []
    start_fmt = (date(2026,6,27) - timedelta(days=120)).strftime('%m/%d/%Y')
    end_fmt   = date(2026,6,27).strftime('%m/%d/%Y')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: captured.append(r.url)
                if ('/files/documents/' in r.url and '/images/' in r.url and '.png' in r.url) else None)

        log.info("Search Bexar...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(BASE+'/search/advanced'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1200)
        for s,e in [('input[id*="start" i]','input[id*="end" i]')]:
            if page.locator(s).count()>0:
                page.fill(s,start_fmt); page.fill(e,end_fmt); break
        for bsel in ['button[type="submit"]','button:has-text("Search")']:
            b=page.locator(bsel).first
            if b.count()>0: b.click(); break
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)

        # Find and click the ALMANZA doc number
        log.info(f"Looking for doc {TARGET_DOC}...")
        el = page.locator(f'td:has-text("{TARGET_DOC}")').first
        if el.count() == 0:
            log.info(f"Doc {TARGET_DOC} not on first page; trying any NTS doc visible")
            # fall back: click any doc number cell
            cells = page.evaluate("""() => Array.from(document.querySelectorAll('td'))
                .map(t=>(t.textContent||'').trim()).filter(t=>/^\\d{6,}$/.test(t)).slice(0,1);""")
            if cells:
                el = page.locator(f'td:has-text("{cells[0]}")').first
                log.info(f"Using fallback doc {cells[0]}")
        if el.count() == 0:
            log.info("No doc to click"); browser.close(); return

        captured.clear()
        el.scroll_into_view_if_needed(); el.click()
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)
        log.info(f"Doc page: {page.url}")

        # The viewer may only load page 1 image initially. Try to trigger multi-page load
        # by scrolling the viewer / clicking "next in book"
        try:
            for _ in range(3):
                nxt = page.locator('button:has-text("Next in Book"), [aria-label*="next" i]').first
                if nxt.count()>0 and nxt.is_visible():
                    nxt.click(); page.wait_for_timeout(2000)
        except Exception: pass
        page.wait_for_timeout(2000)

        log.info(f"Captured {len(captured)} image(s)")
        os.makedirs('/tmp/ps', exist_ok=True)
        saved=[]; seen=set()
        for u in captured:
            k=u.split('?')[0]
            if k in seen: continue
            seen.add(k)
            try:
                body=ctx.request.get(u).body()
                fn=f"/tmp/ps/{len(saved)}.png"
                with open(fn,'wb') as f: f.write(body)
                saved.append(fn)
                log.info(f"  saved page: {len(body)} bytes")
            except Exception as e: log.info(f"dl err {str(e)[:50]}")

        import pytesseract
        from PIL import Image
        log.info("===== NTS DOCUMENT OCR (publicsearch/Bexar) =====")
        for fn in saved[:3]:
            txt = pytesseract.image_to_string(Image.open(fn))
            log.info(f"----- {fn} ({len(txt)} chars) -----")
            for ln in txt.split('\n'):
                if ln.strip(): log.info(f"  | {ln.strip()}")

        browser.close()

if __name__=='__main__':
    main()
