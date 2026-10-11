from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

from dotenv import load_dotenv

from cars_scrap_common.duration_utils import cutoff_date
from scrap_carsomeid_cars_botasaurus.carsomeid_service import CarsomeIdCarsService

load_dotenv(override=True)


def _summarize_results(results: list[dict] | None) -> dict:
    upserted: list[str] = []
    sold_marked: list[str] = []
    errors: list[str] = []
    for row in results or []:
        item = row or {}
        ref = str(item.get("listing_url") or item.get("listing_id") or "").strip()
        if item.get("error"):
            if ref:
                errors.append(ref)
            continue
        if str(item.get("status") or "").strip().lower() == "sold":
            if ref:
                sold_marked.append(ref)
            continue
        if item.get("listing_id") and ref:
            upserted.append(ref)
    return {
        "upserted": upserted,
        "upserted_count": len(upserted),
        "sold_marked": sold_marked,
        "sold_marked_count": len(sold_marked),
        "errors": errors,
        "errors_count": len(errors),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape car listings from Carsome Indonesia (Botasaurus)")
    parser.add_argument("--start-url", default=None, help="Override listing URL (optional)")
    parser.add_argument(
        "--profile",
        default=None,
        help="Override BOTASAURUS_PROFILE only for this carsomeid process (example: carsomeid_scrape, carsomeid_track).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Max listing pages to visit using pageNo pagination (default: 1)",
    )
    parser.add_argument(
        "--all-pages",
        action="store_true",
        help="Visit pageNo=1,2,3... until CarsomeID returns an empty listing page. Safety cap: CARSOMEID_ALL_PAGES_MAX (default 200).",
    )
    parser.add_argument("--details", action="store_true", help="Fetch listing details and upsert into Postgres")
    parser.add_argument(
        "--track-last-scraped-older-than",
        default=None,
        help="Track ACTIVE listings where last_scraped_at <= today - duration (example: 7d, 1w, 3m). Required to enable track mode.",
    )
    parser.add_argument(
        "--track-limit",
        type=int,
        default=10,
        help="Limit number of tracked listings to process (default: 10).",
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
        help="Scrape 1 listing URL, upsert into Postgres, then print the parsed result as JSON",
    )
    args = parser.parse_args()

    if args.profile:
        os.environ["BOTASAURUS_PROFILE"] = str(args.profile).strip()

    is_tracking = bool(args.track_last_scraped_older_than)
    scraper = CarsomeIdCarsService(tracking=is_tracking)
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

        if is_tracking:
            # CarsomeID has no ad date, so tracking is based only on last_scraped_at.
            if args.details:
                raise ValueError("--details cannot be used with --track-last-scraped-older-than (tracking always scrapes details).")
            if args.start_url:
                raise ValueError("--start-url cannot be used with tracking (tracking reads from DB).")
            if args.max_pages != 1:
                raise ValueError("--max-pages cannot be used with tracking (tracking reads from DB).")
            if args.all_pages:
                raise ValueError("--all-pages cannot be used with tracking (tracking reads from DB).")
            if args.debug_detail_url:
                raise ValueError("--debug-detail-url cannot be used with tracking.")

            from scrap_carsomeid_cars_botasaurus.database import get_connection

            limit = int(args.track_limit or 0)
            if limit < 1:
                raise ValueError("--track-limit must be >= 1")

            now = datetime.now().date()
            cutoff = cutoff_date(now=now, older_than=str(args.track_last_scraped_older_than))

            conn = get_connection()
            cursor = conn.cursor()
            links: list[str] = []
            total = 0
            try:
                if args.track_count_only:
                    # Count-only ignores --track-limit: total matching the cutoff.
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM cars_scrap_carsomeid
                        WHERE status = 'active'
                          AND last_scraped_at::date <= %s
                          AND listing_url IS NOT NULL
                        """,
                        [cutoff],
                    )
                    row = cursor.fetchone()
                    total = int(row[0]) if row and row[0] is not None else 0
                else:
                    # Deterministic selection: oldest last_scraped_at first, oldest record next.
                    cursor.execute(
                        """
                        SELECT listing_id, listing_url
                        FROM cars_scrap_carsomeid
                        WHERE status = 'active'
                          AND last_scraped_at::date <= %s
                          AND listing_url IS NOT NULL
                        ORDER BY last_scraped_at ASC, created_at ASC, listing_id ASC
                        LIMIT %s
                        """,
                        [cutoff, limit],
                    )
                    db_rows = [row for row in cursor.fetchall() if row and row[0] and row[1]]
                    seen: set[str] = set()
                    for _listing_id, listing_url in db_rows:
                        url = str(listing_url).strip()
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        links.append(url)
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass

            if args.track_count_only:
                print(f"total_data: {total} listing")
                return 0

            summary = {
                "mode": "track",
                "older_than": args.track_last_scraped_older_than,
                "cutoff_date": str(cutoff),
                "count": len(links),
            }
            if args.track_dry_run:
                print(json.dumps(summary, indent=2, default=str))
                return 0

            results = scraper.scrape_listing_details(links)
            metrics = _summarize_results(results)
            stored = bool(metrics["sold_marked_count"] or metrics["upserted_count"])
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
                        "stored_to_db": stored,
                    },
                    indent=2,
                    default=str,
                )
            )
            # Do not silently report success when every tracked listing failed.
            return 1 if (metrics["errors_count"] and not stored) else 0

        if args.debug_detail_url:
            details = scraper.scrape_listing_details([args.debug_detail_url])
            print(json.dumps({"count": len(details), "sample": compact_sample(details, 1), "stored_to_db": True}, indent=2, default=str))
            return 0

        links = scraper.scrape_all_from_main(start_url=args.start_url, max_pages=args.max_pages, all_pages=args.all_pages)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
