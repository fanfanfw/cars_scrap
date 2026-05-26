from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from scrap_carsomeid_cars_botasaurus.carsomeid_service import CarsomeIdCarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from Carsome Indonesia (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument(
        "--profile",
        default=None,
        help="Override BOTASAURUS_PROFILE only for this carsomeid process (example: carsomeid_scrape).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Listing scroll rounds to run while collecting URLs (default: 1)",
    )
    args = parser.parse_args()

    if args.profile:
        os.environ["BOTASAURUS_PROFILE"] = str(args.profile).strip()

    scraper = CarsomeIdCarsService()
    try:
        links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages)
        print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
