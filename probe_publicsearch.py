"""
Probe v12 — find the JSON API behind the Foreclosures results page.

The results page is a React app; it fetches rows from a backend API. If that API
returns the owner/grantor NAME alongside address + sale date, we can skip OCR
entirely and get complete leads fast. This probe captures every JSON/XHR
response while loading the FC results URL and dumps the most promising payload's
structure (keys + a sample record).
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

captured = []  # (url, status, content_type, body_text)


def on_response(resp):
    try:
        ct = resp.headers.get('content-type', '')
        url = resp.url
        # Capture JSON / api-looking responses; skip static assets + images.
        if ('application/json' in ct or '/api' in url or '/search' in url) and \
           '.png' not in url and '.js' not in url and '.css' not in url:
            body = resp.text()
            captured.append((url, resp.status, ct, body[:200000]))
    except Exception:
        pass


def summarize(obj, depth=0, maxd=3):
    """Return a compact shape description of a JSON object."""
    pad = '  ' * depth
    if depth > maxd:
        return pad + '...'
    if isinstance(obj, dict):
        lines = []
        for k, v in list(obj.items())[:40]:
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(summarize(v, depth + 1, maxd))
            else:
                sv = repr(v)
                if len(sv) > 80:
                    sv = sv[:80] + '...'
                lines.append(f"{pad}{k}: {sv}")
        return '\n'.join(lines)
    if isinstance(obj, list):
        if not obj:
            return pad + '[] (empty)'
        return f"{pad}[list len={len(obj)}], first item:\n" + summarize(obj[0], depth + 1, maxd)
    return pad + repr(obj)


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
        page.on('response', on_response)

        log.info("Warm up session ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        log.info(f"Nav to FC results: {results_url}")
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(5000)

        log.info(f"Captured {len(captured)} JSON/API responses:")
        for url, status, ct, body in captured:
            log.info(f"  [{status}] {ct[:30]} {url[:160]}  (len={len(body)})")

        # Find the payload that most likely holds the result rows.
        best = None
        for url, status, ct, body in captured:
            low = body.lower()
            if any(k in low for k in ['grantor', 'grantee', 'saledate', 'sale_date',
                                      'docnumber', 'doc_number', 'instrument', 'searchresult']):
                best = (url, body)
                break
        if not best and captured:
            best = max(captured, key=lambda c: len(c[3]))[0:1] + (max(captured, key=lambda c: len(c[3]))[3],)

        if not best:
            log.info("No JSON payload captured."); browser.close(); return

        url, body = best
        log.info(f"===== MOST PROMISING PAYLOAD =====\n{url}")
        try:
            data = json.loads(body)
            log.info("----- SHAPE -----")
            for ln in summarize(data).split('\n'):
                log.info(ln)
            # Try to print one full result record if we can find a list of rows.
            def find_rows(o):
                if isinstance(o, list) and o and isinstance(o[0], dict):
                    return o
                if isinstance(o, dict):
                    for v in o.values():
                        r = find_rows(v)
                        if r:
                            return r
                return None
            rows = find_rows(data)
            if rows:
                log.info(f"----- SAMPLE RECORD (of {len(rows)}) -----")
                log.info(json.dumps(rows[0], indent=2)[:4000])
        except Exception as ex:
            log.info(f"JSON parse failed ({ex}); raw head:")
            log.info(body[:3000])

        browser.close()


if __name__ == '__main__':
    main()
