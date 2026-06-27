"""
Probe v5 — check what the RESULTS TABLE already gives us for NTS rows across
all 5 publicsearch counties. We want to confirm: does the table already contain
the property address (street/city/zip) for NTS records? If so, no OCR needed.

Searches each county's recent window, finds NTS rows, prints the full row data
(grantor + address column) exactly as the table provides it.
"""
import re, logging
from datetime import date, timedelta
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTIES = [('bexar','Bexar'),('dallas','Dallas'),('tarrant','Tarrant'),
            ('denton','Denton'),('johnson','Johnson')]

NTS_KEYS = ['NOTICE OF TRUSTEE','NOTICE OF SUBSTITUTE','TRUSTEE SALE','NTS','NOTICE OF FORECLOSURE']

def is_nts(dt):
    dt = dt.upper()
    if 'APPOINTMENT' in dt: return False
    if any(k in dt for k in NTS_KEYS): return True
    if dt == 'NOTICE': return True
    return False

def scan_county(pw, slug, name):
    base = f"https://{slug}.tx.publicsearch.us"
    start_fmt = (date(2026,6,27) - timedelta(days=90)).strftime('%m/%d/%Y')
    end_fmt   = date(2026,6,27).strftime('%m/%d/%Y')
    browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
    ctx = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
    page = ctx.new_page()
    page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page.set_default_timeout(30000)
    try:
        page.goto(base); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(base+'/search/advanced'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1200)
        for s,e in [('input[id*="start" i]','input[id*="end" i]')]:
            if page.locator(s).count()>0:
                page.fill(s,start_fmt); page.fill(e,end_fmt); break
        for bsel in ['button[type="submit"]','button:has-text("Search")']:
            b=page.locator(bsel).first
            if b.count()>0: b.click(); break
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(3500)

        # Parse the results table headers + NTS rows
        soup = BeautifulSoup(page.content(),'lxml')
        found = 0
        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
            if 'grantor' not in headers: continue
            h = {v:i for i,v in enumerate(headers)}
            log.info(f"[{name}] headers: {headers}")
            gi=h.get('grantor',-1); di=h.get('doc type',-1)
            ai=h.get('property address', h.get('legal description', h.get('town',-1)))
            ni=h.get('doc number', h.get('inst number',-1))
            for tr in table.find_all('tr')[1:]:
                cells=[td.get_text(' ',strip=True) for td in tr.find_all('td')]
                if not cells: continue
                def c(i): return cells[i].strip() if 0<=i<len(cells) else ''
                if not is_nts(c(di)): continue
                found += 1
                log.info(f"[{name}] NTS ROW: grantor='{c(gi)}' | doctype='{c(di)}' | ADDRESS_COL='{c(ai)}' | doc#='{c(ni)}'")
                if found >= 5: break
        if found == 0:
            log.info(f"[{name}] 0 NTS rows in 90-day window")
    except Exception as e:
        log.info(f"[{name}] error: {str(e)[:80]}")
    finally:
        browser.close()

def main():
    with sync_playwright() as pw:
        for slug,name in COUNTIES:
            log.info(f"========== {name} ==========")
            scan_county(pw, slug, name)

if __name__=='__main__':
    main()
