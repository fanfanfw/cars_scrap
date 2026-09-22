"""Offline deterministic checks for listing failure classification (AS-CARFAIL-01..03).

No network, no real filesystem/DB writes: fake inline/sanitized drivers, patched
writers. Plain assert-based script following scripts/check_goonet_catalog_parser.py style.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scrap_carmudi_cars_botasaurus.carmudi_service import (
    CarmudiCarsService,
    classify_listing_failure as classify_carmudi_listing_failure,
)
from scrap_carlist_cars_botasaurus.carlist_service import (
    CarlistCarsService,
    classify_listing_failure as classify_carlist_listing_failure,
)
from scrap_mobil123_cars_botasaurus.mobil123_service import Mobil123CarsService
import scrap_carlist_cars_botasaurus.run_scraper as carlist_run
import scrap_carmudi_cars_botasaurus.run_scraper as carmudi_run

TURNSTILE_SNIPPET = 'iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js"'


class _ExceptionOnlyDriver:
    """run_js always raises -> token checks fall back to page_html (offline)."""

    def run_js(self, *args, **kwargs):  # pragma: no cover - trivial stub
        raise RuntimeError("offline fixture: no JS runtime")

    page_html = ""
    current_url = ""


class _HrefDriver:
    def __init__(self, hrefs):
        self._hrefs = list(hrefs)

    def run_js(self, script, args=None):
        if "a[href]" in str(script):
            return self._hrefs
        raise RuntimeError("offline fixture: only anchors supported")


def test_carlist_turnstile_classification():
    svc = CarlistCarsService()
    html = (
        '<html><body><h2>Just a moment...</h2>'
        f'<{TURNSTILE_SNIPPET}></iframe></body></html>'
    )
    driver = _ExceptionOnlyDriver()
    driver.page_html = html
    driver.current_url = "https://www.carlist.my/used-cars-for-sale"
    from cars_scrap_common.botasaurus_utils import is_cloudflare_challenge

    assert is_cloudflare_challenge(driver) is True
    exc = RuntimeError(
        "Blocked by Cloudflare Turnstile while scraping carlist. "
        "target_url='https://www.carlist.my/used-cars-for-sale' current_url='https://www.carlist.my/used-cars-for-sale'. "
        "Run with BOTASAURUS_PROMPT_ON_CLOUDFLARE=true to solve it manually (headful), "
        "or switch to a residential proxy / different IP."
    )
    assert classify_carlist_listing_failure(exc) == "cloudflare_challenge"


def test_carlist_73_byte_upstream_error_classification():
    svc = CarlistCarsService()
    message = "ERR_TUNNEL_CONNECTION_FAILED"
    body = f"<html><body>{message}</body></html>"
    # pad with a trailing space so the sanitized fixture body is exactly 73 bytes
    body = body + " " * max(0, 73 - len(body))
    assert len(body.encode("utf-8")) == 73
    driver = _ExceptionOnlyDriver()
    driver.page_html = body
    assert svc._is_http_error_page_botasaurus(driver) is True
    exc = RuntimeError(
        "Retryable error page while scraping carlist listing: "
        "current_url='https://www.carlist.my/used-cars-for-sale'"
    )
    assert classify_carlist_listing_failure(exc) == "upstream_error"


def test_carlist_exhausted_empty_listing():
    exc = RuntimeError(
        "Retryable soft block page while scraping carlist listing: "
        "current_url='https://www.carlist.my/used-cars-for-sale' reason='no_listing_links'"
    )
    assert classify_carlist_listing_failure(exc) == "empty_listing_page"


def test_carmudi_challenge_classification():
    svc = CarmudiCarsService()
    html = (
        '<html><body><h2>Just a moment...</h2><span>Verify you are human</span>'
        f'<{TURNSTILE_SNIPPET}></iframe></body></html>'
    )
    driver = _ExceptionOnlyDriver()
    driver.page_html = html
    driver.current_url = "https://www.carmudi.co.id/mobil-bekas-dijual/indonesia"
    from cars_scrap_common.botasaurus_utils import is_cloudflare_challenge

    assert is_cloudflare_challenge(driver) is True
    exc = RuntimeError(
        "Blocked by Cloudflare Turnstile while scraping carmudi. "
        "target_url='https://www.carmudi.co.id/mobil-bekas-dijual/indonesia' current_url='https://www.carmudi.co.id/mobil-bekas-dijual/indonesia'. "
        "Run with BOTASAURUS_PROMPT_ON_CLOUDFLARE=true to solve it manually (headful), "
        "or switch to a residential proxy / different IP."
    )
    assert classify_carmudi_listing_failure(exc) == "cloudflare_challenge"


def test_carlist_valid_anchors():
    svc = CarlistCarsService()
    hrefs = [
        "https://www.carlist.my/used-cars-for-sale/12345",
        "https://www.carlist.my/no-numeric-here",
        "https://example.com/other-domain/99999",
    ]
    driver = _HrefDriver(hrefs)
    assert svc._extract_listing_urls_botasaurus(driver) == [
        "https://www.carlist.my/used-cars-for-sale/12345"
    ]


def test_carmudi_valid_anchors():
    svc = CarmudiCarsService()
    hrefs = [
        "https://www.carmudi.co.id/dijual/toyota-avanza-nunggal/335110",
        "https://www.carmudi.co.id/mobil-bekas-dijual/indonesia",
        "https://example.com/other/99999",
    ]
    driver = _HrefDriver(hrefs)
    anchors = svc._extract_listing_urls_botasaurus(driver)
    assert all(
        svc._is_listing_detail_url(url) for url in anchors
    ), anchors
    assert anchors == [
        "https://www.carmudi.co.id/dijual/toyota-avanza-nunggal/335110"
    ]


# CARMUDI-TRACK-01C: sold-page marker regression (offline, no browser/network/DB).
def test_carmudi_sold_markers_and_branch_contract():
    svc = CarmudiCarsService()

    def sold(html: str) -> bool:
        driver = _ExceptionOnlyDriver()
        driver.page_html = html
        return svc._is_sold_page_botasaurus(driver)

    # normalized exact sentence (upper-cased + trailing period) and existing terjual
    assert sold("<h1>MOBIL INI SAAT INI TIDAK TERSEDIA.</h1>") is True
    assert sold("<p>Mobil ini telah terjual</p>") is True
    # generic 404, redirect-ish, and normal pages must never be sold
    assert sold("<h1>404 Not Found</h1><p>Page not found</p>") is False
    assert sold("<p>Redirected elsewhere</p>") is False
    assert sold("<h1>Toyota Avanza 2019</h1><p>Rp 180.000.000</p>") is False

    # CARMUDI-TRACK-01B source contract: sold only when explicit sold page
    source = (REPO_ROOT / "scrap_carmudi_cars_botasaurus" / "carmudi_service.py").read_text()
    assert "should_mark_sold = bool(is_sold_page)" in source
    assert "tracking_mode and (is_not_found_page or redirected_off_detail)" not in source


# MOBIL123-TRACK-02: sold-page marker regression (offline, no browser/network/DB).
def test_mobil123_sold_markers_and_branch_contract():
    svc = Mobil123CarsService()

    def sold(html: str) -> bool:
        driver = _ExceptionOnlyDriver()
        driver.page_html = html
        return svc._is_sold_page_botasaurus(driver)

    # existing explicit Mobil123 sold markers must stay true
    assert sold("<p>Mobil ini telah terjual</p>") is True
    assert sold("<p>Iklan tidak tersedia</p>") is True
    # MOBIL123-TRACK-02B: live evidence phrase, case-insensitive + punctuation.
    assert sold("<p>MOBIL INI SAAT INI TIDAK TERSEDIA.</p>") is True
    # generic 404, redirect-ish, and normal pages must never be sold
    assert sold("<h1>404 Not Found</h1><p>Page not found</p>") is False
    assert sold("<p>Redirected elsewhere</p>") is False
    assert sold("<h1>Toyota Avanza 2019</h1><p>Rp 180.000.000</p>") is False

    # MOBIL123-TRACK-02 source contract: sold only when explicit sold page
    source = (REPO_ROOT / "scrap_mobil123_cars_botasaurus" / "mobil123_service.py").read_text()
    assert "should_mark_sold = bool(is_sold_page)" in source
    assert "tracking_mode and (is_not_found_page or redirected_off_detail)" not in source


# OLX-TRACK-02: explicit sold marker + dedicated status-only persistence (offline).
def test_olx_sold_marker_and_persistence_contract():
    import types
    from scrap_olx_cars_botasaurus.olx_service import OlxCarsService

    svc = OlxCarsService()
    svc_source = (REPO_ROOT / "scrap_olx_cars_botasaurus" / "olx_service.py").read_text()

    def make_driver(html: str, current_url: str = "https://www.olx.co.id/item/x-iid-1"):
        driver = _ExceptionOnlyDriver()
        driver.page_html = html
        driver.current_url = current_url
        driver.get_via = lambda *a, **k: None
        driver.google_get = lambda *a, **k: None
        return driver

    def sold(html: str) -> bool:
        return svc._is_sold_page_botasaurus(make_driver(html))

    MARKER = "kami tidak menemukan apa pun yang cocok dengan pencarian ini"
    SEARCH_URL = "https://www.olx.co.id/indonesia_g1000001/mobil-bekas_c198"
    # exact marker: mixed case, wrapped/newline whitespace, trailing punctuation
    assert sold("<h1>Kami Tidak Menemukan\n  Apa Pun   Yang Cocok Dengan Pencarian Ini.</h1>") is True
    # generic 404 / redirect / normal pages must never be sold
    assert sold("<h1>404 Not Found</h1><p>Halaman tidak ditemukan</p>") is False
    assert sold("<p>Redirected elsewhere</p>") is False
    assert sold("<h1>Toyota Raize 2025</h1><p>Rp 228.000.000</p>") is False
    # separation: marker is not part of the generic not-found tokens
    assert svc._is_listing_not_found_botasaurus(make_driver(MARKER)) is False

    # source order: marker detected after navigation and before the detail waits
    body = svc_source[svc_source.index("def _scrape_single_detail_botasaurus"):]
    assert body.index("is_sold_page = self._is_sold_page_botasaurus(driver)") < body.index("wait_for_css(")
    # same-domain non-detail current_url counts as redirected_off_detail
    assert "not self._is_listing_detail_url(current_url)" in body
    assert "not self._is_on_target_domain(current_url)" not in body

    # branch contract: fresh marker errors, tracking marker returns sold
    cfg = types.SimpleNamespace(
        bypass_cloudflare=False, prompt_on_cloudflare=False, detail_wait_seconds=0.01, poll_interval_seconds=0.01
    )
    try:
        svc._scrape_single_detail_botasaurus(
            make_driver(MARKER), url="https://www.olx.co.id/item/x-iid-1", cfg=cfg, listing_id="1", tracking_mode=False
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("fresh marker must raise, not mark sold")
    tracked = svc._scrape_single_detail_botasaurus(
        make_driver(MARKER), url="https://www.olx.co.id/item/x-iid-1", cfg=cfg, listing_id="1", tracking_mode=True
    )
    assert tracked.get("status") == "sold" and tracked.get("listing_id") == "1"

    # same-domain search/notfound redirect (no marker) -> ambiguous error, never sold
    try:
        svc._scrape_single_detail_botasaurus(
            make_driver("<h1>Cari mobil bekas</h1>", SEARCH_URL),
            url="https://www.olx.co.id/item/x-iid-1",
            cfg=cfg,
            listing_id="1",
            tracking_mode=True,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("same-domain non-detail redirect must error, not sell")
    # exact marker on same-domain non-detail still wins (priority) -> sold
    same_domain_marker = svc._scrape_single_detail_botasaurus(
        make_driver(MARKER, SEARCH_URL), url="https://www.olx.co.id/item/x-iid-1", cfg=cfg, listing_id="1", tracking_mode=True
    )
    assert same_domain_marker.get("status") == "sold"

    # dedicated sold UPDATE only touches listing_url/status/version/last_scraped_at
    sold_sql = svc_source.split("sold_update_sql = ", 1)[1].split('"""', 2)[1]
    assert "status = 'sold'" in sold_sql and "listing_id = %s" in sold_sql
    for field in ("variant", "transmission", "fuel_type"):
        assert field not in sold_sql, field


