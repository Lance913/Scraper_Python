"""
Probe v7 — dump FULL 3-page OCR for the problem docs so I can see exactly
where the PROPERTY address sits vs the AUCTION VENUE address, and where the
real owner name is. Need ground truth to separate them reliably.
"""
import os, sys, logging
sys.path.insert(0,'scrapers')
from playwright.sync_api import sync_playwright
from pdf2image import convert_from_bytes
import pytesseract

logging.basicConfig(level=logging.INFO, format='%(asctime)s [P] %(message)s')
log=logging.getLogger()
SEARCH_URL="https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
APP_BASE="https://www.cclerk.hctx.net/applications/websearch/"
YEAR_NAME='ctl00$ContentPlaceHolder1$ddlYear'; MONTH_NAME='ctl00$ContentPlaceHolder1$ddlMonth'; SEARCH_NAME='ctl00$ContentPlaceHolder1$btnSearch'

def main():
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=True,args=['--disable-blink-features=AutomationControlled'])
        ctx=b.new_context(accept_downloads=True); page=ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.goto(SEARCH_URL); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
        page.evaluate(f"""()=>{{var s=document.querySelector('select[name="{YEAR_NAME}"]');if(s){{s.value='2026';s.dispatchEvent(new Event('change',{{bubbles:true}}));}}}}""")
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.evaluate(f"""()=>{{var s=document.querySelector('select[name="{MONTH_NAME}"]');if(s){{s.value='7';s.dispatchEvent(new Event('change',{{bubbles:true}}));}}}}""")
        page.wait_for_timeout(300)
        try:
            with page.expect_navigation(wait_until='networkidle',timeout=20000):
                page.click(f'input[name="{SEARCH_NAME}"]')
        except Exception as e: log.warning(e)
        page.wait_for_timeout(2500)
        links=page.evaluate("""()=>Array.from(document.querySelectorAll('a')).filter(x=>/FRCL/.test(x.textContent||'')).map(a=>({id:a.textContent.trim(),href:a.getAttribute('href')}));""")

        # Dump full text for 2 docs: the modern one (2406) and 2395 (missing addr)
        targets = [l for l in links if l['id'] in ('FRCL-2026-2406','FRCL-2026-2395','FRCL-2026-2462')]
        for lk in targets:
            body=ctx.request.get(APP_BASE+lk['href']).body()
            log.info(f"########## {lk['id']} — ALL 3 PAGES ##########")
            imgs=convert_from_bytes(body,dpi=200,first_page=1,last_page=3)
            for pi,im in enumerate(imgs,1):
                txt=pytesseract.image_to_string(im)
                log.info(f"---------- PAGE {pi} ----------")
                for ln in txt.split('\n'):
                    if ln.strip(): log.info(f"| {ln.strip()}")
        b.close()

if __name__=='__main__': main()
