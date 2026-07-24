from __future__ import annotations

import argparse
import json
import socket

from dotenv import load_dotenv

from scrap_mudahmy_model_worker.model_worker_service import MudahMyModelWorkerService

load_dotenv(override=True)


def print_status_table(payload: dict) -> None:
    print("STATUS SUMMARY")
    print(f"{'Status':<22} {'Count':>8}")
    print(f"{'-' * 22} {'-' * 8}")
    for status, count in payload["summary"].items():
        print(f"{status:<22} {count:>8}")

    models = payload["models"]
    if not models:
        print("\nNo models found.")
        return

    print(f"\nMODELS (filter: {payload['filter']})")
    columns = (
        ("Brand", "make_name", 18),
        ("Model", "model_name", 22),
        ("Status", "scrape_status", 18),
        ("Count", "last_listing_scrape_count", 7),
        ("Page", "page", 9),
        ("Worker", "listing_claimed_by", 12),
        ("Error", "last_listing_scrape_error", 30),
    )
    print(" ".join(f"{title:<{width}}" for title, _, width in columns))
    print(" ".join("-" * width for _, _, width in columns))
    for model in models:
        row = dict(model)
        row["page"] = f"{row.get('listing_last_page') or 0}/{row.get('listing_total_pages') or '-'}"
        values = []
        for _, key, width in columns:
            value = "-" if row.get(key) is None else str(row[key]).replace("\n", " ")
            if len(value) > width:
                value = value[: width - 1] + "…"
            values.append(f"{value:<{width}}")
        print(" ".join(values))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync and scrape Mudah listings by claimed model")
    parser.add_argument("--sync-master-data", action="store_true")
    parser.add_argument("--bootstrap-existing", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument(
        "--status-filter",
        choices=("pending", "running", "success", "error", "bootstrap_existing"),
    )
    parser.add_argument("--status-limit", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of status table")
    parser.add_argument("--claim-model", action="store_true")
    parser.add_argument("--worker-id", default=socket.gethostname())
    parser.add_argument("--lease-minutes", type=int, default=120)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    parser.add_argument("--page-retries", type=int, default=3)
    parser.add_argument("--details", action="store_true")
    args = parser.parse_args()

    service = MudahMyModelWorkerService()
    if args.sync_master_data:
        print(json.dumps({"mode": "sync_master_data", **service.sync_master_data()}, indent=2))
        return
    if args.bootstrap_existing:
        print(json.dumps({"mode": "bootstrap_existing", **service.bootstrap_existing_models()}, indent=2))
        return
    if args.status:
        if args.status_limit <= 0:
            parser.error("--status-limit must be > 0")
        payload = service.get_status(args.status_filter, args.status_limit)
        if args.json:
            print(json.dumps({"mode": "status", **payload}, indent=2, default=str))
        else:
            print_status_table(payload)
        return
    if not args.claim_model:
        parser.error("use --sync-master-data, --bootstrap-existing, --status, or --claim-model")
    if not str(args.worker_id).strip():
        parser.error("--worker-id cannot be empty")
    if args.lease_minutes <= 0:
        parser.error("--lease-minutes must be > 0")
    if args.max_pages < 0:
        parser.error("--max-pages must be >= 0")
    if args.page_retries <= 0:
        parser.error("--page-retries must be > 0")

    model = service.claim_model(str(args.worker_id).strip(), args.lease_minutes)
    if not model:
        print(json.dumps({"mode": "claim_model", "claimed": False}, indent=2))
        return
    result = service.scrape_claimed_model(
        model,
        str(args.worker_id).strip(),
        args.max_pages,
        args.details,
        args.page_retries,
    )
    print(json.dumps({"mode": "claim_model", "claimed": True, **result}, indent=2, default=str))


if __name__ == "__main__":
    main()