# OLX-TRACK-04B: tracking image-shrink safeguard (offline, no browser/network/DB).
def test_olx_tracking_images_no_shrink_contract():
    from scrap_olx_cars_botasaurus.olx_service import olx_images_upsert_expression

    # regular scrape: historical behavior must stay byte-identical
    assert olx_images_upsert_expression(tracking_mode=False) == "COALESCE(EXCLUDED.images, cars_scrap_olx.images)"
    tracking = olx_images_upsert_expression(tracking_mode=True)
    # real text[] cardinality guard: equal-or-more accepts new, else keeps stored old
    for token in (
        "cardinality(cars_scrap_olx.images)",
        "cardinality(EXCLUDED.images) >= cardinality(cars_scrap_olx.images)",
        "ELSE cars_scrap_olx.images END",
    ):
        assert token in tracking, token
    # null/empty stored old -> accept the newly extracted list
    assert "cars_scrap_olx.images IS NULL OR cardinality(cars_scrap_olx.images) = 0" in tracking
    # wired into the upsert, keyed by the tracking variant (no extra DB fetch)
    svc_source = (REPO_ROOT / "scrap_olx_cars_botasaurus" / "olx_service.py").read_text()
    assert "images = {images_update_sql}," in svc_source
    assert "images_update_sql = olx_images_upsert_expression(tracking_mode=bool(listing_id_by_url))" in svc_source


