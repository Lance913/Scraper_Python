"""
Probe v4 — find an ACTUAL Notice of Trustee Sale doc on publicsearch (not a
Deed of Trust), OCR all its pages, and print the text so we can see the real
NTS layout and build the parser. Searches by document-type keyword to surface NTS.
"""
import re, logging, os
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

SLUG = "bexar"
BASE = f"https://{SLUG}.tx.publicsearch.us"


def main():
    start_fmt = (date(2026,6,27) - timedelta(days=120)).strftime('%m/%d/%Y')  # wide range to find NTS
    end_fmt   = date(2026,6,27).strftime('%m/%d/%Y')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)

        captured = []
        page.on('response', lambda r: captured.append(r.url)
                if ('/files/documents/' in r.url and '/images/' in r.url and '.png' in r.url) else None)

        log.info("Searching for NTS documents...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
        page.goto(BASE + '/search/advanced'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1500)

        # Try to fill a doc-type / keyword field to find Notice of Trustee Sale
        # publicsearch advanced search usually has a "Document Type" or free-text field
        typed = False
        for sel in ['input[id*="docType" i]','input[placeholder*="Type" i]',
                    'input[id*="searchText" i]','input[placeholder*="eyword" i]',
                    'input[type="text"]']:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.fill('NOTICE OF TRUSTEE')
                    log.info(f"typed doc-type into {sel}")
                    typed = True
                    page.wait_for_timeout(1000)
                    # Try selecting an autocomplete option if present
                    try:
                        opt = page.locator('[role="option"], li:has-text("TRUSTEE")').first
                        if opt.count() > 0 and opt.is_visible():
                            opt.click(); log.info("selected autocomplete option")
                    except Exception: pass
                    break
            except Exception: pass

        # Fill date range too
        for s, e in [('input[id*="start" i]','input[id*="end" i]')]:
            try:
                if page.locator(s).count() > 0:
                    page.fill(s, start_fmt); page.fill(e, end_fmt)
                    log.info(f"date {start_fmt}->{end_fmt}"); break
            except Exception: pass

        for btn_sel in ['button[type="submit"]','button:has-text("Search")']:
            try:
                b = page.locator(btn_sel).first
                if b.count() > 0: b.click(); break
            except Exception: pass
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)
        log.info(f"Results URL: {page.url}")

        # Look at doc-type cells to find an NTS row
        rows_info = page.evaluate("""() => {
            var trs = Array.from(document.querySelectorAll('tr'));
            var out = [];
            for (var tr of trs) {
                var txt = (tr.textContent||'');
                if (/NOTICE|TRUSTEE/i.test(txt)) {
                    var nums = (txt.match(/\\d{6,}/g)||[]);
                    out.push({text: txt.slice(0,120), docnum: nums[0]||''});
                }
            }
            return out.slice(0,8);
        }""")
        log.info(f"Found {len(rows_info)} rows mentioning NOTICE/TRUSTEE:")
        for r in rows_info:
            log.info(f"  doc={r['docnum']} | {r['text']}")

        # Click the first NTS row's doc number
        target = next((r['docnum'] for r in rows_info if r['docnum']), None)
        if not target:
            log.info("No NTS doc found in results; printing all doc-type cells for reference")
            dts = page.evaluate("""() => Array.from(document.querySelectorAll('td'))
                .map(t=>(t.textContent||'').trim()).filter(t=>/[A-Z]{3,}/.test(t)).slice(0,20);""")
            log.info(f"Doc types visible: {dts}")
            browser.close(); return

        log.info(f"Opening NTS doc {target}...")
        captured.clear()
        el = page.locator(f'td:has-text("{target}")').first
        el.scroll_into_view_if_needed(); el.click()
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)
        log.info(f"Doc page: {page.url}")

        log.info(f"Captured {len(captured)} image(s)")
        if not captured:
            browser.close(); return

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
            except Exception as e: log.info(f"dl err {str(e)[:50]}")
        log.info(f"saved {len(saved)} page image(s)")

        import pytesseract
        from PIL import Image
        log.info("===== NTS DOCUMENT OCR =====")
        for fn in saved[:2]:
            txt = pytesseract.image_to_string(Image.open(fn))
            log.info(f"----- {fn} ({len(txt)} chars) -----")
            for ln in txt.split('\n'):
                if ln.strip(): log.info(f"  | {ln.strip()}")

        browser.close()

if __name__ == '__main__':
    main()
