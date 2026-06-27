"""
Probe v3 — CONFIRMED: publicsearch serves doc page images at
  /files/documents/{docId}/images/{imageId}_{page}.png?exp=...&sig=...
Now: capture that PNG URL from network, download all pages, OCR them,
and parse owner+address to prove the pipeline works for publicsearch.
"""
import re, logging, os
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

SLUG = "bexar"
BASE = f"https://{SLUG}.tx.publicsearch.us"


def main():
    captured_imgs = []
    net = []
    start_fmt = (date(2026,6,27) - timedelta(days=30)).strftime('%m/%d/%Y')
    end_fmt   = date(2026,6,27).strftime('%m/%d/%Y')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)

        def on_resp(r):
            net.append((r.status, r.headers.get('content-type',''), r.url))
            # Capture document page images specifically
            if '/files/documents/' in r.url and '/images/' in r.url and '.png' in r.url:
                captured_imgs.append(r.url)
        page.on('response', on_resp)

        log.info("Navigating to search...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
        page.goto(BASE + '/search/advanced'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)

        for s, e in [('input[id*="start" i]','input[id*="end" i]')]:
            if page.locator(s).count() > 0:
                page.fill(s, start_fmt); page.fill(e, end_fmt); break
        for btn_sel in ['button[type="submit"]','button:has-text("Search")']:
            b = page.locator(btn_sel).first
            if b.count() > 0:
                b.click(); break
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(3500)

        cells = page.evaluate("""() => Array.from(document.querySelectorAll('td'))
            .map(t=>(t.textContent||'').trim()).filter(t=>/^\\d{6,}$/.test(t)).slice(0,3);""")
        log.info(f"Doc cells: {cells}")

        if not cells:
            log.info("No doc cells found"); browser.close(); return

        # Click first doc to open viewer
        captured_imgs.clear()
        el = page.locator(f'td:has-text("{cells[0]}")').first
        el.scroll_into_view_if_needed(); el.click()
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)  # let images load
        doc_url = page.url
        log.info(f"Doc page: {doc_url}")
        doc_id = doc_url.rstrip('/').split('/')[-1]

        log.info(f"Captured {len(captured_imgs)} document image URL(s)")
        for u in captured_imgs[:5]:
            log.info(f"  IMG: {u[:160]}")

        if not captured_imgs:
            log.info("No images captured — viewer may need a page interaction"); browser.close(); return

        # Download the captured page image(s) via the session request API (carries cookies + sig)
        os.makedirs('/tmp/ps', exist_ok=True)
        saved = []
        # de-dup by base image path (ignore repeated)
        seen = set()
        for u in captured_imgs:
            key = u.split('?')[0]
            if key in seen: continue
            seen.add(key)
            try:
                body = ctx.request.get(u).body()
                fn = f"/tmp/ps/{len(saved)}.png"
                with open(fn,'wb') as f: f.write(body)
                saved.append(fn)
                log.info(f"  saved {fn}: {len(body)} bytes, header={body[:8]}")
            except Exception as ex:
                log.info(f"  dl err: {str(ex)[:60]}")

        # OCR the images
        try:
            import pytesseract
            from PIL import Image
            log.info("=== OCR of document page(s) ===")
            full_text = ""
            for fn in saved[:3]:
                txt = pytesseract.image_to_string(Image.open(fn))
                full_text += txt + "\n"
                log.info(f"--- {fn}: {len(txt)} chars ---")
                for ln in txt.split('\n'):
                    if ln.strip(): log.info(f"  | {ln.strip()}")
        except Exception as ex:
            log.info(f"OCR err: {str(ex)[:100]}")

        browser.close()

if __name__ == '__main__':
    main()
