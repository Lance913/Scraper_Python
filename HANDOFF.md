# PROJECT HANDOFF — TX Pre-Foreclosure Daily Scraper

> **Claude Code: read this whole file first. It is the complete state of the project.**
> It tells you what every file does, what is finished, what is left, the exact plan,
> and the hard constraints. You should not need to ask the user to paste anything else.
> The repository itself is the source of truth — when in doubt, read the actual file.

---

## 1. WHAT THIS PROJECT IS

A daily scraper that pulls **Notice of Trustee Sale (NTS)** filings — i.e. upcoming
foreclosure auctions — from Texas county record portals, extracts the **owner name +
property address**, and writes them to a Google Sheet. The end goal (LATER, not now)
is to feed those addresses into skip-tracing for phone/email enrichment.

The user is a real-estate lead-gen operator. The leads that matter are **distressed
individual homeowners** with an upcoming auction date, NOT builders/developers/HOAs.

### Output format (Google Sheet columns, in this order)
`First Name | Last Name | Address | City | State | Zip Code | County | Foreclosure File Date | Sale Date | Doc ID`

---

## 2. HARD CONSTRAINTS (do not forget these)

1. **Scrapers ONLY run on GitHub Actions, never locally.** The user is in Pakistan;
   the county portals geo-block non-US IPs. GitHub Actions runners are in the US, so
   all real runs happen there. Editing code locally is fine; *running* `python main.py`
   locally will fail/hang. To test, the user triggers a GitHub Actions workflow and
   pastes back the log. (Claude Code removes the file-shuffling friction but NOT this
   "runs happen on GitHub" reality.)

2. **GitHub repo:** `https://github.com/Lance913/Scraper_Python` (private). Local clone
   is the folder this file sits in (`~/Downloads/foreclosure-scraper/`).

3. **Google Sheet ID:** `1_GocjgjF09KlDT_rURnLNufk-rDU-sG8FOEnHOZHRr8`
   (hard-coded in `sheets_writer.py`). Credentials come from the `GOOGLE_CREDENTIALS`
   GitHub Actions secret — already configured.

4. **OCR on scanned county docs tops out around 70–80% clean extraction.** This is the
   real ceiling, not a bug to fix. Some docs are degraded scans or put the address only
   on an "Exhibit A" legal description (Lot/Block, no street). Tiered result is expected:
   some records get full name+address, some get one, some get only the doc_id. That is
   acceptable — skip-trace works on either a name or an address.

5. **Don't over-engineer / don't burn the user's time.** This project took ~18 hours and
   60+ test runs largely due to a slow zip→deploy→paste-log loop. Move efficiently. Make
   real edits in the repo and commit; don't ask the user to shuffle files.

---

## 3. FILE-BY-FILE MAP

```
~/Downloads/foreclosure-scraper/
├── main.py                      # CLI entry. ALL_COUNTIES list + SCRAPER_MAP. Add new counties here.
├── requirements.txt             # deps incl. OCR (pdf2image, pytesseract, pypdf)
├── README.md
├── HANDOFF.md                   # ← this file
│
├── .github/workflows/
│   ├── daily_scrape.yml         # PRODUCTION cron, 7am CST. Installs tesseract+poppler, 60-min timeout.
│   ├── probe.yml                # temp Harris probe workflow — safe to delete
│   └── probe_ps.yml             # temp publicsearch probe workflow — safe to delete
│
├── scrapers/
│   ├── __init__.py              # exports the scraper classes
│   ├── base.py                  # BaseScraper: parse_name(), parse_address(), build_record() (record schema incl. doc_id)
│   ├── counties.py              # thin per-county wrapper classes (one line each). Add new counties here.
│   ├── harris.py                # ✅ DONE — Harris scraper, all-upcoming-months, downloads PDFs, calls harris_extract
│   ├── harris_extract.py        # ✅ DONE — OCR+parse engine. THE REFERENCE IMPLEMENTATION. Reuse its patterns.
│   └── publicsearch.py          # 🔧 IN PROGRESS — Bexar/Dallas/Tarrant/Denton/Johnson. Needs OCR injected.
│
├── sheets_writer.py             # ✅ DONE — gspread writer, column order, doc_id dedup, auto-creates header
│
├── probe_harris_doc.py          # scratch probe (Harris) — done, safe to delete
└── probe_publicsearch.py        # scratch probe (publicsearch) — current investigation file
```

---

## 4. WHAT IS DONE ✅

