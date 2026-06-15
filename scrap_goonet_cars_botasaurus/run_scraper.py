from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from dotenv import load_dotenv

from cars_scrap_common.duration_utils import cutoff_date
from scrap_goonet_cars_botasaurus.goonet_service import GoonetCarsService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape car listings from Goonet Exchange (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument(
        "--profile",
        default=None,
        help="Override BOTASAURUS_PROFILE only for this Goonet process.",
    )
    parser.add_argument("--max-pages", type=int, default=0, help="Max pages to visit (0 = all for listing; default listing mode: all)")
    parser.add_argument("--details", action="store_true", help="Fetch listing details and upsert into Postgres")
    parser.add_argument("--listing", action="store_true", help="Fetch listing URLs from Goonet master maker/model/grade filters via HTTP")
    parser.add_argument("--maker", default=None, help="Goonet maker name or maker_cd, e.g. TOYOTA or 1010")
    parser.add_argument("--model", default=None, help="Goonet model name or car_cd, e.g. ALLION or 10101058")
    parser.add_argument("--grade", default=None, help="Goonet grade name or grade_cd, optional")
    parser.add_argument("--listing-limit", type=int, default=20, help="Goonet listing page size (default: 20)")
    parser.add_argument(
        "--track-older-than",
        default=None,
        help="Re-scrape ACTIVE listings from DB where information_ads_date <= today - duration (example: 1w, 1m, 1y).",
    )
    parser.add_argument("--track-limit", type=int, default=0, help="Limit tracked listings to process (0 = no limit).")
    parser.add_argument(
        "--track-last-scraped-older-than",
        default=None,
        help="Only re-scrape listings where last_scraped_at <= today - duration (example: 7d, 1w, 3m).",
    )
    parser.add_argument("--track-dry-run", action="store_true", help="Only print tracking targets without scraping.")
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
    parser.add_argument("--sync-master-data", action="store_true", help="Sync Goonet maker/model/grade master data into Postgres")
    parser.add_argument("--master-brand-cd", default=None, help="Override GOONET_MASTER_SEED_BRAND_CD for maker endpoint")
    parser.add_argument("--master-max-makers", type=int, default=0, help="Limit makers to sync for testing (0 = all)")
    parser.add_argument("--master-max-models", type=int, default=0, help="Limit models per maker for testing (0 = all)")
    parser.add_argument("--no-master-resume", action="store_true", help="Fetch grades again even if grades for a model already exist")
    parser.add_argument("--sync-missing-master-grades", action="store_true", help="Sync only Goonet models that do not have any grade rows yet")
    parser.add_argument("--missing-grades-limit", type=int, default=0, help="Limit models for --sync-missing-master-grades (0 = all)")
    args = parser.parse_args()

    if args.profile:
        os.environ["BOTASAURUS_PROFILE"] = str(args.profile).strip()

    scraper = GoonetCarsService()
    try:
        def summarize_results(results: list[dict] | None) -> dict:
            items = results or []
            errors: list[str] = []
            sold: list[str] = []
            skipped: list[str] = []
            upserted: list[str] = []
            skip_reasons: dict[str, int] = {}

            def listing_ref(row: dict) -> str:
                return str(row.get("listing_url") or row.get("listing_id") or "").strip()

            for row in items:
                if row.get("error"):
                    ref = listing_ref(row)
                    if ref:
                        errors.append(ref)
                    continue
                status = str(row.get("status") or "").strip().lower()
                if status == "sold":
                    ref = listing_ref(row)
                    if ref:
                        sold.append(ref)
                    continue
                if row.get("skipped_upsert"):
                    ref = listing_ref(row)
                    if ref:
                        skipped.append(ref)
                    reason = str(row.get("skip_reason") or "unknown")
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue
                if row.get("listing_id") and status == "active":
                    ref = listing_ref(row)
                    if ref:
                        upserted.append(ref)

            return {
                "scraped": len(items),
                "upserted": upserted,
                "upserted_count": len(upserted),
                "skipped": skipped,
                "skipped_count": len(skipped),
                "errors": errors,
                "errors_count": len(errors),
                "sold_marked": sold,
                "sold_marked_count": len(sold),
                "skip_reasons": skip_reasons,
            }

        if args.listing:
            links = scraper.scrape_all_from_master_listing(
                maker=args.maker,
                model=args.model,
                grade=args.grade,
                max_pages=int(args.max_pages or 0),
                limit=int(args.listing_limit or 20),
            )
            if not args.details:
                print(json.dumps({"mode": "listing", "count": len(links), "links": links}, indent=2))
                return
            details = scraper.scrape_listing_details_with_run_retries(links, allow_revive_sold=True, require_brand_model=True)
            print(json.dumps({"mode": "listing_details", "count": len(links), **summarize_results(details), "stored_to_db": True}, indent=2, default=str))
            return

        if args.sync_missing_master_grades:
            if args.details or args.track_older_than or args.debug_detail_url or args.sync_master_data:
                raise ValueError("--sync-missing-master-grades cannot be combined with other scrape modes.")
            summary = scraper.sync_missing_master_grades(limit=int(args.missing_grades_limit or 0))
            print(json.dumps({"mode": "sync_missing_master_grades", **summary}, indent=2, default=str))
            return

        if args.sync_master_data:
            if args.details or args.track_older_than or args.debug_detail_url:
                raise ValueError("--sync-master-data cannot be combined with --details, --track-older-than, or --debug-detail-url.")
            summary = scraper.sync_master_data(
                brand_cd=args.master_brand_cd,
                max_makers=int(args.master_max_makers or 0),
                max_models=int(args.master_max_models or 0),
                resume=not bool(args.no_master_resume),
            )
            print(json.dumps({"mode": "sync_master_data", **summary}, indent=2, default=str))
            return

        if args.track_older_than:
            if args.details:
                raise ValueError("--details cannot be used with --track-older-than.")
            if args.start_url:
                raise ValueError("--start-url cannot be used with --track-older-than.")
            if args.debug_detail_url:
                raise ValueError("--debug-detail-url cannot be used with --track-older-than.")

            from scrap_goonet_cars_botasaurus.database import get_connection

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
                sql = f"""
                SELECT listing_id, listing_url
                FROM cars_scrap_goonet
                WHERE {' AND '.join(where)}
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

            links: list[str] = []
            listing_id_by_url: dict[str, str] = {}
            seen: set[str] = set()
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
            print(json.dumps({**summary, **summarize_results(results), "stored_to_db": True}, indent=2, default=str))
            return

        if args.debug_detail_url:
            from scrap_goonet_cars_botasaurus.database import get_connection

            url = str(args.debug_detail_url or "").strip()
            if not url:
                raise ValueError("--debug-detail-url is empty")
            details = scraper.scrape_listing_details_with_run_retries([url], allow_revive_sold=True, require_brand_model=True)
            scraped = details[0] if details else None
            listing_id = (scraped or {}).get("listing_id")
            db_row = None
            if listing_id:
                conn = get_connection()
                cursor = conn.cursor()
                try:
                    cursor.execute(
                        """
                        SELECT row_to_json(t)
                        FROM (
                          SELECT c.*, d.media, d.overview, d.ratings, d.certification, d.specifications, d.equipment, d.hidden_codes, d.raw_json_ld
                          FROM cars_scrap_goonet c
                          LEFT JOIN cars_scrap_goonet_details d USING (listing_id)
                          WHERE c.listing_id = %s
                        ) t
                        """,
                        (str(listing_id),),
                    )
                    row = cursor.fetchone()
                    db_row = row[0] if row else None
                finally:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
            print(
                json.dumps(
                    {
                        "mode": "debug_detail_url",
                        "url": url,
                        "scraped": scraper.to_jsonable(scraped),
                        "summary": summarize_results(details),
                        "db_row": scraper.to_jsonable(db_row),
                    },
                    indent=2,
                    default=str,
                )
            )
            return

        links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages)
        if not args.details:
            print(json.dumps({"count": len(links), "links": links}, indent=2))
            return
        details = scraper.scrape_listing_details_with_run_retries(links, allow_revive_sold=True, require_brand_model=True)
        print(json.dumps({"count": len(details), **summarize_results(details), "stored_to_db": True}, indent=2, default=str))
    finally:
        scraper.close()


if __name__ == "__main__":
    main()
