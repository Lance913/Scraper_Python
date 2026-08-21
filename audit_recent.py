"""
audit_recent.py — REPORT-FIRST tool to retroactively apply the multi-family
filter and zip backfill to leads already written to the live Google Sheet.

Usage:
  python audit_recent.py                # dry-run report only (DEFAULT, safe)
  python audit_recent.py --days 14      # override the lookback window
  python audit_recent.py --apply        # ACTUALLY delete/update rows —
                                          # only run after reviewing the
                                          # dry-run report above.
  python audit_recent.py --selftest     # offline logic check on fake rows
"""
import argparse
import logging
from datetime import datetime, timedelta

import zip_lookup
from filters import is_multifamily

logger = logging.getLogger('audit_recent')


def _parse_pull_date(s):
    try:
        return datetime.strptime((s or '').strip(), '%m/%d/%Y').date()
    except Exception:
        return None


def load_recent_rows(worksheet, days):
    """Returns [(row_number, row_dict), ...]; row_number is 1-based,
    matching real sheet rows (row 1 = header, row 2 = first data row)."""
    all_values = worksheet.get_all_values()
    if not all_values:
        return []
    headers = all_values[0]
    cutoff = datetime.now().date() - timedelta(days=days)
    out = []
    for i, row in enumerate(all_values[1:], start=2):
        rec = dict(zip(headers, row))
        pd = _parse_pull_date(rec.get('Date Pulled', ''))
        if pd and pd >= cutoff:
            out.append((i, rec))
    return out


def build_report(rows):
    """rows: [(row_number, row_dict), ...]. Returns (to_delete, to_update)."""
    to_delete = []   # (row_number, rec)
    candidates = []  # (row_number, rec, mutable dict for zip_lookup)
    for row_num, rec in rows:
        address, city = rec.get('Address', ''), rec.get('City', '')
        if is_multifamily(address, city):
            to_delete.append((row_num, rec))
            continue
        if address and not rec.get('Zip Code', '').strip():
            candidates.append((row_num, rec, {
                'address': address, 'city': city,
                'state': rec.get('State', 'TX'), 'zip_code': '',
            }))
    zip_lookup.backfill_zip([t for _, _, t in candidates])
    to_update = [(row_num, rec, t['zip_code'])
                 for row_num, rec, t in candidates if t['zip_code']]
    return to_delete, to_update


def print_report(to_delete, to_update, limit=25):
    print("\n=== AUDIT REPORT ===")
    print(f"{len(to_delete)} row(s) would be DELETED (unit/multi-family marker):")
    for row_num, rec in to_delete[:limit]:
        print(f"  row {row_num}: {rec.get('First Name')} {rec.get('Last Name')} | "
              f"{rec.get('Address')!r} | city={rec.get('City')!r} | {rec.get('County')} | "
              f"doc={rec.get('Doc ID')}")
    if len(to_delete) > limit:
        print(f"  ... and {len(to_delete) - limit} more")

    print(f"\n{len(to_update)} row(s) would be UPDATED (zip backfilled):")
    for row_num, rec, new_zip in to_update[:limit]:
        print(f"  row {row_num}: {rec.get('Address')!r}, {rec.get('City')!r} "
              f"-> zip {new_zip!r} (was {rec.get('Zip Code')!r})")
    if len(to_update) > limit:
        print(f"  ... and {len(to_update) - limit} more")


def apply_changes(worksheet, to_delete, to_update):
    import sheets_writer
    zip_col = sheets_writer.COLUMN_HEADERS.index('Zip Code') + 1  # 1-based -> 6
    # 1) Updates first — row numbers don't shift for cell updates.
    for row_num, _rec, new_zip in to_update:
        worksheet.update_cell(row_num, zip_col, new_zip)
    # 2) Deletes LAST, highest row number first, so earlier deletes don't
    #    shift the row numbers of rows still queued for deletion.
    for row_num, _rec in sorted(to_delete, key=lambda x: -x[0]):
        worksheet.delete_rows(row_num)


def _selftest():
    fake_rows = [
        (2, {'First Name': 'John', 'Last Name': 'Doe', 'Address': '9910 Royal Lane',
             'City': 'Unit 902', 'State': 'TX', 'Zip Code': '', 'County': 'Dallas',
             'Doc ID': 'D-1', 'Date Pulled': '08/20/2026'}),
        (3, {'First Name': 'Jane', 'Last Name': 'Roe', 'Address': '456 Oak St',
             'City': 'Dallas', 'State': 'TX', 'Zip Code': '', 'County': 'Dallas',
             'Doc ID': 'D-2', 'Date Pulled': '08/19/2026'}),
    ]
    to_delete, to_update = build_report(fake_rows)
    print_report(to_delete, to_update)
    assert len(to_delete) == 1 and to_delete[0][0] == 2, "row 2 (unit case) should be deleted"
    assert all(row_num == 3 for row_num, _, _ in to_update) or not to_update, \
        "only row 3 could ever be an update candidate"
    print("\nSELFTEST PASSED")


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--days', type=int, default=7)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--selftest', action='store_true')
    args = p.parse_args()

    if args.selftest:
        _selftest()
        return

    import sheets_writer
    client = sheets_writer._get_client()
    worksheet = client.open_by_key(sheets_writer.SPREADSHEET_ID).sheet1

    rows = load_recent_rows(worksheet, args.days)
    logger.info(f"{len(rows)} row(s) in the last {args.days} day(s) (by Date Pulled).")

    to_delete, to_update = build_report(rows)
    print_report(to_delete, to_update)

    if not args.apply:
        print("\nDRY RUN — no changes made. Re-run with --apply to execute.")
        return

    print(f"\nAPPLYING: deleting {len(to_delete)} row(s), updating {len(to_update)} row(s)...")
    apply_changes(worksheet, to_delete, to_update)
    print("Done.")


if __name__ == '__main__':
    main()
