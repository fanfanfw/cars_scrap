from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from dotenv import load_dotenv

from cars_scrap_common.duration_utils import cutoff_date
from scrap_olx_cars_botasaurus.olx_service import OlxCarsService

load_dotenv(override=True)


def _fail_listing_stage(failure_type: str) -> int:
    print(
        json.dumps(
            {
                "run_status": "failed",
                "failure_stage": "listing",
                "failure_type": failure_type,
                "listing_count": 0,
                "upserted_count": 0,
                "errors_count": 0,
                "upserted": [],
                "errors": [],
                "stored_to_db": False,
            },
            indent=2,
        )
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape car listings from OLX Indonesia (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument(
        "--profile",
        default=None,
        help="Override BOTASAURUS_PROFILE only for this OLX process (example: olx_scrape, olx_track).",
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

        if args.track_older_than:
            if args.details:
                raise ValueError("--details cannot be used with --track-older-than (tracking always scrapes details).")
            if args.start_url:
                raise ValueError("--start-url cannot be used with --track-older-than (tracking reads from DB).")
            if args.debug_detail_url:
                raise ValueError("--debug-detail-url cannot be used with --track-older-than.")

            from scrap_olx_cars_botasaurus.database import get_connection

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
                FROM cars_scrap_olx
                WHERE {where_joiner.join(where)}
                ORDER BY last_scraped_at ASC, information_ads_date ASC, listing_id ASC
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
                return 0
            if args.track_dry_run:
                print(json.dumps(summary, indent=2, default=str))
                return 0

            results = scraper.scrape_listing_details(
                links,
                listing_id_by_url=listing_id_by_url,
                allow_revive_sold=False,
            )
            metrics = summarize_results(results)
            print(
                json.dumps(
                    {
                        **summary,
                        "scraped": len(results),
                        "upserted": metrics["upserted"],
                        "upserted_count": metrics["upserted_count"],
                        "sold_marked": metrics["sold_marked"],
                        "sold_marked_count": metrics["sold_marked_count"],
                        "errors": metrics["errors"],
                        "errors_count": metrics["errors_count"],
                        "stored_to_db": bool(metrics["sold_marked_count"] or metrics["upserted_count"]),
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0

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
            return 0

        try:
            links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages)
        except Exception:
            return _fail_listing_stage("listing_scrape_error")
        if args.details:
            details = scraper.scrape_listing_details(links, allow_revive_sold=True)
            metrics = summarize_results(details)
            print(json.dumps({"count": len(details), **metrics, "stored_to_db": bool(metrics["upserted_count"])}, indent=2))
        else:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
    finally:
        scraper.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
