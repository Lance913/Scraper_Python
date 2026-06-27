"""
Probe v18 — dump OCR text of a few DENTON foreclosure docs so we can write a
property-address parser. Denton's results table leaves Property Address blank,
so the address must come from the document body (as the name already does).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

SLUG = "denton"
BASE = f"https://{SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 60
N_DOCS = 3


def is_doc_image(url):
    return ('/files/documents/' in url and '/images/' in url and '.png' in url)


def parse_rows(page):
    return page.evaluate("""() => {
        const out=[]; const t=document.querySelector('table'); if(!t) return out;
        const heads=Array.from(t.querySelectorAll('th')).map(h=>(h.textContent||'').trim().toLowerCase());
        const idx=n=>heads.findIndex(h=>h.includes(n));
        const di=idx('doc type'), pa=idx('property address');
        for(const tr of Array.from(t.querySelectorAll('tr')).slice(1)){
            const c=Array.from(tr.querySelectorAll('td')).map(td=>(td.textContent||'').trim());
            if(!c.length) continue;
            const cb=tr.querySelector('input[id^="table-checkbox-"]');
            out.push({doc_type:c[di]||'', property_address:(pa>=0?c[pa]:'')||'',
                      doc_id: cb?cb.id.replace('table-checkbox-',''):''});
        }
        return out;
    }""")


def main():
    captured = []
    s = (TODAY - timedelta(days=WINDOW_DAYS)).strftime('%Y%m%d')
    e = TODAY.strftime('%Y%m%d')
    results_url = f"{BASE}/results?department=FC&recordedDateRange={s},{e}&searchType=advancedSearch"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: captured.append(r.url) if is_doc_image(r.url) else None)

        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)

        rows = [r for r in parse_rows(page) if 'FORECLOS' in r['doc_type'].upper() and r['doc_id']][:N_DOCS]
        log.info(f"{len(rows)} docs; table property_address values: {[r['property_address'] for r in rows]}")

        import pytesseract
        from PIL import Image
        import io
        for r in rows:
            captured.clear()
            page.goto(f"{BASE}/doc/{r['doc_id']}", wait_until='domcontentloaded')
            for _ in range(28):
                if any(is_doc_image(u) for u in captured):
                    break
                page.wait_for_timeout(250)
            png = next((u for u in captured if is_doc_image(u)), None)
            log.info("=" * 70)
            log.info(f"DOC {r['doc_id']} table_addr={r['property_address']!r}")
            if not png:
                log.info("  no png"); continue
            body = ctx.request.get(png).body()
            txt = pytesseract.image_to_string(Image.open(io.BytesIO(body)))
            for ln in txt.split('\n'):
                if ln.strip():
                    log.info(f"  | {ln.strip()}")

        browser.close()


if __name__ == '__main__':
    main()
