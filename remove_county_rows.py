"""Remove every row of the given counties from the master tab (used to redo a county after a reader fix).
   python remove_county_rows.py "Fort Bend" Comal Williamson"""
import sys
import sheets_writer as sw

counties = {c.strip().lower() for c in sys.argv[1:] if c.strip()}
ws = sw._retry(lambda: sw._get_client().open_by_key(sw.SPREADSHEET_ID).sheet1)
rows = sw._retry(lambda: ws.get_all_values())
ci = rows[0].index('County')
keep = [rows[0]] + [r for r in rows[1:] if (r[ci] if ci < len(r) else '').strip().lower() not in counties]
removed = len(rows) - len(keep)
width = max(len(r) for r in rows)
keep = [r + [''] * (width - len(r)) for r in keep]
sw._retry(lambda: ws.clear())
sw._retry(lambda: ws.update('A1', keep, value_input_option='USER_ENTERED'))
print(f'::notice::Removed {removed} rows for {sorted(counties)}; {len(keep) - 1} rows left', flush=True)
