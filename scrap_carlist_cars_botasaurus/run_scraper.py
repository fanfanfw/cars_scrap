from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from scrap_carlist_cars_botasaurus.carlist_service import CarlistCarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from carlist (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument("--max-pages", type=int, default=1, help="Max pages to visit (default: 1)")
    parser.add_argument("--details", action="store_true", help="Upsert minimal details into Postgres")
    args = parser.parse_args()

    scraper = CarlistCarsService()
    try:
        links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages)
        if args.details:
            details = scraper.scrape_listing_details(links)
            print(json.dumps({"count": len(details), "stored_to_db": True}, indent=2))
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()

