"""
Main orchestrator — TX Pre-Foreclosure Daily Scraper

Usage:
  python main.py                                          # all counties, today
  python main.py --date 2026-06-25                        # specific date
  python main.py --counties harris bexar dallas tarrant   # specific counties
  python main.py --dry-run                                # scrape, don't write
"""

import argparse
import glob
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime
from typing import List, Dict

from scrapers import (
    HarrisCountyScraper,
    BexarCountyScraper,
    DallasCountyScraper,
    TarrantCountyScraper,
    DentonCountyScraper,
    JohnsonCountyScraper,
)
import sheets_writer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('main')

ALL_COUNTIES = ['harris', 'bexar', 'dallas', 'tarrant', 'denton', 'johnson']

SCRAPER_MAP = {
    'harris':  HarrisCountyScraper,
    'bexar':   BexarCountyScraper,
    'dallas':  DallasCountyScraper,
    'tarrant': TarrantCountyScraper,
    'denton':  DentonCountyScraper,
    'johnson': JohnsonCountyScraper,
}


def parse_args():
    p = argparse.ArgumentParser(description='TX Foreclosure Scraper')
    p.add_argument('--date', type=str, default=None,
                   help='Target date YYYY-MM-DD (default: today)')
    p.add_argument('--dry-run', action='store_true',
                   help='Scrape but do not write to Google Sheets')
    p.add_argument('--counties', nargs='+', choices=ALL_COUNTIES,
                   default=ALL_COUNTIES,
                   help='Counties to scrape (default: all)')
    p.add_argument('--output', type=str, default=None,
                   help='Write scraped records to this JSON file instead of Sheets '
                        '(used by the per-county matrix jobs).')
    p.add_argument('--from-json', nargs='+', default=None,
                   help='Glob(s) of record JSON files to load and write to Sheets '
                        '(used by the collate job). Skips scraping.')
    return p.parse_args()


def _useful(r: Dict) -> bool:
    """Keep every upcoming filing that has any identifying field — a name, an
    address, or at least a Doc ID reference (so nothing is silently dropped)."""
    return bool(r.get('first_name') or r.get('last_name')
                or r.get('address') or r.get('doc_id'))


def load_records(patterns: List[str]) -> List[Dict]:
    records: List[Dict] = []
    for pat in patterns:
        for path in sorted(glob.glob(pat, recursive=True)):
            try:
                with open(path) as f:
                    data = json.load(f)
                records.extend(data)
                logger.info(f"Loaded {len(data)} records from {path}")
            except Exception as exc:
                logger.error(f"Failed to read {path}: {exc}")
    return records


def run_scrapers(target_date: date, counties: List[str]) -> List[Dict]:
    all_records = []
    for name in counties:
        cls = SCRAPER_MAP[name]
        try:
            records = cls().scrape(target_date)
            logger.info(f"{name.title()}: {len(records)} records")
            all_records.extend(records)
        except Exception as exc:
            logger.error(f"{name.title()} scraper crashed: {exc}", exc_info=True)
    return all_records


def main():
    args = parse_args()

    # Collate mode: load records from JSON artifacts and write them to Sheets.
    if args.from_json:
        records = [r for r in load_records(args.from_json) if _useful(r)]
        logger.info(f"Total records loaded: {len(records)}")
        if not records:
            logger.warning("No records to write.")
            return
        if args.dry_run:
            logger.info("DRY RUN — not writing to Sheets")
            for r in records:
                logger.info(r)
            return
        # Stamp the pull date (when the scraper ran) and record the daily counts.
        pull_date = (datetime.strptime(args.date, '%Y-%m-%d')
                     if args.date else datetime.now()).strftime('%m/%d/%Y')
        scanned_by_county = Counter(r.get('county', '') for r in records)
        new_records = sheets_writer.write_records(records, pull_date=pull_date)
        new_by_county = Counter(r.get('county', '') for r in new_records)
        # Tracker records NEW leads per county (not everything re-scanned in the window).
        sheets_writer.update_daily_tracker(pull_date, new_by_county)
        logger.info(f"Done. {len(new_records)} new rows added (pull date {pull_date}). "
                    f"New per county: {dict(new_by_county)} | "
                    f"scanned per county: {dict(scanned_by_county)}")
        return

    target_date = (datetime.strptime(args.date, '%Y-%m-%d').date()
                   if args.date else date.today())

    logger.info(f"=== TX Foreclosure Scraper | {target_date} ===")
    logger.info(f"Counties: {', '.join(args.counties)}")

    records = [r for r in run_scrapers(target_date, args.counties) if _useful(r)]
    logger.info(f"Total records collected: {len(records)}")

    # Output mode: dump to JSON for the collate job (per-county matrix).
    if args.output:
        with open(args.output, 'w') as f:
            json.dump(records, f)
        logger.info(f"Wrote {len(records)} records to {args.output}")
        return

    if not records:
        logger.warning("No records found.")
        return

    if args.dry_run:
        logger.info("DRY RUN — not writing to Sheets")
        for r in records:
            logger.info(r)
        return

    added = sheets_writer.write_records(records)
    logger.info(f"Done. {added} new rows added to Google Sheets.")


if __name__ == '__main__':
    main()
