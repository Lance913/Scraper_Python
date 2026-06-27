"""
Probe v9 — INTROSPECT the publicsearch advanced-search form.

Why: v8 proved that (a) filling input[id*="docType"] is a no-op (the results URL
carried no doc-type param — only recordedDateRange), and (b) a date-only search
returns an unstable, ~50-row single page, so real Notice of Trustee Sale docs
only show up by luck. We also learned doc_type 'NOTICE' over-matches (a Water
Code statutory notice came back as NTS).

Goal of v9: discover how publicsearch actually filters by document type so the
scraper can request trustee-sale docs directly. We:
  1. Dump every input/select/button/combobox on /search/advanced (id, name,
     placeholder, aria, role) + all <select> options + visible label text.
  2. Try to interact with the document-type control, type 'TRUSTEE', and capture
     the autocomplete options that appear (the exact NTS doc-type names).
  3. If we can select a doc type, submit and log the resulting results URL (to
     capture the correct query param) + the doc types returned.
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import date, timedelta
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format='%(asctime)s [PS] %(message)s')
log = logging.getLogger()

COUNTY_SLUG = "bexar"
COUNTY_NAME = "Bexar"
BASE = f"https://{COUNTY_SLUG}.tx.publicsearch.us"
TODAY = date(2026, 6, 27)
WINDOW_DAYS = 120

DUMP_JS = """() => {
  const dump = el => ({
    tag: el.tagName,
    type: el.getAttribute('type') || '',
    id: el.id || '',
    name: el.getAttribute('name') || '',
    placeholder: el.getAttribute('placeholder') || '',
    aria: el.getAttribute('aria-label') || '',
    role: el.getAttribute('role') || '',
    text: (el.textContent || '').trim().slice(0, 40),
  });
  const controls = Array.from(document.querySelectorAll(
    'input,select,textarea,button,[role="combobox"],[role="listbox"],[role="button"]'
  )).map(dump);
  const selects = Array.from(document.querySelectorAll('select')).map(s => ({
    id: s.id, name: s.name,
    options: Array.from(s.options).map(o => o.text).slice(0, 60),
  }));
  const labels = Array.from(document.querySelectorAll('label'))
    .map(l => l.textContent.trim()).filter(Boolean).slice(0, 40);
  return { controls, selects, labels };
}"""


def main():
    start_fmt = (TODAY - timedelta(days=WINDOW_DAYS)).strftime('%m/%d/%Y')
    end_fmt = TODAY.strftime('%m/%d/%Y')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=['--disable-blink-features=AutomationControlled']
        )
        ctx = browser.new_context(user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        ))
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page.set_default_timeout(30000)

        log.info(f"Loading {BASE}/search/advanced ...")
        page.goto(BASE); page.wait_for_load_state('networkidle'); page.wait_for_timeout(800)
        page.goto(BASE + '/search/advanced')
        page.wait_for_load_state('networkidle'); page.wait_for_timeout(1500)

        # 1) Dump the whole form.
        info = page.evaluate(DUMP_JS)
        log.info("===== FORM LABELS =====")
        log.info(json.dumps(info['labels'], indent=0))
        log.info("===== SELECT ELEMENTS (id / options) =====")
        for s in info['selects']:
            log.info(f"select id={s['id']!r} name={s['name']!r} options={s['options']}")
        log.info("===== CONTROLS (input/button/combobox) =====")
        for c in info['controls']:
            # skip pure-icon buttons with no useful identity
            if not any([c['id'], c['name'], c['placeholder'], c['aria'], c['role'], c['text']]):
                continue
            log.info(
                f"{c['tag']:8} type={c['type']:10} id={c['id']!r} name={c['name']!r} "
                f"ph={c['placeholder']!r} aria={c['aria']!r} role={c['role']!r} text={c['text']!r}"
            )

        # 2) Try to interact with a document-type control and capture options.
        log.info("===== TRY DOC-TYPE AUTOCOMPLETE ('TRUSTEE') =====")
        doc_type_selectors = [
            'input[id*="docType" i]', 'input[placeholder*="document type" i]',
            'input[placeholder*="doc type" i]', 'input[aria-label*="document type" i]',
            '[role="combobox"]', 'input[placeholder*="Type" i]',
        ]
        for sel in doc_type_selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                log.info(f"interacting with {sel}")
                try:
                    loc.click()
                    loc.fill('TRUSTEE')
                    page.wait_for_timeout(1500)
                    opts = page.evaluate("""() => Array.from(document.querySelectorAll(
                        '[role="option"], [class*="option" i], li'
                    )).map(o => (o.textContent||'').trim()).filter(t => t && t.length < 80).slice(0, 40)""")
                    log.info(f"  autocomplete options after typing TRUSTEE: {opts}")
                except Exception as e:
                    log.info(f"  interact err: {str(e)[:80]}")
                break
        else:
            log.info("no doc-type control found among selectors")

        # 3) Also dump the page text near 'Document Type' to understand layout.
        body_txt = page.evaluate(
            "() => (document.body.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean).slice(0,80)"
        )
        log.info("===== ADVANCED-SEARCH PAGE TEXT (first 80 lines) =====")
        for ln in body_txt:
            log.info(f"  | {ln}")

        browser.close()


if __name__ == '__main__':
    main()
