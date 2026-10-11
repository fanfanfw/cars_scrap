from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from dotenv import load_dotenv

from cars_scrap_common.duration_utils import cutoff_date
from scrap_mobil123_cars_botasaurus.mobil123_service import Mobil123CarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from mobil123 (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument(
        "--profile",
        default=None,
        help="Override BOTASAURUS_PROFILE only for this mobil123 process (example: mobil123_scrape, mobil123_track).",
    )
    parser.add_argument("--max-pages", type=int, default=1, help="Max pages to visit (default: 1)")
    parser.add_argument("--details", action="store_true", help="Fetch listing details and upsert into Postgres")
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
        "--track-last-scraped-older-than",
        default=None,
        help="Only re-scrape listings where last_scraped_at <= today - duration (example: 7d, 1w, 3m).",
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
        "--debug-detail-url",
        default=None,
        help="Scrape 1 listing URL, upsert into Postgres, then print the inserted DB row as JSON",
    )
    args = parser.parse_args()

    if args.profile:
        os.environ["BOTASAURUS_PROFILE"] = str(args.profile).strip()

    is_tracking = bool(args.track_older_than)
    scraper = Mobil123CarsService(tracking=is_tracking)
    try:
        def summarize_results(results: list[dict] | None) -> dict:
            items = results or []
            scraped = len(items)
            error_urls: list[str] = []
            sold_urls: list[str] = []
            skipped_urls: list[str] = []
            upserted_urls: list[str] = []
            skip_reasons: dict[str, int] = {}

            def listing_ref(row: dict) -> str:
                url = str(row.get("listing_url") or "").strip()
                if url:
                    return url
                listing_id = str(row.get("listing_id") or "").strip()
                return listing_id

            for r in items:
                row = r or {}
                if row.get("error"):
                    ref = listing_ref(row)
                    if ref:
                        error_urls.append(ref)
                    continue

                status = str(row.get("status") or "").strip().lower()
                listing_id = row.get("listing_id")

                if status == "sold":
                    ref = listing_ref(row)
                    if ref:
                        sold_urls.append(ref)
                    continue

                if row.get("skipped_upsert"):
                    ref = listing_ref(row)
                    if ref:
                        skipped_urls.append(ref)
                    reason = str(row.get("skip_reason") or "unknown")
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue

                if not listing_id:
                    ref = listing_ref(row)
                    if ref:
                        skipped_urls.append(ref)
                    reason = "missing_listing_id"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue

                if status == "active":
                    ref = listing_ref(row)
                    if ref:
                        upserted_urls.append(ref)

            return {
                "scraped": scraped,
                "upserted": upserted_urls,
                "upserted_count": len(upserted_urls),
                "skipped": skipped_urls,
                "skipped_count": len(skipped_urls),
                "errors": error_urls,
                "errors_count": len(error_urls),
                "sold_marked": sold_urls,
                "sold_marked_count": len(sold_urls),
                "skip_reasons": skip_reasons,
            }

        if is_tracking:
            if args.details:
                raise ValueError("--details cannot be used with --track-older-than (tracking always scrapes details).")
            if args.start_url:
                raise ValueError("--start-url cannot be used with --track-older-than (tracking reads from DB).")
            if args.debug_detail_url:
                raise ValueError("--debug-detail-url cannot be used with --track-older-than.")

            from scrap_mobil123_cars_botasaurus.database import get_connection

            now = datetime.now().date()
            cutoff = cutoff_date(now=now, older_than=str(args.track_older_than))
            cutoff_last_scraped = None
            if args.track_last_scraped_older_than:
                cutoff_last_scraped = cutoff_date(now=now, older_than=str(args.track_last_scraped_older_than))

            conn = get_connection()
            cursor = conn.cursor()
            try:
                limit = int(args.track_limit or 0)
                if limit < 0:
                    raise ValueError("--track-limit must be >= 0")

                where = [
                    "status = 'active'",
                    "information_ads_date IS NOT NULL",
                    "information_ads_date <= %s",
                    "listing_url IS NOT NULL",
                ]
                params: list[object] = [cutoff]
                if cutoff_last_scraped:
                    where.insert(-1, "last_scraped_at::date <= %s")
                    params.append(cutoff_last_scraped)

                where_joiner = "\n                  AND "
                sql = f"""
                SELECT listing_id, listing_url
                FROM cars_scrap_mobil123
                WHERE {where_joiner.join(where)}
                ORDER BY last_scraped_at ASC, information_ads_date ASC
                """
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
                listing_id_by_url[url] = str(listing_id).strip()

            summary = {
                "mode": "track",
                "older_than": args.track_older_than,
                "cutoff_date": str(cutoff),
                "last_scraped_older_than": args.track_last_scraped_older_than,
                "last_scraped_cutoff_date": str(cutoff_last_scraped) if cutoff_last_scraped else None,
                "count": len(links),
            }
            if args.track_count_only:
                print(f"total_data: {len(links)} listing")
                return
            if args.track_dry_run:
                print(json.dumps(summary, indent=2, default=str))
                return

            results = scraper.scrape_listing_details_with_run_retries(links, listing_id_by_url=listing_id_by_url)
            metrics = summarize_results(results)
            print(
                json.dumps(
                    {
                        **summary,
                        "scraped": len(results),
                        "sold_marked": metrics["sold_marked"],
                        "sold_marked_count": metrics["sold_marked_count"],
                        "upserted": metrics["upserted"],
                        "upserted_count": metrics["upserted_count"],
                        "skipped": metrics["skipped"],
                        "skipped_count": metrics["skipped_count"],
                        "errors": metrics["errors"],
                        "errors_count": metrics["errors_count"],
                        "skip_reasons": metrics["skip_reasons"],
                        "stored_to_db": bool(metrics["sold_marked_count"] or metrics["upserted_count"]),
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        if args.debug_detail_url:
            from scrap_mobil123_cars_botasaurus.database import get_connection

            url = (args.debug_detail_url or "").strip()
            if not url:
                raise ValueError("--debug-detail-url is empty")

            details = scraper.scrape_listing_details_with_run_retries(
                [url],
                allow_revive_sold=True,
                require_brand_model=True,
            )
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
                    cursor.execute("SELECT * FROM cars_scrap_mobil123 WHERE listing_id = %s", (str(listing_id),))
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
            details = scraper.scrape_listing_details_with_run_retries(
                links,
                allow_revive_sold=True,
                require_brand_model=True,
            )
            metrics = summarize_results(details)
            print(
                json.dumps(
                    {"count": len(details), **metrics, "stored_to_db": bool(metrics["upserted_count"])},
                    indent=2,
                )
            )
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
