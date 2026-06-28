"""
Probe v19 — why does Tarrant return 0 FC rows? Dump Tarrant's department options
and the FC results page structure (table headers, row count, any 'no results'
message), compared to what Bexar returns.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

TODAY = date(2026, 6, 27)


def inspect(pw, slug):
    base = f"https://{slug}.tx.publicsearch.us"
    s = (TODAY - timedelta(days=60)).strftime('%Y%m%d')
    e = TODAY.strftime('%Y%m%d')
    results_url = f"{base}/results?department=FC&recordedDateRange={s},{e}&searchType=advancedSearch"
    browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    ctx = browser.new_context(user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                          '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'))
    page = ctx.new_page()
    page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page.set_default_timeout(30000)
    try:
        log.info(f"===== {slug.upper()} =====")
        page.goto(base); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        # Department options on the advanced search page.
        page.goto(base + '/search/advanced'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1500)
        depts = page.evaluate("""() => {
            const out=[];
            const lb=document.querySelector('#department-listbox');
            if(lb) for(const o of lb.querySelectorAll('[role="option"],li')) out.push((o.textContent||'').trim());
            return out;
        }""")
        log.info(f"{slug}: department options: {depts}")

        # FC results.
        page.goto(results_url); page.wait_for_load_state('networkidle'); page.wait_for_timeout(5000)
        log.info(f"{slug}: results URL -> {page.url}")
        info = page.evaluate("""() => {
            const tables=[...document.querySelectorAll('table')].map(t=>({
                headers:[...t.querySelectorAll('th')].map(h=>(h.textContent||'').trim()),
                rows: t.querySelectorAll('tr').length
            }));
            const body=(document.body.innerText||'');
            const nores=/no\\s+results|0\\s+results|no\\s+records|did not match/i.test(body);
            const m=body.match(/([\\d,]+)\\s+results/i);
            return {tables, noResultsMsg:nores, countPhrase: m?m[0]:'(none)',
                    bodyHead: body.split('\\n').map(s=>s.trim()).filter(Boolean).slice(0,40)};
        }""")
        log.info(f"{slug}: count={info['countPhrase']} noResultsMsg={info['noResultsMsg']}")
        for t in info['tables']:
            log.info(f"{slug}: table headers={t['headers']} rows={t['rows']}")
        if not info['tables'] or info['noResultsMsg']:
            log.info(f"{slug}: BODY TEXT (first 40 lines):")
            for ln in info['bodyHead']:
                log.info(f"  | {ln}")
    except Exception as ex:
        log.info(f"{slug}: error {str(ex)[:120]}")
    finally:
        browser.close()


def main():
    with sync_playwright() as pw:
        inspect(pw, 'tarrant')
        inspect(pw, 'bexar')  # control: known-good


if __name__ == '__main__':
    main()
