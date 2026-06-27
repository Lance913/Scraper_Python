"""
Diagnostic probe — runs INSIDE GitHub Actions where Harris is reachable.
Determines how the foreclosure document is actually served so we can OCR it.

Run via: python probe_harris_doc.py
"""
import re
import logging
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PROBE] %(message)s')
log = logging.getLogger()

SEARCH_URL  = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
YEAR_NAME   = 'ctl00$ContentPlaceHolder1$ddlYear'
MONTH_NAME  = 'ctl00$ContentPlaceHolder1$ddlMonth'
SEARCH_NAME = 'ctl00$ContentPlaceHolder1$btnSearch'


def main():
    net = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: net.append((r.status, r.headers.get('content-type',''), r.url)))

        log.info("Loading portal...")
        page.goto(SEARCH_URL)
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(1000)

        page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{YEAR_NAME}"]');
            if(s){{s.value='2026';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.evaluate(f"""() => {{ var s=document.querySelector('select[name="{MONTH_NAME}"]');
            if(s){{s.value='7';s.dispatchEvent(new Event('change',{{bubbles:true}}));}} }}""")
        page.wait_for_timeout(300)
        try:
            with page.expect_navigation(wait_until='networkidle', timeout=20000):
                page.click(f'input[name="{SEARCH_NAME}"]')
        except Exception as e:
            log.warning(f"nav: {e}")
        page.wait_for_timeout(2500)
        log.info(f"Results URL: {page.url}")

        # Inspect the doc link
        link = page.evaluate("""() => {
            var links = Array.from(document.querySelectorAll('a'));
            var f = links.filter(a => /FRCL/.test(a.textContent||''));
            if(!f.length) return {found:false, total: links.length};
            var a=f[0];
            return {found:true, text:a.textContent.trim(), href:a.getAttribute('href'),
                    onclick:a.getAttribute('onclick'), target:a.getAttribute('target'),
                    html:a.outerHTML.slice(0,500)};
        }""")
        log.info(f"LINK STRUCTURE: {link}")

        net.clear()
        log.info("Clicking doc ID...")
        popup = None
        try:
            with ctx.expect_page(timeout=8000) as pi:
                page.evaluate("""() => { var links=Array.from(document.querySelectorAll('a'));
                    var f=links.filter(a=>/FRCL/.test(a.textContent||'')); if(f.length)f[0].click(); }""")
            popup = pi.value
            log.info(">>> POPUP OPENED")
        except Exception as e:
            log.info(f"no popup: {str(e)[:80]}")
        page.wait_for_timeout(4000)

        if popup:
            try:
                popup.wait_for_load_state('domcontentloaded', timeout=10000)
                log.info(f"POPUP URL: {popup.url}")
                # what's IN the popup? iframe? embed? img?
                struct = popup.evaluate("""() => ({
                    iframes: Array.from(document.querySelectorAll('iframe')).map(f=>f.src),
                    embeds: Array.from(document.querySelectorAll('embed')).map(e=>e.src),
                    objects: Array.from(document.querySelectorAll('object')).map(o=>o.data),
                    imgs: Array.from(document.querySelectorAll('img')).map(i=>i.src).slice(0,5),
                    bodytext: (document.body.innerText||'').slice(0,200)
                })""")
                log.info(f"POPUP STRUCTURE: {struct}")
            except Exception as e:
                log.info(f"popup read err: {e}")

        log.info("=== Network: document-like responses ===")
        for s, ct, url in net:
            if any(k in ct.lower() for k in ['image','pdf','tiff','octet']) or \
               any(k in url.lower() for k in ['viewec','image','tiff','pdf','getdoc','docview']):
                log.info(f"  [{s}] {ct[:35]} | {url[:150]}")

        log.info("=== Network: all (first 30) ===")
        for s, ct, url in net[:30]:
            log.info(f"  [{s}] {ct[:25]:25} {url[:120]}")

        browser.close()

if __name__ == '__main__':
    main()
