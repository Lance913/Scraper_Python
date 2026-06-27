"""
Probe v15 — de-risk the FC scraper rewrite. Two things:
  1. Doc-type distribution across several FC results pages (so is_nts is correct).
  2. End-to-end owner-NAME path: extract docId from a NOTICE row's
     `table-checkbox-{docId}`, open /doc/{docId}, capture the page-1 PNG, OCR it,
     and parse the owner with harris_extract's existing patterns.
Also tallies how many rows are upcoming (sale_date >= today).
"""
import logging
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, datetime, timedelta
from playwright.sync_api import sync_playwright
from scrapers.harris_extract import parse_owner

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTY_SLUG = "bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 120
SCAN_PAGES = 4


def is_doc_image(url):
    return ('/files/documents/' in url and '/images/' in url and '.png' in url)


def parse_rows(page):
    """Parse FC results rows incl. the internal docId from the checkbox id."""
    return page.evaluate("""() => {
        const out = [];
        const t = document.querySelector('table');
        if (!t) return out;
        const heads = Array.from(t.querySelectorAll('th')).map(h => (h.textContent||'').trim().toLowerCase());
        const idx = n => heads.findIndex(h => h.includes(n));
        const di=idx('doc type'), rd=idx('recorded'), sd=idx('sale date'),
              dn=idx('doc number'), rm=idx('remark'), pa=idx('property address');
        for (const tr of Array.from(t.querySelectorAll('tr')).slice(1)) {
            const c = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent||'').trim());
            if (!c.length) continue;
            const cb = tr.querySelector('input[id^="table-checkbox-"]');
            const docId = cb ? cb.id.replace('table-checkbox-','') : '';
            out.push({ doc_type:c[di]||'', recorded:c[rd]||'', sale_date:c[sd]||'',
                       doc_number:c[dn]||'', remarks:c[rm]||'', property_address:c[pa]||'', doc_id:docId });
        }
        return out;
    }""")


def go_next(page):
    el = page.locator('[aria-label="next page"]').first
    if el.count() > 0 and el.is_enabled():
        el.click(); page.wait_for_load_state('networkidle'); page.wait_for_timeout(2500)
        return True
    return False


def upcoming(sale_date):
    try:
        return datetime.strptime(sale_date, '%m/%d/%Y').date() >= TODAY
    except Exception:
        return False


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

        # 1) Scan several pages: doc-type distribution + upcoming tally.
        dtypes = Counter()
        up = 0
        total = 0
        sample_notice = []
        for pg in range(1, SCAN_PAGES + 1):
            rows = parse_rows(page)
            log.info(f"page {pg}: {len(rows)} rows")
            for r in rows:
                total += 1
                dtypes[r['doc_type']] += 1
                if upcoming(r['sale_date']):
                    up += 1
                if 'FORECLOS' in r['doc_type'].upper() and r['doc_id'] and len(sample_notice) < 3:
                    sample_notice.append(r)
            if not go_next(page):
                log.info("no next page"); break

        log.info(f"===== DOC TYPE DISTRIBUTION ({total} rows over {SCAN_PAGES} pages) =====")
        for dt, n in dtypes.most_common():
            log.info(f"  {n:4}  {dt!r}")
        log.info(f"upcoming (sale_date >= {TODAY}): {up}/{total}")

        # 2) Owner-name OCR path for a few NOTICE rows (fetch /doc/{id} -> PNG -> OCR).
        import pytesseract
        from PIL import Image
        os.makedirs('/tmp/ps', exist_ok=True)
        log.info("===== OWNER-NAME OCR VALIDATION =====")
        for r in sample_notice:
            captured.clear()
            doc_url = f"{BASE}/doc/{r['doc_id']}"
            page.goto(doc_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(5000)
            if not captured:
                log.info(f"  doc {r['doc_id']}: no PNG captured"); continue
            png_url = captured[0]
            try:
                body = ctx.request.get(png_url).body()
                fn = f"/tmp/ps/{r['doc_id']}.png"
                with open(fn, 'wb') as f:
                    f.write(body)
                txt = pytesseract.image_to_string(Image.open(fn))
                owner = parse_owner(txt)
                log.info(f"  doc {r['doc_id']} addr={r['property_address']!r} sale={r['sale_date']!r}")
                log.info(f"     -> OWNER(parse_owner) = {owner!r}  (page1 {len(txt)} chars, {len(body)} bytes)")
            except Exception as ex:
                log.info(f"  doc {r['doc_id']} err: {str(ex)[:80]}")

        browser.close()


if __name__ == '__main__':
    main()
