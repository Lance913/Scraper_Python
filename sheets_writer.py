"""
Google Sheets Writer

Dedup key priority:
  1. If doc_id present → county + doc_id (unique per filing regardless of name)
  2. Else if address present → first + last + county + file_date + address
  3. Else → first + last + county + file_date

Harris records always have doc_id (FRCL-YYYY-XXXX), so they dedup correctly
even though names/addresses are blank.

Column order: First Name, Last Name, Address, City, State, Zip, County,
              Foreclosure File Date, Sale Date, Doc ID
"""

import os
import json
import logging
from typing import List, Dict

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger('sheets_writer')

SPREADSHEET_ID = '1_GocjgjF09KlDT_rURnLNufk-rDU-sG8FOEnHOZHRr8'

SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
]

COLUMN_HEADERS = [
    'First Name',
    'Last Name',
    'Address',
    'City',
    'State',
    'Zip Code',
    'County',
    'Foreclosure File Date',
    'Sale Date',
    'Doc ID',
    'Date Pulled',
]

# Column index (1-based) of "Foreclosure File Date" — the sheet is sorted on this.
FILE_DATE_COL = COLUMN_HEADERS.index('Foreclosure File Date') + 1  # 8
LAST_COL_LETTER = chr(ord('A') + len(COLUMN_HEADERS) - 1)          # 'K'

# ── Daily tracker tab ───────────────────────────────────────────────────────
TRACKER_TAB = 'Daily Counts'
TRACKER_COUNTIES = ['Harris', 'Bexar', 'Dallas', 'Tarrant', 'Denton', 'Johnson']
TRACKER_HEADERS = ['Date'] + TRACKER_COUNTIES + ['Total']


def _get_client() -> gspread.Client:
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if not creds_json:
        raise EnvironmentError("GOOGLE_CREDENTIALS environment variable is not set.")
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(worksheet: gspread.Worksheet):
    first_row = worksheet.row_values(1)
    if first_row != COLUMN_HEADERS:
        logger.info("Sheet headers missing or outdated — resetting row 1.")
        worksheet.update('A1', [COLUMN_HEADERS])


def _record_key(rec: Dict) -> str:
    county  = str(rec.get('county',   '')).strip().lower()
    doc_id  = str(rec.get('doc_id',   '')).strip()
    first   = str(rec.get('first_name','')).strip().lower()
    last    = str(rec.get('last_name', '')).strip().lower()
    file_dt = str(rec.get('file_date', '')).strip()
    address = str(rec.get('address',  '')).strip().lower()

    if doc_id:
        return f"docid|{county}|{doc_id}"
    if address and address not in ('n/a', ''):
        return f"{first}|{last}|{county}|{file_dt}|{address}"
    return f"{first}|{last}|{county}|{file_dt}"


def _existing_keys(worksheet: gspread.Worksheet) -> set:
    records = worksheet.get_all_records()
    return {
        _record_key({
            'doc_id':     r.get('Doc ID',                ''),
            'first_name': r.get('First Name',            ''),
            'last_name':  r.get('Last Name',             ''),
            'county':     r.get('County',                ''),
            'file_date':  r.get('Foreclosure File Date', ''),
            'address':    r.get('Address',               ''),
        })
        for r in records
    }


def _to_row(rec: Dict, pull_date: str = '') -> List[str]:
    return [
        rec.get('first_name', ''),
        rec.get('last_name',  ''),
        rec.get('address',    ''),
        rec.get('city',       ''),
        rec.get('state',      'TX'),
        rec.get('zip_code',   ''),
        rec.get('county',     ''),
        rec.get('file_date',  ''),
        rec.get('sale_date',  ''),
        rec.get('doc_id',     ''),
        pull_date,
    ]


def _sort_by_file_date(worksheet: gspread.Worksheet):
    """Sort the whole sheet (excluding the header row) by Foreclosure File Date.

    Dates are written with USER_ENTERED so Sheets stores them as real dates and
    sorts chronologically. Non-fatal if it fails — the data is already written."""
    try:
        n_rows = len(worksheet.col_values(1))  # includes header
        if n_rows > 2:
            worksheet.sort((FILE_DATE_COL, 'asc'),
                           range=f'A2:{LAST_COL_LETTER}{n_rows}')
            logger.info(f"Sorted sheet by file date (rows 2–{n_rows}).")
    except Exception as e:
        logger.warning(f"Sort by file date failed (data still written): {e}")


def write_records(records: List[Dict], pull_date: str = '') -> int:
    if not records:
        logger.info("No records to write.")
        return 0

    client    = _get_client()
    worksheet = client.open_by_key(SPREADSHEET_ID).sheet1

    _ensure_headers(worksheet)
    existing   = _existing_keys(worksheet)

    new_rows   = []
    seen_today = set()

    for rec in records:
        key = _record_key(rec)
        if key in existing or key in seen_today:
            continue
        seen_today.add(key)
        new_rows.append(_to_row(rec, pull_date))

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        logger.info(f"Wrote {len(new_rows)} new rows to Google Sheets.")
        _sort_by_file_date(worksheet)
    else:
        logger.info("All records were duplicates — nothing written.")

    return len(new_rows)


def update_daily_tracker(pull_date: str, found_by_county: Dict[str, int]):
    """Upsert one row per day on the 'Daily Counts' tab: how many records were
    pulled per county that day. Re-running a day overwrites its row."""
    client = _get_client()
    ss = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = ss.worksheet(TRACKER_TAB)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=TRACKER_TAB, rows=1000, cols=len(TRACKER_HEADERS))
        ws.append_row(TRACKER_HEADERS, value_input_option='USER_ENTERED')
        logger.info(f"Created '{TRACKER_TAB}' tab.")

    counts = [int(found_by_county.get(c, 0)) for c in TRACKER_COUNTIES]
    row = [pull_date] + counts + [sum(counts)]

    dates = ws.col_values(1)  # includes header
    if pull_date in dates:
        idx = dates.index(pull_date) + 1
        ws.update(f'A{idx}', [row], value_input_option='USER_ENTERED')
        logger.info(f"Updated tracker row for {pull_date}: {dict(zip(TRACKER_COUNTIES, counts))}")
    else:
        ws.append_row(row, value_input_option='USER_ENTERED')
        logger.info(f"Appended tracker row for {pull_date}: {dict(zip(TRACKER_COUNTIES, counts))}")
