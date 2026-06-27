"""
Probe v13 — robustly find the JSON data API behind the FC results page.

v12 captured almost nothing (reading bodies inside the response event failed
silently). v13 records EVERY response object, then after the page settles prints
all xhr/fetch endpoints and reads the body of the data API (the one mentioning
grantor/saleDate/docNumber) to see if owner names are available without OCR.
"""
import json
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


def summarize(obj, depth=0, maxd=4):
    pad = '  ' * depth
    if depth > maxd:
        return pad + '...'
    if isinstance(obj, dict):
        out = []
        for k, v in list(obj.items())[:50]:
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}:")
                out.append(summarize(v, depth + 1, maxd))
            else:
                sv = repr(v)
                out.append(f"{pad}{k}: {sv[:90]}")
        return '\n'.join(out)
    if isinstance(obj, list):
        if not obj:
            return pad + '[]'
        return f"{pad}[list len={len(obj)}] first:\n" + summarize(obj[0], depth + 1, maxd)
    return pad + repr(obj)[:90]


def main():
    s = (TODAY - timedelta(days=WINDOW_DAYS)).strftime('%Y%m%d')
    e = TODAY.strftime('%Y%m%d')
    results_url = (f"{BASE}/results?department=FC"
                   f"&recordedDateRange={s},{e}&searchType=advancedSearch")

    responses = []  # raw Response objects

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
        page.on('response', lambda r: responses.append(r))

        log.info("Warm up session ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        log.info(f"Nav to FC results: {results_url}")
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(6000)

        # List all xhr/fetch endpoints.
        log.info("===== XHR / FETCH ENDPOINTS =====")
        api_candidates = []
        for r in responses:
            try:
                rt = r.request.resource_type
            except Exception:
                rt = '?'
            if rt in ('xhr', 'fetch'):
                ct = r.headers.get('content-type', '')
                log.info(f"  [{r.status}] {rt} {ct[:25]} {r.url[:170]}")
                if 'json' in ct or '/api' in r.url or 'graphql' in r.url.lower():
                    api_candidates.append(r)

        # Read bodies of API candidates; find the one with result rows.
        log.info(f"===== READING {len(api_candidates)} API CANDIDATE BODIES =====")
        data_resp = None
        for r in api_candidates:
            try:
                body = r.text()
            except Exception as ex:
                log.info(f"  body read failed for {r.url[:80]}: {str(ex)[:60]}")
                continue
            low = body.lower()
            hit = any(k in low for k in ['grantor', 'grantee', 'saledate', 'sale_date',
                                         'docnumber', 'doc_number', 'instrumentnumber',
                                         'searchresult', 'results', 'totalcount'])
            log.info(f"  {r.url[:120]} len={len(body)} data_hit={hit}")
            if hit and (data_resp is None or len(body) > len(data_resp[1])):
                data_resp = (r.url, body)

        if not data_resp:
            log.info("No data API body identified."); browser.close(); return

        url, body = data_resp
        log.info(f"===== DATA API =====\n{url}")
        try:
            data = json.loads(body)
            log.info("----- SHAPE -----")
            for ln in summarize(data).split('\n'):
                log.info(ln)

            def find_rows(o):
                if isinstance(o, list) and o and isinstance(o[0], dict):
                    return o
                if isinstance(o, dict):
                    for v in o.values():
                        rr = find_rows(v)
                        if rr:
                            return rr
                return None
            rows = find_rows(data)
            if rows:
                log.info(f"----- SAMPLE RECORD (of {len(rows)}) -----")
                log.info(json.dumps(rows[0], indent=2)[:5000])
        except Exception as ex:
            log.info(f"JSON parse failed ({ex}); head:")
            log.info(body[:3000])

        browser.close()


if __name__ == '__main__':
    main()
