from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from scrap_mudahmy_cars_botasaurus.mudahmy_service import MudahMyCarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from mudah.my (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument("--max-pages", type=int, default=1, help="Max pages to visit (default: 1)")
    parser.add_argument("--details", action="store_true", help="Upsert minimal details into Postgres")
    parser.add_argument(
        "--print-links",
        action="store_true",
        help="Print collected listing links (useful for debugging)",
    )
    parser.add_argument(
        "--debug-detail-url",
        default=None,
        help="Scrape 1 listing URL, upsert into Postgres, then print the inserted DB row as JSON",
    )
    args = parser.parse_args()

    scraper = MudahMyCarsService()
    try:
        if args.debug_detail_url:
            from scrap_mudahmy_cars_botasaurus.database import get_connection

            url = (args.debug_detail_url or "").strip()
            if not url:
                raise ValueError("--debug-detail-url is empty")

            details = scraper.scrape_listing_details([url])
            scraped = details[0] if details else None
            listing_id = (scraped or {}).get("listing_id")

            db_row = None
            if listing_id:
                conn = None
                cursor = None
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM cars_scrap_mudahmy WHERE listing_id = %s", (int(listing_id),))
                    row = cursor.fetchone()
                    if row:
                        cols = [desc[0] for desc in cursor.description or []]
                        db_row = dict(zip(cols, row))
                finally:
                    try:
                        if cursor:
                            cursor.close()
                    except Exception:
                        pass
                    try:
                        if conn:
                            conn.close()
                    except Exception:
                        pass

            print(
                json.dumps(
                    {
                        "debug_detail_url": url,
                        "stored_to_db": bool(db_row),
                        "scraped_result": scraped,
                        "db_row": db_row,
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages)
        if args.details:
            details = scraper.scrape_listing_details(links)
            payload = {"count": len(details), "stored_to_db": True}
            if args.print_links:
                payload["links"] = links
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
