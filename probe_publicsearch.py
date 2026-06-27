"""
Probe v17 — can we get the page-1 PNG URL WITHOUT rendering the React doc viewer?

The OCR bottleneck is that each owner name currently needs page.goto(/doc/{id})
+ a ~6s wait for the React viewer to request the signed PNG. If the signed image
URL is already present in the server-rendered /doc/{id} HTML (fetchable via
ctx.request.get with session cookies, ~0.2s), we can OCR every upcoming doc
cheaply and get names for ALL leads.

This probe: warm a session, grab a few FC doc_ids, fetch /doc/{id} HTML via
request.get, and look for the /files/documents/.../images/*.png?...sig=... URL.
If found, fetch + OCR it directly (no viewer) and time the whole thing.
"""
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTY_SLUG = "bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 120

IMG_RE = re.compile(r'https://[^\s"\'<>]*?/files/documents/[^\s"\'<>]*?/images/[^\s"\'<>]*?\.png[^\s"\'<>]*')


def parse_rows(page):
    return page.evaluate("""() => {
        const out=[]; const t=document.querySelector('table'); if(!t) return out;
        const heads=Array.from(t.querySelectorAll('th')).map(h=>(h.textContent||'').trim().toLowerCase());
        const idx=n=>heads.findIndex(h=>h.includes(n));
        const di=idx('doc type'), dn=idx('doc number'), pa=idx('property address');
        for(const tr of Array.from(t.querySelectorAll('tr')).slice(1)){
            const c=Array.from(tr.querySelectorAll('td')).map(td=>(td.textContent||'').trim());
            if(!c.length) continue;
            const cb=tr.querySelector('input[id^="table-checkbox-"]');
            out.push({doc_type:c[di]||'', doc_number:c[dn]||'', property_address:c[pa]||'',
                      doc_id: cb?cb.id.replace('table-checkbox-',''):''});
        }
        return out;
    }""")


def main():
    s = (TODAY - timedelta(days=WINDOW_DAYS)).strftime('%Y%m%d')
    e = TODAY.strftime('%Y%m%d')
    results_url = (f"{BASE}/results?department=FC&recordedDateRange={s},{e}&searchType=advancedSearch")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)

        log.info("warm up + load FC results ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)

        rows = [r for r in parse_rows(page) if 'FORECLOS' in r['doc_type'].upper() and r['doc_id']][:5]
        log.info(f"testing {len(rows)} docs")

        import pytesseract
        from PIL import Image
        import io

        from scrapers.publicsearch_extract import parse_owner, split_name

        for r in rows:
            t0 = time.monotonic()
            html = ctx.request.get(f"{BASE}/doc/{r['doc_id']}").text()
            urls = IMG_RE.findall(html)
            log.info(f"doc {r['doc_id']}: HTML {len(html)}B, {len(urls)} image URL(s) in HTML")
            if not urls:
                # show whether any 'files/documents' fragment exists at all
                frag = re.findall(r'files/documents[^\s"\'<>]{0,80}', html)[:2]
                log.info(f"   no full PNG url; fragments={frag}")
                continue
            png = urls[0]
            log.info(f"   url: {png[:130]}")
            body = ctx.request.get(png).body()
            txt = pytesseract.image_to_string(Image.open(io.BytesIO(body)))
            owner = parse_owner(txt)
            dt = time.monotonic() - t0
            log.info(f"   {len(body)}B png, OCR owner={owner!r} {split_name(owner) if owner else ''} "
                     f"[{dt:.1f}s total, NO viewer render]")

        browser.close()


if __name__ == '__main__':
    main()