### Harris County — fully working, in production
- `scrapers/harris.py` + `scrapers/harris_extract.py`.
- Portal: `https://www.cclerk.hctx.net/applications/websearch/FRCL_R.aspx`
- Scrapes **all upcoming auction months** (current month through current+4), filters to
  sale_date >= today. (Earlier versions wrongly used fixed offsets and returned past
  auctions — that's fixed; it now pulls every upcoming month.)
- Documents: the doc-ID link is a **relative** href `ViewECdocs.aspx?ID=...` that resolves
  against `https://www.cclerk.hctx.net/applications/websearch/` and **streams a scanned PDF
  download** (no text layer). Fetch via the in-session Playwright request API:
  `ctx.request.get(APP_BASE + href).body()`.
- Pipeline: download PDF → OCR all 3 pages (pdf2image + pytesseract) → parse owner name +
  property address → fall back to doc_id if unreadable.
- Parser highlights (in `harris_extract.py`): anchors on labeled fields ("Property Address:",
  "Grantor(s)", "Trustor(s)", "executed by"), a header address block, and **rejects the
  auction-venue address** (Bayou City Event Center / 9401 Knight Road / Houston 77045) and
  **rejects out-of-area ZIPs** (Dallas/Plano/Addison 75xxx, El Paso 79xxx, etc.) which are
  law-firm/servicer mailing addresses, not the property. Harris-area ZIPs = 770xx–777xx.
- **Verified live:** writes to the sheet, columns line up, dedup confirmed (second run =
  "All records were duplicates — nothing written"). Typical run: ~116 upcoming records,
  ~30 full name+address, ~25 name-or-address, rest doc_id-only.

### sheets_writer.py — done
- Column order as in §1. Dedup key priority: (1) `docid|county|doc_id` if doc_id present,
  (2) `first|last|county|file_date|address` if address, (3) `first|last|county|file_date`.
- Auto-writes the header row if missing. Clearing the whole sheet (incl. header) is safe —
  it rebuilds the header on next run.

### daily_scrape.yml — done
- Cron `0 13 * * *` (7am CST). Installs `tesseract-ocr` + `poppler-utils`, pip-installs
  requirements, caches Playwright Chromium, 60-min timeout (OCR is slow: ~1.2s/page,
  15–25 min for a full Harris run). Supports manual dispatch with date/counties/dry_run inputs.

---

## 5. WHAT IS LEFT 🔧  (this is the active work — do these in order)

### TASK A — Wire OCR into the 5 publicsearch counties
Counties: **Bexar, Dallas, Tarrant, Denton, Johnson** — all on the same `publicsearch.us`
platform, all handled by `scrapers/publicsearch.py`. One fix covers all five.

**What we already proved about publicsearch (don't re-investigate):**
- The scraper already finds NTS rows correctly and clicks the correct doc number cell,
  landing on a doc page at `https://{slug}.tx.publicsearch.us/doc/{docId}`.
- That page shows a cosmetic "Your web browser is out of date" React wall — but the actual
  document loads underneath as **PNG page images** at:
  `https://{slug}.tx.publicsearch.us/files/documents/{docId}/images/{imageId}_{page}.png?exp=...&sig=...`
  (session-signed URL). Capture it by listening to network responses, then fetch via
  `ctx.request.get(url).body()` (carries the session cookies + signature). Header is
  `\x89PNG`. OCR quality on these PNGs is **excellent** — cleaner than Harris.
- The results-table "Property Address" column is **NOT usable** for NTS rows — it's `N/A`
  or a legal description (Lot/Block). So the address MUST come from OCR of the document.

**Where to inject:** `scrapers/publicsearch.py`, method `_fetch_sale_date()`, around the
block (~line 265) where it clicks the doc cell and lands on the doc page. Right now that
block reads `BeautifulSoup(page.content()).get_text()` — which only gets the "out of date"
wall, so sale_date and address usually come back empty. Replace/augment with:
  1. Before clicking, attach a response listener that captures `/files/documents/.../images/*.png` URLs.
  2. After landing on the doc page (and waiting for images to load; may need to click
     "Next in Book" to force multipage image loads), fetch each captured PNG via
     `ctx.request.get()`, save bytes.
  3. OCR each page (pytesseract + PIL.Image — note: PNG, so use `Image.open()`, not
     pdf2image). Combine page text.
  4. Parse **sale date**, **owner name**, **property address** from the OCR text.
  5. Fall back to doc_id (already in the schema) if unreadable.

**Parser:** Reuse the approach in `harris_extract.py` but tune for the publicsearch NTS
layout — you MUST OCR a real publicsearch NTS doc first to see its format (the existing
`probe_publicsearch.py` is set up for this; the latest attempt accidentally grabbed a UCC
filing because the target NTS wasn't on results page 1 — make the probe click the row the
scraper already identified as NTS, not the first doc-number cell). publicsearch ZIP gate
should be per-county metro, not Harris's 77xxx (Bexar→78xxx San Antonio, Dallas/Tarrant/
Denton→75xxx DFW, Johnson→76xxx). Reject the servicer/law-firm and "venue" addresses the
same way Harris does.

**Reality check to set with the user:** these 5 counties have very low *individual*-homeowner
NTS volume — most NTS filings there are builders/HOAs/funds (D R Horton, Lennar, Beazer,
LGI, "Villas at Town Center HOA", "Purchasing Fund 2025-1 LLC") which `is_residential_lead()`
in `publicsearch.py` already filters out. Across a 90-day window all five counties combined
yielded ~1 individual lead. The OCR wiring is still worth doing for completeness and to catch
volume when it appears, but don't expect big numbers here. The volume is in Harris and the
new counties (Task B).

### TASK B — Add high-volume Houston-metro counties
The user wants more *homeowner* leads. The Houston suburbs are where they are. Add:
**Fort Bend, Montgomery, Galveston** (and optionally Brazoria).

- First determine each county's portal type. Montgomery & Galveston likely use a
  Harris-style county-clerk ASP.NET site or a publicsearch.us instance — check by visiting
  e.g. `https://{county}.tx.publicsearch.us` and the county clerk's official records search.
- If a county is on **publicsearch.us** → just add a one-line wrapper in
  `scrapers/counties.py` (see existing Bexar/Dallas pattern) and add it to `ALL_COUNTIES`
  + `SCRAPER_MAP` in `main.py`. It inherits everything once Task A is done.
- If a county uses a **different portal** → it needs its own scraper module modeled on
  `harris.py` (search form automation → results table → doc download → OCR via the
  `harris_extract.py` engine).
- Add each new county to: `main.py` (`ALL_COUNTIES`, `SCRAPER_MAP`), `scrapers/counties.py`
  (wrapper or new module import), and the default counties string in
  `.github/workflows/daily_scrape.yml`.

### TASK C — Cleanup
- Delete the temp probe files once OCR is wired: `probe_harris_doc.py`,
  `probe_publicsearch.py`, `.github/workflows/probe.yml`, `.github/workflows/probe_ps.yml`.

### LATER (explicitly deferred by the user — do NOT start unless asked)
- Skip-trace enrichment (phone/email from address). The user mentioned TruePeopleSearch;
  note that TPS has no public API and uses CAPTCHAs/anti-bot, so it's unreliable for
  automation. Recommend a real skip-trace API instead (BatchData, Skip Genie, IDI/LexisNexis,
  or PropStream batch) when this phase starts. Input will be the sheet's
  `Address, City, State, Zip Code`.

---

## 6. HOW TO TEST (the loop)

1. Make edits in the repo, commit, push.
2. Tell the user to run the relevant GitHub Actions workflow:
   - Production/full test: **"Daily TX Foreclosure Scraper"** → Run workflow.
     Use **dry_run = true** for inspection (prints records, writes nothing).
     Use **dry_run = false** to actually write to the sheet.
   - Probe: the temp probe workflows, for investigation.
3. User pastes the Actions log back. Read it, iterate.
4. Remember: a full Harris OCR run is 15–25 min. Don't assume a hang.

### Key technical facts to keep handy
- publicsearch PNG image URL: `https://{slug}.tx.publicsearch.us/files/documents/{docId}/images/{imageId}_{page}.png?exp=...&sig=...` — fetch with `ctx.request.get()` (carries session auth).
- Harris doc PDF: relative `ViewECdocs.aspx?ID=...` resolved against
  `https://www.cclerk.hctx.net/applications/websearch/`, streams a PDF download.
- Playwright launch args used everywhere: `--disable-blink-features=AutomationControlled`
  + init script `Object.defineProperty(navigator,'webdriver',{get:()=>undefined})`.
- publicsearch advanced search: navigate directly to `{base}/search/advanced` (clicking the
  link is intercepted by a tooltip overlay). Fill `input[id*="start" i]` / `input[id*="end" i]`,
  click `button[type="submit"]` or `button:has-text("Search")`.
- After clicking a doc on publicsearch: if URL changed use `page.go_back()`; if a modal
  opened (URL unchanged) press `Escape`.

---

## 7. SUGGESTED FIRST STEPS FOR CLAUDE CODE

1. Read `scrapers/publicsearch.py` fully, then `scrapers/harris_extract.py` fully.
2. Fix `probe_publicsearch.py` to OCR a *real NTS* document (click the NTS row the scraper
   identifies, not the first doc cell), have the user run "Probe PublicSearch Doc Delivery",
   and read the OCR to learn the publicsearch NTS layout.
3. Build a `publicsearch_extract.py` (mirroring `harris_extract.py`) for the NTS layout +
   per-metro ZIP gates.
4. Inject capture+OCR into `_fetch_sale_date()` in `publicsearch.py`.
5. Test via dry-run on GitHub Actions. Then move to Task B (new counties), then Task C (cleanup).

Good luck. The repo is the memory — trust the code over any assumption.
