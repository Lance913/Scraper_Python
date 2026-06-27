"""Probe v8 — run NEW parser on 10 real docs, report hit rate + show records."""
import sys, logging
sys.path.insert(0,'scrapers')
from playwright.sync_api import sync_playwright
from harris_extract import extract_from_pdf_bytes
logging.basicConfig(level=logging.INFO, format='%(asctime)s [P] %(message)s')
log=logging.getLogger()
SEARCH_URL="https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
APP_BASE="https://www.cclerk.hctx.net/applications/websearch/"
Y='ctl00$ContentPlaceHolder1$ddlYear';M='ctl00$ContentPlaceHolder1$ddlMonth';S='ctl00$ContentPlaceHolder1$btnSearch'

def main():
    with sync_playwright() as pw:
        b=pw.chromium.launch(headless=True,args=['--disable-blink-features=AutomationControlled'])
        ctx=b.new_context(accept_downloads=True);page=ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.goto(SEARCH_URL);page.wait_for_load_state('networkidle');page.wait_for_timeout(1000)
        page.evaluate(f"""()=>{{var s=document.querySelector('select[name="{Y}"]');if(s){{s.value='2026';s.dispatchEvent(new Event('change',{{bubbles:true}}));}}}}""")
        page.wait_for_load_state('networkidle');page.wait_for_timeout(800)
        page.evaluate(f"""()=>{{var s=document.querySelector('select[name="{M}"]');if(s){{s.value='7';s.dispatchEvent(new Event('change',{{bubbles:true}}));}}}}""")
        page.wait_for_timeout(300)
        try:
            with page.expect_navigation(wait_until='networkidle',timeout=20000):
                page.click(f'input[name="{S}"]')
        except Exception as e: log.warning(e)
        page.wait_for_timeout(2500)
        links=page.evaluate("""()=>Array.from(document.querySelectorAll('a')).filter(x=>/FRCL/.test(x.textContent||'')).map(a=>({id:a.textContent.trim(),href:a.getAttribute('href')}));""")
        log.info(f"Running NEW parser on 10 of {len(links)} docs")
        full=part=none=0
        for lk in links[:10]:
            try:
                body=ctx.request.get(APP_BASE+lk['href']).body()
                r=extract_from_pdf_bytes(body)
                nm=f"{r['first_name']} {r['last_name']}".strip()
                ad=f"{r['address']}, {r['city']}, {r['state']} {r['zip_code']}".strip(' ,')
                hn,ha=bool(r['first_name']),bool(r['address'])
                t="FULL" if(hn and ha)else("PART" if(hn or ha)else"NONE")
                if t=="FULL":full+=1
                elif t=="PART":part+=1
                else:none+=1
                log.info(f"  [{lk['id']}] {t}: {nm!r} | {ad!r}")
            except Exception as e:
                log.info(f"  [{lk['id']}] ERR {str(e)[:60]}")
        log.info(f"=== {full} FULL, {part} PARTIAL, {none} NONE (of 10) ===")
        log.info(f"=== Usable (name OR addr): {full+part}/10 ===")
        b.close()
if __name__=='__main__': main()