# OLX-RETRY-02: whole-run retry wrapper over only erred URLs (offline, no browser/network/DB).
def test_olx_run_retry_wrapper_contract():
    import scrap_olx_cars_botasaurus.olx_service as olx_module

    svc = olx_module.OlxCarsService()
    svc._logged_proxy_ip = True
    A, B, C = (f"https://www.olx.co.id/item/{tag}-iid-{idx}" for tag, idx in (("a", 1), ("b", 2), ("c", 3)))
    calls: list[tuple[list[str], str]] = []
    resets: list[int] = []

    def fake_details(links, *, listing_id_by_url=None, listing_meta_by_url=None, allow_revive_sold=False):
        calls.append((list(links), os.environ.get("BOTASAURUS_PROFILE", "")))
        out = []
        for url in links:
            base = svc._build_error_result(url, listing_id=listing_id_by_url.get(url) if listing_id_by_url else None)
            if url == A:
                out.append({**base, "error": None, "status": "active", "brand": "Toyota"})
            elif url == C:
                out.append({**base, "error": None, "status": "sold"})
            else:
                out.append({**base, "error": "boom_b"})  # B keeps failing across attempts
        return out

    svc.scrape_listing_details = fake_details  # fake method, no class binding
    env = {"OLX_RUN_MAX_ATTEMPTS": "2", "OLX_RUN_RETRY_WAIT_SECONDS": "0", "BOTASAURUS_PROFILE": "olx_track"}
    with unittest.mock.patch.dict(os.environ, env, clear=False), unittest.mock.patch.object(
        olx_module, "reset_cached_proxy", lambda: resets.append(1)
    ):
        results = svc.scrape_listing_details_with_run_retries(
            [A, B, C], listing_id_by_url={A: "1", B: "2", C: "3"}, listing_meta_by_url={B: {"information_ads": "x"}}
        )

    assert [c[0] for c in calls] == [[A, B, C], [B]], calls  # attempt1 all, attempt2 only errors
    assert calls[1][1].startswith("olx_track_run_r2_"), calls[1][1]  # attempt2 fresh unique profile
    assert len(resets) == 4 and svc._logged_proxy_ip is False  # reset before/after each attempt
    assert [r["listing_url"] for r in results] == [A, B, C]  # original order preserved
    assert results[0]["brand"] == "Toyota" and results[0]["error"] is None
    assert results[2]["status"] == "sold" and results[2]["error"] is None  # sold never retried
    assert results[1]["error"] == "boom_b"  # failed twice -> final error wins

    # a missing result becomes an error, and attempts are capped (no infinite loop)
    svc.scrape_listing_details = lambda links, **kwargs: []
    with unittest.mock.patch.dict(os.environ, env, clear=False), unittest.mock.patch.object(
        olx_module, "reset_cached_proxy", lambda: None
    ):
        missing = svc.scrape_listing_details_with_run_retries([A, B], listing_id_by_url={A: "1", B: "2"})
    assert [r["error"] for r in missing] == ["missing_result_from_run", "missing_result_from_run"]


