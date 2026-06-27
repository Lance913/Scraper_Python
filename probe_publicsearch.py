"""
Probe v16 — dump full OCR text of a few FC 'NOTICE OF FORECLOSE' docs (all pages)
so we can write publicsearch_extract.py's owner-name parser against the real
publicsearch NTS layout. harris_extract.parse_owner returns '' here because the
phrasing is "executed by NAME, securing the payment..." (terminator differs).
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTY_SLUG = "bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 120
N_DOCS = 3


def is_doc_image(url):
    return ('/files/documents/' in url and '/images/' in url and '.png' in url)


def parse_rows(page):
    return page.evaluate("""() => {
        const out = []; const t = document.querySelector('table'); if (!t) return out;
        const heads = Array.from(t.querySelectorAll('th')).map(h => (h.textContent||'').trim().toLowerCase());
        const idx = n => heads.findIndex(h => h.includes(n));
        const di=idx('doc type'), sd=idx('sale date'), dn=idx('doc number'), pa=idx('property address');
        for (const tr of Array.from(t.querySelectorAll('tr')).slice(1)) {
            const c = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent||'').trim());
            if (!c.length) continue;
            const cb = tr.querySelector('input[id^="table-checkbox-"]');
            out.push({ doc_type:c[di]||'', sale_date:c[sd]||'', doc_number:c[dn]||'',
                       property_address:c[pa]||'', doc_id: cb ? cb.id.replace('table-checkbox-','') : '' });
        }
        return out;
    }""")


def main():
    captured = []
    s = (TODAY - timedelta(days=WINDOW_DAYS)).strftime('%Y%m%d')
    e = TODAY.strftime('%Y%m%d')
    results_url = (f"{BASE}/results?department=FC"
                   f"&recordedDateRange={s},{e}&searchType=advancedSearch")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=['--disable-blink-features=AutomationControlled']
        )
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: captured.append(r.url) if is_doc_image(r.url) else None)

        log.info("Warm up ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)

        rows = [r for r in parse_rows(page) if 'FORECLOS' in r['doc_type'].upper() and r['doc_id']]
        targets = rows[:N_DOCS]
        log.info(f"dumping OCR for {len(targets)} docs")

        import pytesseract
        from PIL import Image
        os.makedirs('/tmp/ps', exist_ok=True)

        for r in targets:
            captured.clear()
            page.goto(f"{BASE}/doc/{r['doc_id']}")
            page.wait_for_load_state('networkidle'); page.wait_for_timeout(5000)
            # Force multi-page image loads.
            try:
                for _ in range(4):
                    nxt = page.locator('button:has-text("Next in Book"), [aria-label*="next in book" i]').first
                    if nxt.count() > 0 and nxt.is_visible():
                        nxt.click(); page.wait_for_timeout(2000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            seen, pages = set(), []
            for u in captured:
                k = u.split('?')[0]
                if k in seen:
                    continue
                seen.add(k)
                try:
                    body = ctx.request.get(u).body()
                    fn = f"/tmp/ps/{r['doc_id']}_{len(pages)}.png"
                    with open(fn, 'wb') as f:
                        f.write(body)
                    pages.append(fn)
                except Exception as ex:
                    log.info(f"  dl err {str(ex)[:50]}")

            log.info("=" * 70)
            log.info(f"DOC {r['doc_id']} | {r['doc_type']} | sale={r['sale_date']} | "
                     f"addr={r['property_address']!r} | {len(pages)} page(s)")
            for pi, fn in enumerate(pages[:3]):
                txt = pytesseract.image_to_string(Image.open(fn))
                log.info(f"----- page {pi+1} ({len(txt)} chars) -----")
                for ln in txt.split('\n'):
                    if ln.strip():
                        log.info(f"  | {ln.strip()}")

        browser.close()


if __name__ == '__main__':
    main()
