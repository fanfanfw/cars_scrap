from __future__ import annotations

import argparse
import json
import os

from dotenv import load_dotenv

from scrap_olx_cars_botasaurus.olx_service import OlxCarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from OLX Indonesia (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument(
        "--profile",
        default=None,
        help="Override BOTASAURUS_PROFILE only for this OLX process (example: olx_scrape).",
    )
    parser.add_argument("--max-pages", type=int, default=1, help="Max pages to visit (default: 1)")
    parser.add_argument("--details", action="store_true", help="Fetch listing details and upsert into Postgres")
    parser.add_argument("--debug-detail-url", default=None, help="Scrape 1 OLX detail URL and print JSON result")
    args = parser.parse_args()

    if args.profile:
        os.environ["BOTASAURUS_PROFILE"] = str(args.profile).strip()

    scraper = OlxCarsService()
    try:
        def summarize_results(results: list[dict] | None) -> dict:
            items = results or []
            upserted: list[str] = []
            errors: list[str] = []
            sold_marked: list[str] = []
            for item in items:
                row = item or {}
                ref = str(row.get("listing_url") or row.get("listing_id") or "").strip()
                if row.get("error"):
                    if ref:
                        errors.append(ref)
                    continue
                if str(row.get("status") or "").lower() == "sold":
                    if ref:
                        sold_marked.append(ref)
                    continue
                if row.get("listing_id") and ref:
                    upserted.append(ref)
            return {
                "scraped": len(items),
                "upserted": upserted,
                "upserted_count": len(upserted),
                "errors": errors,
                "errors_count": len(errors),
                "sold_marked": sold_marked,
                "sold_marked_count": len(sold_marked),
            }

        if args.debug_detail_url:
            from scrap_olx_cars_botasaurus.database import get_connection

            url = (args.debug_detail_url or "").strip()
            if not url:
                raise ValueError("--debug-detail-url is empty")

            details = scraper.scrape_listing_details([url], allow_revive_sold=True)
            scraped = details[0] if details else None
            metrics = summarize_results(details)
            listing_id = (scraped or {}).get("listing_id")

            db_row = None
            if listing_id:
                conn = None
                cursor = None
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM cars_scrap_olx WHERE listing_id = %s", (str(listing_id),))
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
                        "stored_to_db": bool(metrics["upserted_count"]),
                        "db_row_found": bool(db_row),
                        "metrics": metrics,
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
            details = scraper.scrape_listing_details(links, allow_revive_sold=True)
            metrics = summarize_results(details)
            print(json.dumps({"count": len(details), **metrics, "stored_to_db": bool(metrics["upserted_count"])}, indent=2))
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
