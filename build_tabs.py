"""Rebuild the summary tabs of the Data Extraction sheet from the master 'Foreclosures' tab.

  Daily Counts      - one row per pull date, new rows per county (all 13 counties) + Total.
  Ready to Skip Trace - today's new properties (upcoming sale, real address, not multi-family,
                        not the auction venue, not already sent on an earlier day), ready for DealMachine.

Runs after every scraper writes (Scraper_Python collate job and the Collin/Ellis/Travis job).
"""
import re
from collections import defaultdict
from datetime import datetime, date

import gspread
import sheets_writer as sw
from filters import is_multifamily

COUNTIES = ['Harris', 'Montgomery', 'Fort Bend', 'Williamson', 'Travis', 'Bexar', 'Comal',
            'Dallas', 'Tarrant', 'Denton', 'Johnson', 'Collin', 'Ellis']
READY_TAB = 'Ready to Skip Trace'
READY_HEADERS = ['Property Address', 'City', 'State', 'Zip', 'County', 'Owner First Name', 'Owner Last Name',
                 'Sale Date', 'Foreclosure File Date', 'Doc ID', 'Date Pulled']
JUNK = re.compile(r'9401\s+KNIGHT|BAYOU\s+CITY|EVENT\s+CENTER|COURTHOUSE|P\.?\s?O\.?\s*BOX|HEIMANN|PATTON\s+HALL', re.I)


def _d(s):
    for f in ('%m/%d/%Y', '%Y-%m-%d', '%m/%d/%y'):
        try: return datetime.strptime(str(s).strip(), f).date()
        except ValueError: pass
    return None


def _county(s):
    s = re.sub(r'\s+county$', '', str(s).strip(), flags=re.I)
    s = {'fortbend': 'Fort Bend', 'fort bend': 'Fort Bend'}.get(s.lower(), s.title())
    return s


def _norm_addr(a):
    return re.sub(r'[^A-Z0-9]', '', str(a).upper())


def main(today: date = None):
    today = today or date.today()
    ss = sw._retry(lambda: sw._get_client().open_by_key(sw.SPREADSHEET_ID))
    rows = sw._retry(lambda: ss.sheet1.get_all_values())
    hdr, body = rows[0], rows[1:]
    ix = {h: i for i, h in enumerate(hdr)}
    g = lambda r, h: r[ix[h]].strip() if h in ix and ix[h] < len(r) else ''

    # ---- Daily Counts ----
    per = defaultdict(lambda: defaultdict(int))
    for r in body:
        pd = _d(g(r, 'Date Pulled'))
        if pd: per[pd][_county(g(r, 'County'))] += 1
    out = [['Date'] + COUNTIES + ['Total']]
    for pd in sorted(per, reverse=True):
        c = [per[pd].get(k, 0) for k in COUNTIES]
        out.append([pd.strftime('%m/%d/%Y')] + c + [sum(per[pd].values())])
    _replace_tab(ss, sw.TRACKER_TAB, out)

    # ---- Ready to Skip Trace: properties first pulled on the latest pull date, upcoming sale ----
    first_seen = {}
    for r in body:
        a = _norm_addr(g(r, 'Address'))
        pd = _d(g(r, 'Date Pulled'))
        if a and pd and (a not in first_seen or pd < first_seen[a]): first_seen[a] = pd
    latest = max((d for d in per if d <= today), default=None)
    ready, seen = [], set()
    for r in body:
        addr, a = g(r, 'Address'), _norm_addr(g(r, 'Address'))
        if not a or a in seen or _d(g(r, 'Date Pulled')) != latest or first_seen.get(a) != latest: continue
        sd = _d(g(r, 'Sale Date'))
        if sd and sd < today: continue
        if JUNK.search(addr) or is_multifamily(addr, g(r, 'City')) or not re.match(r'^\d', addr): continue
        seen.add(a)
        ready.append([addr, g(r, 'City'), g(r, 'State') or 'TX', g(r, 'Zip Code'), _county(g(r, 'County')),
                      g(r, 'First Name'), g(r, 'Last Name'), g(r, 'Sale Date'), g(r, 'Foreclosure File Date'),
                      g(r, 'Doc ID'), g(r, 'Date Pulled')])
    ready.sort(key=lambda x: (x[4], _d(x[7]) or date.max))
    _replace_tab(ss, READY_TAB, [READY_HEADERS] + ready, index=1)
    print(f'::notice::Ready to Skip Trace: {len(ready)} properties (pulled {latest}). '
          f'Daily Counts: {len(out) - 1} days', flush=True)


def _replace_tab(ss, title, values, index=None):
    try:
        ws = sw._retry(lambda: ss.worksheet(title))
        sw._retry(lambda: ws.clear())
    except gspread.WorksheetNotFound:
        ws = sw._retry(lambda: ss.add_worksheet(title=title, rows=max(100, len(values) + 10),
                                                cols=len(values[0]), index=index))
    if ws.col_count < len(values[0]):
        sw._retry(lambda: ws.add_cols(len(values[0]) - ws.col_count))
    if ws.row_count < len(values):
        sw._retry(lambda: ws.add_rows(len(values) - ws.row_count + 10))
    sw._retry(lambda: ws.update('A1', values, value_input_option='USER_ENTERED'))
    sw._retry(lambda: ws.freeze(rows=1))


if __name__ == '__main__':
    main()
