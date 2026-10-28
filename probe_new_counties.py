"""Probe: are Montgomery / Fort Bend / Williamson / Comal on publicsearch.us with a Foreclosures (FC) department?"""
import json, os
from datetime import date, timedelta
from playwright.sync_api import sync_playwright
out = {}
end = date.today(); start = end - timedelta(days=30)
rng = f"{start:%Y%m%d},{end:%Y%m%d}"
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    ctx = b.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36')
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    for slug in ['montgomery', 'fortbend', 'fort-bend', 'williamson', 'comal', 'galveston', 'brazoria']:
        p = ctx.new_page(); info = {}
        try:
            r = p.goto(f"https://{slug}.tx.publicsearch.us/", timeout=40000); p.wait_for_timeout(3000)
            info['home'] = [r.status if r else None, p.title()[:80]]
            r = p.goto(f"https://{slug}.tx.publicsearch.us/results?department=FC&recordedDateRange={rng}&searchType=advancedSearch", timeout=40000)
            p.wait_for_timeout(6000)
            t = p.inner_text('body')
            info['fc'] = [r.status if r else None, p.title()[:80], t[:600]]
        except Exception as e:
            info['err'] = str(e)[:200]
        out[slug] = info; p.close()
    b.close()
os.makedirs('probe_out', exist_ok=True); json.dump(out, open('probe_out/new_counties.json', 'w'), indent=1)
