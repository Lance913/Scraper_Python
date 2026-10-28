"""Probe: how many result pages does Harris FRCL_R show per auction month, and what does the pager look like?"""
import json, re, sys
from playwright.sync_api import sync_playwright
URL = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
out = {}
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    for y, m in [(2026, 11), (2026, 12), (2027, 1)]:
        p = b.new_page(); p.goto(URL); p.wait_for_load_state('networkidle')
        p.select_option('select[name="ctl00$ContentPlaceHolder1$ddlYear"]', str(y)); p.wait_for_load_state('networkidle'); p.wait_for_timeout(800)
        p.select_option('select[name="ctl00$ContentPlaceHolder1$ddlMonth"]', str(m)); p.wait_for_timeout(300)
        with p.expect_navigation(wait_until='networkidle', timeout=30000): p.click('input[name="ctl00$ContentPlaceHolder1$btnSearch"]')
        p.wait_for_timeout(2000)
        info = p.evaluate("""() => {
          const t = document.body.innerText;
          const frcl = (t.match(/FRCL-\\d{4}-\\d+/g) || []);
          const links = [...document.querySelectorAll('a')].map(a => [a.textContent.trim(), (a.getAttribute('href')||'').slice(0,90)]).filter(x => /^(\\d+|Next|Last|First|Prev|\\.\\.\\.|>|>>)$/.test(x[0]));
          const counts = t.match(/(\\d+)\\s+(record|records|result|results|items)/ig) || [];
          const sels = [...document.querySelectorAll('select')].map(s => s.name + '=' + s.value + ' [' + [...s.options].slice(0,8).map(o=>o.value).join(',') + ']');
          return {frcl_on_page: new Set(frcl).size, pager: links.slice(0,40), counts, selects: sels, snippet: t.slice(t.indexOf('FRCL')-400, t.indexOf('FRCL')+200)};
        }""")
        out[f'{y}-{m}'] = info; p.close()
    b.close()
import os; os.makedirs('probe_out', exist_ok=True)
json.dump(out, open('probe_out/harris_pages.json', 'w'), indent=1); print(json.dumps(out, indent=1)[:3000])
