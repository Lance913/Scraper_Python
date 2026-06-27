"""
Probe v11 — confirm the direct Foreclosures results URL, parse the new table
schema, check pagination, and OCR a real Notice of Foreclosure to see where the
OWNER NAME sits (the only field the results table doesn't give us).

What v10 established:
  - Department=Foreclosures (department=FC) returns a foreclosure-only table:
      Doc Type | Recorded Date | Sale Date | Doc Number | Remarks | Property Address
    The Sale Date AND full street Property Address come straight from the table
    (no OCR needed). Only the owner/grantor NAME is missing from the table.
  - Results are query-driven:
      {base}/results?department=FC&recordedDateRange=YYYYMMDD,YYYYMMDD&searchType=advancedSearch

v11:
  1. Navigate DIRECTLY to that results URL (no form interaction).
  2. Parse the new table schema; print several rows.
  3. Inspect pagination (controls + any total-count text).
  4. Click the first NOTICE OF FORECLOSE doc, OCR its pages -> find the owner name.
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
COUNTY_NAME = "Bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 120


def is_doc_image(url):
    return ('/files/documents/' in url and '/images/' in url and '.png' in url)


def parse_fc_table(page):
    """Parse the Foreclosures results table into row dicts using its real headers."""
    return page.evaluate("""() => {
        const out = [];
        for (const t of document.querySelectorAll('table')) {
            const heads = Array.from(t.querySelectorAll('th')).map(h => (h.textContent||'').trim().toLowerCase());
            const idx = name => heads.findIndex(h => h.includes(name));
            const di = idx('doc type'), rd = idx('recorded'), sd = idx('sale date'),
                  dn = idx('doc number'), rm = idx('remark'), pa = idx('property address');
            if (di < 0) continue;
            for (const tr of Array.from(t.querySelectorAll('tr')).slice(1)) {
                const c = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent||'').trim());
                if (!c.length) continue;
                out.push({
                    doc_type: c[di]||'', recorded: c[rd]||'', sale_date: c[sd]||'',
                    doc_number: c[dn]||'', remarks: c[rm]||'', property_address: c[pa]||'',
                });
            }
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

        # Need a session first (cookies), then go straight to the results URL.
        log.info("Warm up session at base ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        log.info(f"Direct nav: {results_url}")
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)
        log.info(f"landed: {page.url}")

        rows = parse_fc_table(page)
        log.info(f"parsed {len(rows)} rows from FC table")
        for r in rows[:8]:
            log.info(f"  {r}")

        # Pagination / total-count inspection.
        pag = page.evaluate("""() => {
            const txt = (document.body.innerText||'');
            const m = txt.match(/([\\d,]+)\\s+(results|records|documents)/i);
            const btns = Array.from(document.querySelectorAll('button,a,[role="button"]'))
              .map(b => ((b.textContent||'').trim() + '|' + (b.getAttribute('aria-label')||'')))
              .filter(s => /next|prev|page|\\u203a|\\u2039|\\d+\\s*of\\s*\\d+/i.test(s)).slice(0, 25);
            return { count_phrase: m ? m[0] : '(none)', controls: btns };
        }""")
        log.info(f"count phrase: {pag['count_phrase']}")
        log.info(f"pagination controls: {pag['controls']}")

        # OCR the first NOTICE OF FORECLOSE doc to locate the owner name.
        nts = next((r for r in rows if 'FORECLOS' in r['doc_type'].upper()
                    or 'TRUSTEE' in r['doc_type'].upper()), None)
        if not nts:
            log.info("no foreclosure row to OCR"); browser.close(); return
        doc_num = nts['doc_number']
        log.info(f"OCR target: doc={doc_num} addr={nts['property_address']!r} sale={nts['sale_date']!r}")

        captured.clear()
        clicked = False
        for sel in [f'td:text-is("{doc_num}")', f'td:has-text("{doc_num}")']:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.scroll_into_view_if_needed(); el.click(); clicked = True; break
        if not clicked:
            log.info("could not click doc cell"); browser.close(); return

        page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)
        log.info(f"doc page: {page.url}")
        try:
            for _ in range(4):
                nxt = page.locator('button:has-text("Next in Book"), [aria-label*="next" i]').first
                if nxt.count() > 0 and nxt.is_visible():
                    nxt.click(); page.wait_for_timeout(2000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        log.info(f"captured {len(captured)} image URL(s)")
        os.makedirs('/tmp/ps', exist_ok=True)
        saved, seen = [], set()
        for u in captured:
            k = u.split('?')[0]
            if k in seen:
                continue
            seen.add(k)
            try:
                body = ctx.request.get(u).body()
                fn = f"/tmp/ps/{len(saved)}.png"
                with open(fn, 'wb') as f:
                    f.write(body)
                saved.append(fn)
                log.info(f"  saved page {len(saved)}: {len(body)} bytes hdr={body[:4]!r}")
            except Exception as ex:
                log.info(f"  dl err {str(ex)[:60]}")

        import pytesseract
        from PIL import Image
        log.info("===== REAL NOTICE OF FORECLOSURE OCR =====")
        for fn in saved[:3]:
            txt = pytesseract.image_to_string(Image.open(fn))
            log.info(f"----- {fn} ({len(txt)} chars) -----")
            for ln in txt.split('\n'):
                if ln.strip():
                    log.info(f"  | {ln.strip()}")

        browser.close()


if __name__ == '__main__':
    main()
