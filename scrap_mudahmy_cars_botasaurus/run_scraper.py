from __future__ import annotations

import argparse
import json
from datetime import datetime

from dotenv import load_dotenv

from cars_scrap_common.duration_utils import cutoff_date
from scrap_mudahmy_cars_botasaurus.mudahmy_service import MudahMyCarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from mudah.my (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument("--max-pages", type=int, default=1, help="Max pages to visit (default: 1)")
    parser.add_argument("--details", action="store_true", help="Upsert minimal details into Postgres")
    parser.add_argument(
        "--track-older-than",
        default=None,
        help="Re-scrape ACTIVE listings from DB where information_ads_date <= today - duration (example: 1w, 1m, 1y).",
    )
    parser.add_argument(
        "--track-limit",
        type=int,
        default=0,
        help="Limit number of tracked listings to process (0 = no limit).",
    )
    parser.add_argument(
        "--track-dry-run",
        action="store_true",
        help="Only print how many listings match tracking criteria (no scraping).",
    )
    parser.add_argument(
        "--track-count",
        "--track-count-only",
        dest="track_count_only",
        action="store_true",
        help="Print only the total matching listings count (no scraping).",
    )
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
        def summarize_results(results: list[dict] | None) -> dict:
            items = results or []
            scraped = len(items)
            errors = 0
            sold_detected = 0
            skipped = 0
            upserted = 0
            skip_reasons: dict[str, int] = {}

            for r in items:
                row = r or {}
                if row.get("error"):
                    errors += 1
                    continue

                status = str(row.get("status") or "").strip().lower()
                listing_id = row.get("listing_id")

                if status == "sold":
                    sold_detected += 1
                    continue

                if row.get("skipped_upsert"):
                    skipped += 1
                    reason = str(row.get("skip_reason") or "unknown")
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue

                if not listing_id:
                    skipped += 1
                    reason = "missing_listing_id"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue

                if status == "active":
                    upserted += 1

            return {
                "scraped": scraped,
                "upserted": upserted,
                "skipped": skipped,
                "errors": errors,
                "sold_detected": sold_detected,
                "skip_reasons": skip_reasons,
            }

        if args.track_older_than:
            if args.details:
                raise ValueError("--details cannot be used with --track-older-than (tracking always scrapes details).")
            if args.start_url:
                raise ValueError("--start-url cannot be used with --track-older-than (tracking reads from DB).")
            if args.debug_detail_url:
                raise ValueError("--debug-detail-url cannot be used with --track-older-than.")

            from scrap_mudahmy_cars_botasaurus.database import get_connection

            now = datetime.now().date()
            cutoff = cutoff_date(now=now, older_than=str(args.track_older_than))

            conn = get_connection()
            cursor = conn.cursor()
            try:
                limit = int(args.track_limit or 0)
                if limit < 0:
                    raise ValueError("--track-limit must be >= 0")

                sql = """
                SELECT listing_id, listing_url
                FROM cars_scrap_mudahmy
                WHERE status = 'active'
                  AND information_ads_date IS NOT NULL
                  AND information_ads_date <= %s
                  AND listing_url IS NOT NULL
                ORDER BY information_ads_date ASC
                """
                params: list[object] = [cutoff]
                if limit:
                    sql += " LIMIT %s"
                    params.append(limit)

                cursor.execute(sql, params)
                db_rows = [row for row in cursor.fetchall() if row and row[0] and row[1]]
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

            seen: set[str] = set()
            links: list[str] = []
            listing_id_by_url: dict[str, str] = {}
            for listing_id, listing_url in db_rows:
                url = str(listing_url).strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                links.append(url)
                listing_id_by_url[url] = str(listing_id)

            summary = {
                "mode": "track",
                "older_than": args.track_older_than,
                "cutoff_date": str(cutoff),
                "count": len(links),
                "sample": links[:20],
            }
            if args.track_count_only:
                print(f"total_data: {len(links)} listing")
                return
            if args.track_dry_run:
                print(json.dumps(summary, indent=2, default=str))
                return

            results = scraper.scrape_listing_details(links, listing_id_by_url=listing_id_by_url)
            metrics = summarize_results(results)
            sold = sum(1 for r in results if (r or {}).get("status") == "sold")
            errored = sum(1 for r in results if (r or {}).get("error"))
            print(
                json.dumps(
                    {
                        **summary,
                        "scraped": len(results),
                        "sold_marked": sold,
                        "upserted": metrics["upserted"],
                        "skipped": metrics["skipped"],
                        "errors": errored,
                        "skip_reasons": metrics["skip_reasons"],
                        "stored_to_db": bool(sold or metrics["upserted"]),
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        if args.debug_detail_url:
            from scrap_mudahmy_cars_botasaurus.database import get_connection

            url = (args.debug_detail_url or "").strip()
            if not url:
                raise ValueError("--debug-detail-url is empty")

            details = scraper.scrape_listing_details([url], allow_revive_sold=True, require_brand_model=True)
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
                        "stored_to_db": bool(metrics["upserted"]),
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
            details = scraper.scrape_listing_details(links, allow_revive_sold=True, require_brand_model=True)
            metrics = summarize_results(details)
            payload = {"count": len(details), **metrics, "stored_to_db": bool(metrics["upserted"])}
            if args.print_links:
                payload["links"] = links
            print(json.dumps(payload, indent=2))
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
