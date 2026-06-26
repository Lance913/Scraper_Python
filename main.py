"""
Main orchestrator for the TX Pre-Foreclosure Daily Scraper

Usage:
  python main.py                     # scrape today
  python main.py --date 2026-06-25   # scrape a specific date
  python main.py --dry-run           # scrape but don't write to Sheets
"""

import argparse
import logging
import sys
from datetime import date, datetime
from typing import List, Dict

from scrapers import (
    HarrisCountyScraper,
    BexarCountyScraper,
    DallasCountyScraper,
    TarrantCountyScraper,
)
import sheets_writer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger('main')


def parse_args():
    p = argparse.ArgumentParser(description='TX Foreclosure Scraper')
    p.add_argument(
        '--date',
        type=str,
        default=None,
        help='Target date in YYYY-MM-DD format (default: today)',
    )
    p.add_argument(
        '--dry-run',
        action='store_true',
        help='Scrape but do not write to Google Sheets',
    )
    p.add_argument(
        '--counties',
        nargs='+',
        choices=['harris', 'bexar', 'dallas', 'tarrant'],
        default=['harris', 'bexar', 'dallas', 'tarrant'],
        help='Which counties to scrape (default: all four)',
    )
    return p.parse_args()


def run_scrapers(target_date: date, counties: List[str]) -> List[Dict]:
    scraper_map = {
        'harris':  HarrisCountyScraper,
        'bexar':   BexarCountyScraper,
        'dallas':  DallasCountyScraper,
        'tarrant': TarrantCountyScraper,
    }

    all_records = []
    for name in counties:
        cls = scraper_map[name]
        try:
            scraper = cls()
            records = scraper.scrape(target_date)
            logger.info(f"{name.title()}: {len(records)} records")
            all_records.extend(records)
        except Exception as exc:
            logger.error(f"{name.title()} scraper crashed: {exc}", exc_info=True)

    return all_records


def main():
    args = parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        target_date = date.today()

    logger.info(f"=== TX Foreclosure Scraper | {target_date} ===")
    logger.info(f"Counties: {', '.join(args.counties)}")

    records = run_scrapers(target_date, args.counties)

    logger.info(f"Total records collected: {len(records)}")

    if not records:
        logger.warning("No records found. This is normal on weekends / non-filing days.")
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
