from __future__ import annotations

import argparse
import json

from scrap_goonet_catalog_botasaurus.catalog_service import BASE_CATALOG_URL, GoonetCatalogService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape Goonet Exchange catalog specifications")
    parser.add_argument("--sync-all", action="store_true", help="Sync makes, models, spec links, and spec details")
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument("--resume", action="store_true", help="Resume the latest resumable catalog run")
    resume_group.add_argument("--resume-run-id", type=int, metavar="ID", help="Resume a specific resumable catalog run")
    parser.add_argument("--sync-makes", action="store_true", help="Sync catalog makes")
    parser.add_argument("--sync-models", action="store_true", help="Sync catalog models")
    parser.add_argument("--sync-spec-links", action="store_true", help="Sync catalog spec links")
    parser.add_argument("--sync-spec-details", action="store_true", help="Sync catalog spec details")
    parser.add_argument("--detail-url", default=None, help="Parse one catalog detail URL for debugging")
    parser.add_argument("--model-url", default=None, help="Parse one catalog model URL for debugging")
    parser.add_argument("--make-url", default=None, help="Parse one catalog make URL for debugging")
    parser.add_argument("--max-makes", type=int, default=0, help="Max makes to process (0 = all)")
    parser.add_argument("--max-models", type=int, default=0, help="Max models to process (0 = all)")
    parser.add_argument("--max-specs", type=int, default=0, help="Max specs to process (0 = all)")
    parser.add_argument("--delay-seconds", type=float, default=0.0, help="Delay between requests in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Print parsed data without DB writes")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    modes = [
        "sync_all" if args.sync_all else None,
        "resume" if args.resume or args.resume_run_id is not None else None,
        "sync_makes" if args.sync_makes else None,
        "sync_models" if args.sync_models else None,
        "sync_spec_links" if args.sync_spec_links else None,
        "sync_spec_details" if args.sync_spec_details else None,
        "detail_url" if args.detail_url else None,
        "model_url" if args.model_url else None,
        "make_url" if args.make_url else None,
    ]
    modes = [mode for mode in modes if mode]
    if not modes:
        parser.print_help()
        return
    if len(modes) > 1:
        parser.error("choose only one sync/debug mode")

    service = GoonetCatalogService()
    if args.resume or args.resume_run_id is not None:
        if args.dry_run:
            parser.error("--resume does not support --dry-run")
        try:
            result = service.resume(
                args.resume_run_id,
                max_makes=args.max_makes,
                max_models=args.max_models,
                max_specs=args.max_specs,
                delay_seconds=args.delay_seconds,
            )
        except RuntimeError as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.sync_all:
        if args.dry_run:
            parser.error("--sync-all --dry-run is not supported; use --make-url/--model-url/--detail-url dry-run checks")
        print(
            json.dumps(
                service.sync_all(
                    max_makes=args.max_makes,
                    max_models=args.max_models,
                    max_specs=args.max_specs,
                    delay_seconds=args.delay_seconds,
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if args.sync_makes:
        makes = service.parse_makes(service.fetch_html(BASE_CATALOG_URL), BASE_CATALOG_URL)
        if args.max_makes > 0:
            makes = makes[: args.max_makes]
        if args.dry_run:
            print(json.dumps(makes, indent=2, ensure_ascii=False))
        else:
            print(json.dumps({"synced": service.upsert_makes(makes)}, indent=2))
        return

    if args.sync_spec_details:
        if args.dry_run:
            print(json.dumps(service.get_pending_spec_details(args.max_specs), indent=2, ensure_ascii=False, default=str))
        else:
            print(
                json.dumps(
                    service.sync_pending_spec_details(max_specs=args.max_specs, delay_seconds=args.delay_seconds),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        return

    if args.make_url and args.dry_run:
        make_url = service.absolute_url(args.make_url, BASE_CATALOG_URL)
        make_slug = service.extract_make_slug(make_url)
        if not make_slug:
            parser.error("--make-url must be a catalog make URL")
        models = service.parse_models(
            service.fetch_html(make_url),
            {"make_slug": make_slug, "make_name": make_slug, "make_url": make_url},
        )
        if args.max_models > 0:
            models = models[: args.max_models]
        print(json.dumps(models, indent=2, ensure_ascii=False))
        return

    if args.model_url and args.dry_run:
        model_url = service.absolute_url(args.model_url, BASE_CATALOG_URL)
        model_slug = service.extract_model_slug(model_url)
        if not model_slug:
            parser.error("--model-url must be a catalog model URL")
        specs = service.parse_model_specs(
            service.fetch_html(model_url),
            {"model_slug": model_slug, "make_slug": model_slug.split("__", 1)[0], "model_url": model_url},
        )
        if args.max_specs > 0:
            specs = specs[: args.max_specs]
        print(json.dumps(specs, indent=2, ensure_ascii=False))
        return

    if args.detail_url and args.dry_run:
        detail_url = service.absolute_url(args.detail_url, BASE_CATALOG_URL)
        catalog_spec_id = service.extract_catalog_spec_id(detail_url)
        if not catalog_spec_id:
            parser.error("--detail-url must be a catalog detail URL")
        print(
            json.dumps(
                service.parse_spec_detail(service.fetch_html(detail_url), {"catalog_spec_id": catalog_spec_id, "spec_url": detail_url}),
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    print(
        json.dumps(
            {
                "mode": modes[0],
                "status": "not_implemented",
                "detail_url": args.detail_url,
                "model_url": args.model_url,
                "make_url": args.make_url,
                "max_makes": args.max_makes,
                "max_models": args.max_models,
                "max_specs": args.max_specs,
                "delay_seconds": args.delay_seconds,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
