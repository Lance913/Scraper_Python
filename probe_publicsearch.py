"""
Probe v14 — is the owner/grantor name available WITHOUT OCR?

v13 showed the FC results are server-rendered HTML (no JSON API). The visible
table has no name column. v14 checks two cheap (no-OCR) name sources:
  1. The raw outerHTML of a result row — hidden cells / data-attributes / the
     link to /doc/{id}.
  2. The server-rendered /doc/{id} detail page HTML — many record portals list
     Grantor/Grantee/DocType/Legal there even when the document body is a PNG.
If either contains the grantor name, we can fetch it via ctx.request.get() per
doc (fast) instead of OCR.
"""
import logging
import os
import re
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


def main():
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

        log.info("Warm up session ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        log.info(f"Nav FC results: {results_url}")
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(5000)

        # 1) Raw HTML of the first 2 data rows.
        rows_html = page.evaluate("""() => {
            const t = document.querySelector('table');
            if (!t) return [];
            return Array.from(t.querySelectorAll('tr')).slice(1, 3).map(tr => tr.outerHTML);
        }""")
        log.info("===== RAW ROW HTML (first 2) =====")
        for i, h in enumerate(rows_html):
            log.info(f"----- row[{i}] -----")
            for chunk in re.findall(r'.{1,300}', h):
                log.info(f"  {chunk}")

        # Find a /doc/{id} link from the row HTML or any anchor.
        doc_hrefs = page.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/doc/"]'))
            .map(a => a.getAttribute('href')).slice(0, 5)""")
        log.info(f"/doc/ hrefs found: {doc_hrefs}")

        # If no anchors, click first row to discover the doc id.
        doc_id = None
        if doc_hrefs:
            m = re.search(r'/doc/(\d+)', doc_hrefs[0])
            doc_id = m.group(1) if m else None
        if not doc_id:
            first_cell = page.locator('table tr:nth-child(2) td').nth(6)  # doc number col
            if first_cell.count() > 0:
                first_cell.click()
                page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)
                m = re.search(r'/doc/(\d+)', page.url)
                doc_id = m.group(1) if m else None
                log.info(f"navigated to doc page: {page.url}")

        log.info(f"doc_id = {doc_id}")
        if not doc_id:
            log.info("no doc id; stopping"); browser.close(); return

        # 2) Fetch the /doc/{id} detail page HTML via the session request API.
        doc_url = f"{BASE}/doc/{doc_id}"
        resp = ctx.request.get(doc_url)
        html = resp.text()
        log.info(f"doc page HTML len={len(html)} status={resp.status}")

        # Look for party-name labels in the HTML.
        for label in ['grantor', 'grantee', 'party', 'partyName', 'data-grantor',
                      'doctype', 'doc_type', 'legal', 'instrument']:
            idxs = [m.start() for m in re.finditer(label, html, re.I)][:3]
            for ix in idxs:
                snippet = html[max(0, ix - 40):ix + 160].replace('\n', ' ')
                log.info(f"  [{label}] ...{snippet}...")

        # Also dump any visible text that looks like a name list near the top.
        try:
            page.goto(doc_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(3000)
            vis = page.evaluate(
                "() => (document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean).slice(0,60)"
            )
            log.info("===== DOC PAGE VISIBLE TEXT (first 60 lines) =====")
            for ln in vis:
                log.info(f"  | {ln}")
        except Exception as ex:
            log.info(f"doc visible-text err: {str(ex)[:80]}")

        browser.close()


if __name__ == '__main__':
    main()
