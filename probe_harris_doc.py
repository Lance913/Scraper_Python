"""
Probe v2 — the doc link is RELATIVE and opens target=_blank.
Correct absolute base is the websearch app dir, NOT the domain root.
This time: wait properly, capture popup network + final content.
"""
import re, logging
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PROBE] %(message)s')
log = logging.getLogger()

SEARCH_URL  = "https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx"
APP_BASE    = "https://www.cclerk.hctx.net/applications/websearch/"  # <-- relative resolves here
YEAR_NAME   = 'ctl00$ContentPlaceHolder1$ddlYear'
MONTH_NAME  = 'ctl00$ContentPlaceHolder1$ddlMonth'
SEARCH_NAME = 'ctl00$ContentPlaceHolder1$btnSearch'


def main():
    popup_net = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)

        log.info("Loading portal...")
        page.goto(SEARCH_URL); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
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

        # Grab the relative href and build correct absolute URL
        href = page.evaluate("""() => {
            var a = Array.from(document.querySelectorAll('a')).filter(x=>/FRCL/.test(x.textContent||''))[0];
            return a ? a.getAttribute('href') : null;
        }""")
        abs_url = APP_BASE + href if href and not href.startswith('http') else href
        log.info(f"Relative href: {href[:60]}...")
        log.info(f"Built absolute: {abs_url[:90]}...")

        # Approach A: Open popup by clicking, attach network listener, wait LONG
        log.info("=== Clicking to open popup ===")
        popup = None
        try:
            with ctx.expect_page(timeout=8000) as pi:
                page.evaluate("""() => { var a=Array.from(document.querySelectorAll('a')).filter(x=>/FRCL/.test(x.textContent||''))[0]; if(a)a.click(); }""")
            popup = pi.value
            popup.on('response', lambda r: popup_net.append((r.status, r.headers.get('content-type',''), r.url)))
            log.info(">>> POPUP OPENED, waiting 15s for it to fully render...")
        except Exception as e:
            log.info(f"no popup: {str(e)[:80]}")

        if popup:
            page.wait_for_timeout(15000)  # give the doc viewer time
            try:
                log.info(f"POPUP final URL: {popup.url}")
            except Exception as e:
                log.info(f"url err: {e}")
            try:
                struct = popup.evaluate("""() => ({
                    url: location.href,
                    title: document.title,
                    iframes: Array.from(document.querySelectorAll('iframe')).map(f=>f.src||f.getAttribute('src')),
                    embeds: Array.from(document.querySelectorAll('embed')).map(e=>e.src),
                    objects: Array.from(document.querySelectorAll('object')).map(o=>o.data),
                    imgs: Array.from(document.querySelectorAll('img')).map(i=>i.src).filter(s=>s).slice(0,8),
                    bodytext: (document.body ? document.body.innerText : '').slice(0,250)
                })""")
                log.info(f"POPUP STRUCTURE: {struct}")
            except Exception as e:
                log.info(f"popup struct err: {str(e)[:100]}")

            log.info("=== POPUP network (doc-like) ===")
            for s, ct, url in popup_net:
                if any(k in ct.lower() for k in ['image','pdf','tiff','octet']) or \
                   any(k in url.lower() for k in ['viewec','image','tiff','pdf','getdoc','.jpg','.png','.gif']):
                    log.info(f"  [{s}] {ct[:35]} | {url[:150]}")
            log.info("=== POPUP network (ALL) ===")
            for s, ct, url in popup_net[:25]:
                log.info(f"  [{s}] {ct[:28]:28} {url[:120]}")

        # Approach B: Try fetching the built absolute URL directly in a fresh tab (same context = same cookies)
        log.info("=== Approach B: direct goto on correct absolute URL ===")
        try:
            test = ctx.new_page()
            resp = test.goto(abs_url, wait_until='domcontentloaded', timeout=20000)
            log.info(f"Direct goto status: {resp.status if resp else 'none'}, ct={resp.headers.get('content-type','') if resp else ''}")
            test.wait_for_timeout(3000)
            btext = test.inner_text('body')[:250]
            log.info(f"Direct body: {btext.replace(chr(10),' ')}")
            bstruct = test.evaluate("""() => ({
                iframes: Array.from(document.querySelectorAll('iframe')).map(f=>f.src),
                imgs: Array.from(document.querySelectorAll('img')).map(i=>i.src).slice(0,5)
            })""")
            log.info(f"Direct struct: {bstruct}")
        except Exception as e:
            log.info(f"Direct goto err: {str(e)[:120]}")

        browser.close()

if __name__ == '__main__':
    main()
