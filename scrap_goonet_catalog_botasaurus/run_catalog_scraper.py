from __future__ import annotations

import argparse
import json
import sys

from scrap_goonet_catalog_botasaurus.catalog_service import (
    BASE_CATALOG_URL,
    SAMPLE_REFRESH_MAX_IDS,
    GoonetCatalogService,
    SampleRefreshRollbackError,
)


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
    # GNC-DETAIL-01: explicit opt-in write of ONE scraped detail into an EXISTING
    # row. OFF by default; only valid with --detail-url. The default
    # (``--detail-url --dry-run``) stays read-only and byte-for-byte unchanged.
    # The write verifies the canonical URL + strong page self-identity, preserves
    # the stored status verbatim (an existing ``missing`` row stays ``missing``),
    # never inserts a row, and requires a verified pre-write rollback snapshot.
    parser.add_argument(
        "--write-detail",
        action="store_true",
        help="With --detail-url, write the scraped detail into the EXISTING row (default: read-only)",
    )
    parser.add_argument("--model-url", default=None, help="Parse one catalog model URL for debugging")
    parser.add_argument("--make-url", default=None, help="Parse one catalog make URL for debugging")
    parser.add_argument("--max-makes", type=int, default=0, help="Max makes to process (0 = all)")
    parser.add_argument("--max-models", type=int, default=0, help="Max models to process (0 = all)")
    parser.add_argument("--max-specs", type=int, default=0, help="Max specs to process (0 = all)")
    parser.add_argument("--delay-seconds", type=float, default=0.0, help="Delay between requests in seconds")
    # GNC-FULL-02: explicit opt-in full-spec detail refresh. Only valid together
    # with --sync-all or --resume and OFF by default. When set, EVERY spec
    # enumerated on a model page is re-fetched/upserted (including rows already
    # complete with non-thumb media) instead of the default skip of complete
    # rows. Capped --max-specs still bounds each run; an interrupted flagged run
    # keeps a durable run-scoped progress marker so a resume does not silently
    # skip or re-fetch already-processed specs in the same refresh lineage.
    parser.add_argument(
        "--refresh-all-spec-details",
        action="store_true",
        help="With --sync-all/--resume, re-fetch/upsert EVERY spec (even already complete non-thumb rows)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print parsed data without DB writes")
    # GNC-IMG-03: bounded sample-refresh of EXISTING specs only. Exactly 1..3
    # explicit catalog_spec_id values must be named; there is no auto-select and
    # no sync-all. The default is READ-ONLY; --apply gates any DB write and then
    # requires a verified rollback snapshot first.
    parser.add_argument(
        "--refresh-sample-spec-id",
        action="append",
        default=None,
        metavar="CATALOG_SPEC_ID",
        help=f"Refresh media for one existing catalog spec id (repeat {1}-{SAMPLE_REFRESH_MAX_IDS} times)",
    )
    parser.add_argument("--apply", action="store_true", help="Actually write sample-refresh updates (default: read-only)")
    # GNC-FULL-03E-2a: fail-closed opt-in for the ONLY safe way to apply media to
    # rows the inventory proved have a gallery. Only valid with
    # --refresh-sample-spec-id; the default (unset) leaves every existing
    # invocation byte-for-byte unchanged.
    parser.add_argument(
        "--require-proven-gallery",
        action="store_true",
        help="With --refresh-sample-spec-id, only write ids proven by the read-only image inventory to have a gallery",
    )
    parser.add_argument("--rollback-from", default=None, metavar="PATH", help="Restore rows from a sample-refresh snapshot artifact")
    parser.add_argument(
        "--rollback-apply",
        action="store_true",
        help="Actually perform the rollback restore (default: dry-run report)",
    )
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
        "refresh_sample" if args.refresh_sample_spec_id else None,
        "rollback" if args.rollback_from else None,
    ]
    modes = [mode for mode in modes if mode]
    if not modes:
        parser.print_help()
        return
    if len(modes) > 1:
        parser.error("choose only one sync/debug mode")
    if args.apply and modes[0] != "refresh_sample":
        parser.error("--apply is only valid with --refresh-sample-spec-id")
    # GNC-FULL-03E-2a: the proven-gallery gate only applies to the sample refresh.
    if args.require_proven_gallery and modes[0] != "refresh_sample":
        parser.error("--require-proven-gallery is only valid with --refresh-sample-spec-id")
    if args.rollback_apply and modes[0] != "rollback":
        parser.error("--rollback-apply is only valid with --rollback-from")
    # GNC-FULL-02: the full-detail refresh opt-in is only meaningful for the
    # bulk sync/resume paths; never widen any other mode or the sample refresh.
    if args.refresh_all_spec_details and modes[0] not in ("sync_all", "resume"):
        parser.error("--refresh-all-spec-details is only valid with --sync-all or --resume")
    # GNC-DETAIL-01: the detail write opt-in is only valid with --detail-url and
    # is mutually exclusive with --dry-run (which must stay strictly read-only).
    if args.write_detail and modes[0] != "detail_url":
        parser.error("--write-detail is only valid with --detail-url")
    if args.write_detail and args.dry_run:
        parser.error("--write-detail cannot be combined with --dry-run")

    service = GoonetCatalogService()
    if modes[0] == "rollback":
        try:
            result = service.rollback_sample_refresh(args.rollback_from, apply=bool(args.rollback_apply))
        except SampleRefreshRollbackError as exc:
            # Fail closed: the transaction was rolled back and nothing restored.
            parser.error(str(exc))
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    if modes[0] == "refresh_sample":
        try:
            summary = service.refresh_sample_specs(
                args.refresh_sample_spec_id,
                apply=bool(args.apply),
                delay_seconds=args.delay_seconds,
                require_proven_gallery=bool(args.require_proven_gallery),
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return
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
                refresh_all_spec_details=bool(args.refresh_all_spec_details),
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
                    refresh_all_spec_details=bool(args.refresh_all_spec_details),
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

    if args.detail_url and args.write_detail:
        # GNC-DETAIL-01: explicit opt-in write. The service validates the URL,
        # requires the row to pre-exist, verifies strong page identity, preserves
        # the stored status verbatim, snapshots and guards the single write.
        try:
            summary = service.write_detail_url(args.detail_url, apply=True)
        except ValueError:
            # Bounded, non-leaky: a non-canonical/invalid URL must not be echoed.
            parser.error("--detail-url must be a canonical catalog detail URL")
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        # A failed/blocked apply must surface a NONZERO exit so an operator (or
        # wrapper) cannot mistake "missing"/"conflict"/"error"/"partial" for
        # success. ONLY a real write outcome (changed) or an already-current row
        # (unchanged) is success; the summary carries only the catalog_spec_id and
        # a fixed reason label — no URL/secret is disclosed.
        return_code = 0 if summary["result"] in ("changed", "unchanged") else 1
        if return_code:
            # Only bounded machine labels; never the page URL/body.
            print(f"detail write did not apply: result={summary['result']}", file=sys.stderr)
        raise SystemExit(return_code)

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
