# TX Pre-Foreclosure Daily Scraper

Pulls **Notice of Trustee Sale (NTS)** filings from four Texas county clerk portals daily and appends them to a Google Sheet.

| County  | Portal Used |
|---------|-------------|
| Harris (Houston)  | cclerk.hctx.net |
| Bexar (San Antonio) | bexar.tx.publicsearch.us |
| Dallas | dallas.tx.publicsearch.us |
| Tarrant (Fort Worth) | tarrant.tx.publicsearch.us |

**Fields captured:** First Name · Last Name · Address · City · State · Zip · County · Foreclosure File Date · Sale Date

---

## One-Time Setup (20 min)

### Step 1 — Fork / clone this repo to your GitHub

Push this entire folder as a new private GitHub repository.

---

### Step 2 — Enable Google Sheets API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Search **"Google Sheets API"** → Enable
4. Search **"Google Drive API"** → Enable

---

### Step 3 — Create a Service Account

1. In GCP → **IAM & Admin → Service Accounts → Create Service Account**
2. Give it any name (e.g. `foreclosure-bot`)
3. Click **Done** — no extra roles needed
4. Click the new service account → **Keys tab → Add Key → JSON**
5. Download the JSON file — keep it safe

---

### Step 4 — Share the Google Sheet with the Service Account

1. Open the JSON file you downloaded
2. Copy the `client_email` value (looks like `foreclosure-bot@your-project.iam.gserviceaccount.com`)
3. Open your Google Sheet → **Share** → paste that email → set to **Editor**

---

### Step 5 — Add the Secret to GitHub

1. In your GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `GOOGLE_CREDENTIALS`
3. Value: paste the **entire contents** of the JSON key file (the whole `{...}` block)
4. Save

---

### Step 6 — Test it manually

In GitHub → **Actions tab → Daily TX Foreclosure Scraper → Run workflow**

- Leave fields blank for "today"  
- Set `dry_run = true` on first test to verify scraping without writing  
- Check the run logs for output

---

## Schedule

Runs automatically every day at **7:00 AM CST** (13:00 UTC).  
During CDT (March–November), this fires at 8:00 AM local time.  

To change the time, edit `.github/workflows/daily_scrape.yml`:
```yaml
schedule:
  - cron: '0 13 * * *'   # change 13 to your preferred UTC hour
```

---

## Manual / Backfill Run

From the GitHub Actions UI → **Run workflow** with:
- `date`: e.g. `2026-06-24` to backfill a specific day
- `counties`: `harris bexar` to run only specific counties
- `dry_run`: `true` to preview without writing

Or locally:
```bash
pip install -r requirements.txt
export GOOGLE_CREDENTIALS='{ ... paste JSON ... }'
python main.py                      # today
python main.py --date 2026-06-24    # specific date
python main.py --dry-run            # no Sheets write
python main.py --counties harris    # one county only
```

---

## Notes on Data

- **TX auctions are the first Tuesday of each month.** New NTS filings come in throughout the month (21+ days before the sale), not all at once.
- **Names** come from the Deed of Trust grantor field. Individual owners show as `JOHN DOE`. Entities show as `DOE LLC` — last name will be the entity name.
- **Duplicates** are filtered by Address + County + Sale Date before writing.
- If a run finds zero records, that's normal — not every day has new filings.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GOOGLE_CREDENTIALS not set` | Check GitHub Secret name matches exactly |
| `403 Forbidden` from Sheets | Re-share the sheet with the service account email |
| `0 records` on a weekday | County portals may update overnight; try `--date yesterday` |
| Harris scraper fails | The ASP.NET portal may have changed its ViewState field names — check logs and update `scrapers/harris.py` |
| publicsearch.us returns 404 | API path changed; update `PublicSearchScraper._api_search()` endpoint URL |

---

## File Structure

```
foreclosure-scraper/
├── .github/workflows/daily_scrape.yml   # GitHub Actions cron
├── scrapers/
│   ├── __init__.py
│   ├── base.py          # Shared utilities (name parsing, HTTP, etc.)
│   ├── harris.py        # Harris County (ASP.NET WebForms portal)
│   ├── publicsearch.py  # Shared class for publicsearch.us counties
│   └── counties.py      # Bexar, Dallas, Tarrant thin wrappers
├── main.py              # CLI orchestrator
├── sheets_writer.py     # Google Sheets integration
├── requirements.txt
└── README.md
```
