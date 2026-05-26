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
    parser.add_argument("--details", action="store_true", help="Fetch listing details and upsert into Postgres")
    parser.add_argument(
        "--debug-detail-url",
        default=None,
        help="Scrape 1 listing URL, upsert into Postgres, then print the parsed result as JSON",
    )
    args = parser.parse_args()

    if args.profile:
        os.environ["BOTASAURUS_PROFILE"] = str(args.profile).strip()

    scraper = CarsomeIdCarsService()
    try:
        def compact_sample(items: list[dict], limit: int = 10) -> list[dict]:
            out: list[dict] = []
            for row in items[:limit]:
                item = dict(row)
                raw = item.pop("raw_detail", None)
                if isinstance(raw, dict):
                    item["raw_detail_keys"] = sorted(raw.keys())
                out.append(item)
            return out

        if args.debug_detail_url:
            details = scraper.scrape_listing_details([args.debug_detail_url])
            print(json.dumps({"count": len(details), "sample": compact_sample(details, 1), "stored_to_db": True}, indent=2, default=str))
            return

        links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages)
        if args.details:
            details = scraper.scrape_listing_details(links)
            errors = [row for row in details if row.get("error")]
            print(
                json.dumps(
                    {
                        "count": len(details),
                        "upserted_count": len(details) - len(errors),
                        "errors_count": len(errors),
                        "sample": compact_sample(details),
                        "stored_to_db": len(details) > len(errors),
                    },
                    indent=2,
                    default=str,
                )
            )
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
