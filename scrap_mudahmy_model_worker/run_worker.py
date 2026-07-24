from __future__ import annotations

import argparse
import json
import socket

from dotenv import load_dotenv

from scrap_mudahmy_model_worker.model_worker_service import MudahMyModelWorkerService

load_dotenv(override=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync and scrape Mudah listings by claimed model")
    parser.add_argument("--sync-master-data", action="store_true")
    parser.add_argument("--bootstrap-existing", action="store_true")
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
    if not args.claim_model:
        parser.error("use --sync-master-data, --bootstrap-existing, or --claim-model")
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
