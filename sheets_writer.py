"""
Google Sheets Writer

Appends new foreclosure records to the target spreadsheet.
Deduplicates by Address + County + Sale Date — won't add a row it already sees.

Requirements
  - GOOGLE_CREDENTIALS env var: the full JSON of a GCP service account key
  - The target sheet must be shared with the service account email (editor access)
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
]

# Columns used to detect duplicates (0-indexed into COLUMN_HEADERS)
DEDUP_COLS = ['Address', 'County', 'Sale Date']


def _get_client() -> gspread.Client:
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if not creds_json:
        raise EnvironmentError("GOOGLE_CREDENTIALS environment variable is not set.")
    creds_data = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_data, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(worksheet: gspread.Worksheet):
    first_row = worksheet.row_values(1)
    if first_row != COLUMN_HEADERS:
        logger.info("Sheet headers missing or outdated — resetting row 1.")
        worksheet.update('A1', [COLUMN_HEADERS])


def _existing_dedup_keys(worksheet: gspread.Worksheet) -> set:
    all_records = worksheet.get_all_records()
    keys = set()
    for r in all_records:
        key = '|'.join(
            str(r.get(col, '')).strip().lower()
            for col in DEDUP_COLS
        )
        keys.add(key)
    return keys


def _record_key(rec: Dict) -> str:
    mapping = {
        'Address':   rec.get('address', ''),
        'County':    rec.get('county', ''),
        'Sale Date': rec.get('sale_date', ''),
    }
    return '|'.join(str(mapping[c]).strip().lower() for c in DEDUP_COLS)


def _to_row(rec: Dict) -> List[str]:
    return [
        rec.get('first_name', ''),
        rec.get('last_name', ''),
        rec.get('address', ''),
        rec.get('city', ''),
        rec.get('state', 'TX'),
        rec.get('zip_code', ''),
        rec.get('county', ''),
        rec.get('file_date', ''),
        rec.get('sale_date', ''),
    ]


def write_records(records: List[Dict]) -> int:
    """
    Write new records to Google Sheets.
    Returns the number of rows actually added.
    """
    if not records:
        logger.info("No records to write.")
        return 0

    client    = _get_client()
    sheet     = client.open_by_key(SPREADSHEET_ID)
    worksheet = sheet.sheet1

    _ensure_headers(worksheet)
    existing_keys = _existing_dedup_keys(worksheet)

    new_rows   = []
    seen_today = set()

    for rec in records:
        key = _record_key(rec)
        if key in existing_keys or key in seen_today:
            continue
        seen_today.add(key)
        new_rows.append(_to_row(rec))

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option='USER_ENTERED')
        logger.info(f"Wrote {len(new_rows)} new rows to Google Sheets.")
    else:
        logger.info("All records were duplicates — nothing written.")

    return len(new_rows)
