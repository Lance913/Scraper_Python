"""
Probe v2 — use the PROVEN scraper navigation to actually reach a Bexar doc page,
then hunt for the download / view-image control that serves the real document.
"""
import re, logging
from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

SLUG = "bexar"
BASE = f"https://{SLUG}.tx.publicsearch.us"


def main():
    net = []
    # Wider date range to surface ANY document (not just NTS) so we reach a doc page
    start_fmt = (date(2026,6,27) - timedelta(days=30)).strftime('%m/%d/%Y')
    end_fmt   = date(2026,6,27).strftime('%m/%d/%Y')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'))
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: net.append((r.status, r.headers.get('content-type',''), r.url)))

        log.info("Loading base then advanced search...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)
        page.goto(BASE + '/search/advanced'); page.wait_for_load_state('networkidle'); page.wait_for_timeout(1000)

        # Fill date range (proven selectors)
        filled = False
        for s, e in [('input[id*="start" i]','input[id*="end" i]'),
                     ('input[placeholder*="Start" i]','input[placeholder*="End" i]')]:
            try:
                if page.locator(s).count() > 0 and page.locator(e).count() > 0:
                    page.fill(s, start_fmt); page.fill(e, end_fmt)
                    log.info(f"date range {start_fmt}→{end_fmt}"); filled=True; break
            except Exception: pass
        log.info(f"date filled: {filled}")

        # Click Search
        for btn_sel in ['button[type="submit"]','button:has-text("Search")']:
            try:
                btn = page.locator(btn_sel).first
                if btn.count() > 0:
                    btn.click(); break
            except Exception: pass
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(3500)
        log.info(f"After search URL: {page.url}")

        # Find a doc number cell and click it (proven approach)
        net.clear()
        clicked = False
        # Grab first doc-number-looking cell
        cells = page.evaluate("""() => {
            var tds = Array.from(document.querySelectorAll('td'));
            return tds.map(t=>(t.textContent||'').trim()).filter(t=>/^\\d{6,}$/.test(t)).slice(0,5);
        }""")
        log.info(f"Doc-number cells found: {cells}")
        if cells:
            try:
                el = page.locator(f'td:has-text("{cells[0]}")').first
                results_url = page.url
                el.scroll_into_view_if_needed(); el.click()
                page.wait_for_load_state('networkidle'); page.wait_for_timeout(5000)
                log.info(f"After doc click URL: {page.url}")
                clicked = True
            except Exception as e:
                log.info(f"click err: {e}")

        # Now inspect the doc page for the DOWNLOAD / VIEW IMAGE control
        struct = page.evaluate("""() => {
            var allBtns = Array.from(document.querySelectorAll('button,a')).map(b=>({
                tag:b.tagName, t:(b.textContent||'').trim().slice(0,40),
                h:b.getAttribute('href'), cls:(b.className||'').slice(0,40)
            })).filter(x=>x.t || x.h);
            return {
                title: document.title,
                url: location.href,
                iframes: Array.from(document.querySelectorAll('iframe')).map(f=>f.src),
                embeds: Array.from(document.querySelectorAll('embed')).map(e=>e.src),
                // anything mentioning download/view/image/pdf
                relevant: allBtns.filter(x=>/download|view image|view document|pdf|image|print|save/i.test(x.t)||/download|\\.pdf|image/i.test(x.h||'')),
                all_buttons: allBtns.filter(x=>x.tag==='BUTTON').slice(0,20),
                bodytext: (document.body.innerText||'').slice(0,250)
            };
        }""")
        log.info("=== DOC PAGE STRUCTURE ===")
        for k,v in struct.items():
            log.info(f"  {k}: {v}")

        # If there's a download/view-image button, click it and watch network
        log.info("=== Trying to click a download/view-image control ===")
        for label in ['Download','View Image','View Document','Print','Image','Save']:
            try:
                b = page.locator(f'button:has-text("{label}"), a:has-text("{label}")').first
                if b.count() > 0 and b.is_visible():
                    log.info(f"Clicking '{label}'...")
                    net.clear()
                    try:
                        with page.expect_download(timeout=8000) as dl:
                            b.click()
                        d = dl.value
                        log.info(f">>> DOWNLOAD: {d.suggested_filename}")
                        p = "/tmp/ps_doc"; d.save_as(p)
                        import os
                        with open(p,'rb') as f: hdr=f.read(8)
                        log.info(f">>> size={os.path.getsize(p)}, header={hdr}")
                    except Exception as ed:
                        log.info(f"no download event ({str(ed)[:50]}); checking network/new tab")
                        page.wait_for_timeout(3000)
                        for s,ct,url in net:
                            if any(k in ct.lower() for k in ['pdf','image','tiff','octet']):
                                log.info(f"  NET [{s}] {ct[:30]} {url[:120]}")
                    break
            except Exception as e:
                log.info(f"  {label}: {str(e)[:50]}")

        log.info("=== All doc-like network responses ===")
        for s,ct,url in net:
            if any(k in ct.lower() for k in ['pdf','image','tiff','octet']) or \
               any(k in url.lower() for k in ['.pdf','/image','/download','docimage','/api/v','/documents/']):
                log.info(f"  [{s}] {ct[:30]:30} {url[:130]}")

        browser.close()

if __name__ == '__main__':
    main()
