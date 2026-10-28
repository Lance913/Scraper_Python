"""Probe 2: why Harris paging stops after page 1 and why doc OCR returns nothing."""
import json, os, re
from playwright.sync_api import sync_playwright
SEARCH_URL = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
APP_BASE = "https://www.cclerk.hctx.net/applications/websearch/"
out = {}
def firsts(page):
    return page.evaluate("""() => [...document.querySelectorAll('a')].filter(x => /FRCL/.test(x.textContent||'')).map(a => [a.textContent.trim(), a.getAttribute('href')]).slice(0, 3)""")
with sync_playwright() as p:
    b = p.chromium.launch(headless=True); ctx = b.new_context(accept_downloads=True); page = ctx.new_page()
    page.goto(SEARCH_URL); page.wait_for_load_state('networkidle')
    page.select_option('select[name="ctl00$ContentPlaceHolder1$ddlYear"]', '2026'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
    opts = page.evaluate("""() => [...document.querySelector('select[name="ctl00$ContentPlaceHolder1$ddlMonth"]').options].map(o => [o.value, o.text])""")
    out['month_opts'] = opts
    val = next(v for v, t in opts if t.strip().lower().startswith('nov'))
    page.select_option('select[name="ctl00$ContentPlaceHolder1$ddlMonth"]', val); page.wait_for_timeout(500)
    page.click('input[name="ctl00$ContentPlaceHolder1$btnSearch"]'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(3000)
    out['p1'] = firsts(page); out['p1_url'] = page.url
    out['p1_count'] = len(page.evaluate("""() => [...document.querySelectorAll('a')].filter(x => /FRCL/.test(x.textContent||''))"""))
    # try the postback (expected to fail)
    try:
        page.evaluate("""() => __doPostBack('ctl00$ContentPlaceHolder1$GridView1','Page$2')""")
    except Exception as e: out['pb_err'] = str(e)[:300]
    for w in (3, 8, 20):
        page.wait_for_timeout(w * 1000 if w == 3 else (w - 3) * 1000)
        try: out[f'after_{w}s'] = [page.url, firsts(page)]
        except Exception as e: out[f'after_{w}s'] = str(e)[:200]
    # try clicking the "3" pager link
    try:
        page.locator('a', has_text=re.compile(r'^\s*3\s*$')).first.click(); page.wait_for_load_state('networkidle'); page.wait_for_timeout(4000)
        out['click3'] = firsts(page)
    except Exception as e: out['click3_err'] = str(e)[:300]
    # doc fetch
    import sys; sys.path.insert(0, '.')
    from scrapers.harris_extract import ocr_pdf_bytes, extract_from_text
    out['docs'] = []
    for doc_id, href in (out.get('click3') or [])[:3]:
        body = ctx.request.get(APP_BASE + href).body()
        t = ocr_pdf_bytes(body, max_pages=3)
        out['docs'].append({'id': doc_id, 'len': len(body), 'text': t[:3000], 'extract': extract_from_text(t)})
    b.close()
os.makedirs('probe_out', exist_ok=True); json.dump(out, open('probe_out/harris_pages2.json', 'w'), indent=1)