# CARSOME-TRACK-02/03: status enum + bounded retry + track count/dry-run (offline, no DB writes).
def test_carsomeid_status_retry_and_track_cli_contract():
    import types
    import requests
    import scrap_carsomeid_cars_botasaurus.carsomeid_service as cs
    import scrap_carsomeid_cars_botasaurus.database as cs_db
    import scrap_carsomeid_cars_botasaurus.run_scraper as cs_run
    c = cs.classify_carsomeid_status
    for name, label, expected in (
        ("Listed", "In Transaction", "active"), ("PreListed", "In Reconditioning", "active"),
        ("Unlisted", None, "sold"), (None, "Sold", "sold"), ("Listed", "Unavailable", "sold"),
    ):
        assert c(sale_status_name=name, car_status_label=label) == expected, (name, label)
    for name, label in ((None, None), ("", ""), ("Weird", None), ("Weird", "Whatever")):
        try:
            c(sale_status_name=name, car_status_label=label)
        except RuntimeError:
            pass
        else:
            raise AssertionError((name, label))
    svc = cs.CarsomeIdCarsService()
    svc.logger.setLevel(50)
    URL = "https://www.carsome.id/beli-mobil-bekas/a/b/c/x"
    VALID = {"code": "0000", "data": {"car": {"carNo": "X", "saleStatusName": "Listed"}}}
    ns = lambda code: types.SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: code)
    err = lambda s: requests.HTTPError(response=types.SimpleNamespace(status_code=s))

    def attempts(seq):
        box = list(seq)

        def fake(*_a, **_k):
            item = box.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with unittest.mock.patch.object(cs.requests, "post", fake), unittest.mock.patch.dict(
            os.environ, {"CARSOMEID_DETAIL_RETRY_WAIT_SECONDS": "0"}, clear=False
        ):
            try:
                svc.fetch_detail_api(URL)
                raised = False
            except Exception:
                raised = True
        return len(seq) - len(box), raised

    assert attempts([requests.Timeout(), ns(VALID)]) == (2, False)  # timeout then success
    assert (attempts([err(429), ns(VALID)]), attempts([err(503), ns(VALID)])) == ((2, False), (2, False))
    assert (attempts([err(400), ns(VALID)]), attempts([ns({"code": "9999", "data": {}}), ns(VALID)])) == ((1, True), (1, True))

    class Conn:
        def cursor(self): return self
        def execute(self, sql, params=None): self.sql = " ".join(str(sql).split()).upper(); self.params = params
        def fetchone(self): return (5,)
        def fetchall(self): return [("1", URL), ("2", URL + "2")]
        def close(self): pass

    conn, base = Conn(), ["run_scraper", "--track-last-scraped-older-than", "7d"]

    def cli(*extra):
        sys.argv = [*base, *extra]
        buf = io.StringIO()
        with unittest.mock.patch.object(cs_db, "get_connection", lambda: conn), contextlib.redirect_stdout(buf):
            rc = cs_run.main()
        return rc, buf.getvalue(), conn

    original = sys.argv
    try:
        rc, out, c1 = cli("--track-count-only")
        assert rc == 0 and out.strip() == "total_data: 5 listing" and "COUNT(*)" in c1.sql and "LIMIT" not in c1.sql
        rc, out, c2 = cli("--track-limit", "3", "--track-dry-run")
        assert rc == 0 and "LIMIT" in c2.sql and c2.params[-1] == 3 and json.loads(out)["count"] == 2
        for extra in (["--start-url", "x"], ["--details"], ["--all-pages"], ["--max-pages", "2"], ["--debug-detail-url", "x"]):
            try:
                cli(*extra)
            except ValueError:
                pass
            else:
                raise AssertionError(extra)
    finally:
        sys.argv = original


