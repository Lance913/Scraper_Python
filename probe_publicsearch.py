"""
Probe publicsearch.us (Bexar) document delivery.

The detail pages show "browser out of date" — but is there a downloadable
document behind it (like Harris's ViewECdocs PDF)? We investigate:
  1. Reach a doc detail page (/doc/{id})
  2. Inspect the page for download links, iframes, embeds, PDF endpoints
  3. Watch network for any PDF/image/octet-stream responses
  4. Look for a "download" / "view image" button and what URL it hits
  5. Try common publicsearch doc-image API patterns
"""
import re, logging
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

SLUG = "bexar"
BASE = f"https://{SLUG}.tx.publicsearch.us"


def main():
    net = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.set_default_timeout(30000)
        page.on('response', lambda r: net.append((r.status, r.headers.get('content-type',''), r.url)))

        # Go straight to advanced search, do a broad search to get any doc
        log.info("Loading advanced search...")
        page.goto(f"{BASE}/search/advanced", wait_until='domcontentloaded')
        page.wait_for_timeout(3000)

        # Fill a date range search to surface documents (any type)
        try:
            # Find date inputs
            start = page.query_selector('input[id*="start" i], input[name*="start" i], input[placeholder*="rom" i]')
            end   = page.query_selector('input[id*="end" i], input[name*="end" i], input[placeholder*="o" i]')
            if start: start.fill('05/01/2026')
            if end: end.fill('06/27/2026')
            log.info("Filled date range")
        except Exception as e:
            log.info(f"date fill: {e}")

        # Submit search (Enter or button)
        try:
            page.keyboard.press('Enter')
            page.wait_for_timeout(4000)
        except Exception as e:
            log.info(f"submit: {e}")

        log.info(f"After search URL: {page.url}")

        # Find first result row link to a /doc/
        doc_url = page.evaluate(f"""() => {{
            var as = Array.from(document.querySelectorAll('a'));
            var d = as.filter(a => /\\/doc\\//.test(a.getAttribute('href')||''));
            return d.length ? d[0].getAttribute('href') : null;
        }}""")
        log.info(f"First doc href: {doc_url}")

        if not doc_url:
            # Try clicking a result row instead
            log.info("No direct doc link; trying to click first result row...")
            try:
                rows = page.query_selector_all('tr, [role="row"], .result-row, td')
                log.info(f"Found {len(rows)} clickable row-ish elements")
                if rows:
                    net.clear()
                    rows[1].click() if len(rows)>1 else rows[0].click()
                    page.wait_for_timeout(4000)
                    log.info(f"After row click URL: {page.url}")
            except Exception as e:
                log.info(f"row click: {e}")
        else:
            full = doc_url if doc_url.startswith('http') else BASE + doc_url
            log.info(f"Navigating to doc: {full}")
            net.clear()
            page.goto(full, wait_until='domcontentloaded')
            page.wait_for_timeout(5000)
            log.info(f"Doc page URL: {page.url}")

        # Inspect the doc page structure
        struct = page.evaluate("""() => ({
            title: document.title,
            iframes: Array.from(document.querySelectorAll('iframe')).map(f=>f.src),
            embeds: Array.from(document.querySelectorAll('embed')).map(e=>e.src),
            objects: Array.from(document.querySelectorAll('object')).map(o=>o.data),
            imgs: Array.from(document.querySelectorAll('img')).map(i=>i.src).filter(s=>s&&!s.includes('icon')&&!s.includes('logo')).slice(0,8),
            download_links: Array.from(document.querySelectorAll('a')).map(a=>({t:(a.textContent||'').trim().slice(0,30),h:a.getAttribute('href')})).filter(x=>/download|view|image|pdf|doc/i.test(x.t)||/download|\\.pdf|image/i.test(x.h||'')).slice(0,10),
            buttons: Array.from(document.querySelectorAll('button')).map(b=>(b.textContent||'').trim().slice(0,30)).filter(t=>t).slice(0,15),
            bodytext: (document.body.innerText||'').slice(0,200)
        })""")
        log.info(f"DOC PAGE STRUCTURE:")
        for k, v in struct.items():
            log.info(f"  {k}: {v}")

        log.info("=== Network: document-like (pdf/image/octet) ===")
        for s, ct, url in net:
            if any(k in ct.lower() for k in ['pdf','image','tiff','octet']) or \
               any(k in url.lower() for k in ['.pdf','/image','/download','docimage','/api/']):
                log.info(f"  [{s}] {ct[:35]:35} {url[:130]}")

        log.info("=== Network: API calls (json, may reveal doc endpoints) ===")
        for s, ct, url in net:
            if 'json' in ct.lower() or '/api/' in url.lower():
                log.info(f"  [{s}] {ct[:30]:30} {url[:130]}")

        browser.close()

if __name__ == '__main__':
    main()