def _run_cli_failure(run_module, service_cls, *, failure_message, db_modules):
    def raise_failure(self, start_url=None, max_pages=1):
        raise failure_message

    original = service_cls.scrape_all_from_main
    service_cls.scrape_all_from_main = raise_failure

    def db_guard(*args, **kwargs):
        raise AssertionError("DB access attempted on listing failure")

    patched: list[tuple[object, object]] = []
    original_argv = sys.argv
    try:
        for module in db_modules:
            get_connection = getattr(module, "get_connection", None)
            if get_connection is not None:
                setattr(module, "get_connection", db_guard)
                patched.append((module, get_connection))
        sys.argv = ["run_scraper", "--start-url", "https://fixture.invalid/used-cars"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_module.main()
        return rc, buf.getvalue()
    finally:
        sys.argv = original_argv
        for module, get_connection in patched:
            setattr(module, "get_connection", get_connection)
        service_cls.scrape_all_from_main = original


def test_carlist_cli_nonzero_no_db():
    import scrap_carlist_cars_botasaurus.database as carlist_db
    import scrap_carlist_cars_botasaurus.carlist_service as carlist_service_module

    rc, out = _run_cli_failure(
        carlist_run,
        CarlistCarsService,
        failure_message=RuntimeError(
            "Retryable soft block page while scraping carlist listing: "
            "current_url='https://www.carlist.my/used-cars-for-sale' reason='no_listing_links'"
        ),
        db_modules=[carlist_db, carlist_service_module],
    )
    assert rc == 1
    payload = json.loads(out)
    assert payload["run_status"] == "failed"
    assert payload["failure_stage"] == "listing"
    assert payload["failure_type"] == "empty_listing_page"
    assert payload["listing_count"] == 0
    assert payload["stored_to_db"] is False


def test_carmudi_cli_nonzero_no_db():
    import scrap_carmudi_cars_botasaurus.database as carmudi_db
    import scrap_carmudi_cars_botasaurus.carmudi_service as carmudi_service_module

    rc, out = _run_cli_failure(
        carmudi_run,
        CarmudiCarsService,
        failure_message=RuntimeError(
            "Retryable soft block page while scraping carmudi listing: "
            "current_url='https://www.carmudi.co.id/mobil-bekas-dijual/indonesia' reason='no_listing_links'"
        ),
        db_modules=[carmudi_db, carmudi_service_module],
    )
    assert rc == 1
    payload = json.loads(out)
    assert payload["run_status"] == "failed"
    assert payload["failure_stage"] == "listing"
    assert payload["failure_type"] == "empty_listing_page"
    assert payload["listing_count"] == 0
    assert payload["stored_to_db"] is False


def _debug_dir_files(kind: str) -> int:
    """Count real files in the production debug capture dir (repo-relative)."""
    base = REPO_ROOT / "botasaurus_test" / "output" / f"{kind}_debug"
    if not base.is_dir():
        return 0
    return sum(1 for path in base.iterdir() if path.is_file())


def _run_capture_checks(svc_class, kind: str) -> bool:
    svc = svc_class()
    html = "<html><body>offline fixture</body></html>"
    slug = f"{kind}_listing_soft_block"

    def fresh_driver(current_url):
        driver = _ExceptionOnlyDriver()
        driver.page_html = html
        driver.current_url = current_url
        return driver

    real_before = _debug_dir_files("carlist") + _debug_dir_files("carmudi")

    payload = dict(slug=slug, reason="reason_from_fixture", url="fixture.invalid/used-cars")
    with tempfile.TemporaryDirectory(prefix="clf_check_") as tmp:
        tmp_path = pathlib.Path(tmp).resolve()
        original_cwd = os.getcwd()
        try:
            # Production out_dir is relative ("botasaurus_test/output/<svc>_debug");
            # chdir makes actual write paths land inside the temp dir only.
            os.chdir(tmp_path)
            # same fingerprint (sha1(slug|reason|current_url)) twice -> one HTML+JSON pair
            svc._save_debug_capture_botasaurus(fresh_driver("https://fixture.invalid/used-cars"), **payload)
            svc._save_debug_capture_botasaurus(fresh_driver("https://fixture.invalid/used-cars"), **payload)
            # distinct fingerprint (different current_url) -> another full pair
            time.sleep(1.05)  # production filenames are second-resolution timestamped
            svc._save_debug_capture_botasaurus(fresh_driver("https://fixture.invalid/other"), **payload)
            out_dir = tmp_path / "botasaurus_test" / "output" / f"{kind}_debug"
            files = sorted(path.name for path in out_dir.iterdir())
            assert len(files) == 4, files
            html_files = [name for name in files if not name.endswith(".json")]
            json_files = [name for name in files if name.endswith(".json")]
            assert len(html_files) == 2, files
            assert len(json_files) == 2, files
            assert all(path.is_file() for path in out_dir.iterdir()), files
        finally:
            os.chdir(original_cwd)
    # TemporaryDirectory cleanup removes the whole tree
    assert not pathlib.Path(tmp).exists(), "temp capture dir not cleaned up"
    # no writes under real repo debug dirs
    real_after = _debug_dir_files("carlist") + _debug_dir_files("carmudi")
    assert real_after == real_before, (real_before, real_after)
    return True


def test_capture_ceiling_carlist():
    assert _run_capture_checks(CarlistCarsService, "carlist")


def test_capture_ceiling_carmudi():
    assert _run_capture_checks(CarmudiCarsService, "carmudi")


def main() -> int:
    checks = (
        test_carlist_turnstile_classification,
        test_carlist_73_byte_upstream_error_classification,
        test_carlist_exhausted_empty_listing,
        test_carmudi_challenge_classification,
        test_carlist_valid_anchors,
        test_carmudi_valid_anchors,
        test_carmudi_sold_markers_and_branch_contract,
        test_mobil123_sold_markers_and_branch_contract,
        test_olx_sold_marker_and_persistence_contract,
        test_olx_tracking_images_no_shrink_contract,
        test_olx_run_retry_wrapper_contract,
        test_carsomeid_status_retry_and_track_cli_contract,
        test_capture_ceiling_carlist,
        test_capture_ceiling_carmudi,
        test_carlist_cli_nonzero_no_db,
        test_carmudi_cli_nonzero_no_db,
    )
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - surface failure, stay offline
            failed += 1
            print(f"FAIL {check.__name__}: {exc}")
        else:
            print(f"PASS {check.__name__}")
    print("network_access: never_performed; db_access: guarded_and_never_invoked")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
