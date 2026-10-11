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


# CARMUDI-CF-02: detector must recognize the real Carmudi interstitial signals
# (title-only, hidden cf-turnstile-response control, "Performing security
# verification") and keep the legacy signals (offline, no browser/network/DB).
def test_carmudi_cf02_detector_signals():
    from cars_scrap_common.botasaurus_utils import (
        _detect_cloudflare_signals,
        cloudflare_signals_from_facts,
        is_cloudflare_challenge,
    )

    # title-only challenge (no iframe, no body token)
    title_only = _ExceptionOnlyDriver()
    title_only.page_html = "<html><head><title>Just a moment...</title></head><body></body></html>"
    assert is_cloudflare_challenge(title_only) is True
    assert "title:just a moment" in _detect_cloudflare_signals(title_only)

    # hidden cf-turnstile-response control only (the live Carmudi shape: no iframe)
    control_only = _ExceptionOnlyDriver()
    control_only.page_html = (
        '<html><body><div style="display: grid;">'
        '<input type="hidden" name="cf-turnstile-response" id="cf-chl-widget-x_response">'
        "</div></body></html>"
    )
    assert is_cloudflare_challenge(control_only) is True
    assert "control:cf-turnstile-response" in _detect_cloudflare_signals(control_only)

    # new body copy
    new_copy = _ExceptionOnlyDriver()
    new_copy.page_html = "<html><body><h2>Performing security verification</h2></body></html>"
    assert is_cloudflare_challenge(new_copy) is True
    assert "text:performing security verification" in _detect_cloudflare_signals(new_copy)

    # legacy signals retained: "Verify you are human" + Turnstile iframe
    legacy = _ExceptionOnlyDriver()
    legacy.page_html = (
        f"<html><body><span>Verify you are human</span><{TURNSTILE_SNIPPET}></iframe></body></html>"
    )
    assert is_cloudflare_challenge(legacy) is True
    assert "text:verify you are human" in _detect_cloudflare_signals(legacy)
    assert "iframe:turnstile" in _detect_cloudflare_signals(legacy)

    # normal page must never be flagged
    normal = _ExceptionOnlyDriver()
    normal.page_html = "<html><head><title>Toyota Avanza 2019</title></head><body>Rp 180.000.000</body></html>"
    assert is_cloudflare_challenge(normal) is False
    assert _detect_cloudflare_signals(normal) == ()

    # the live DOM facts path (dict from run_js) yields the same labels
    facts = cloudflare_signals_from_facts(
        title="Just a moment...",
        text="Performing security verification",
        has_control=True,
        has_iframe=False,
    )
    assert "title:just a moment" in facts
    assert "text:performing security verification" in facts
    assert "control:cf-turnstile-response" in facts
    assert cloudflare_signals_from_facts() == ()


def test_cf02_sanitizer_drops_url_secrets():
    from cars_scrap_common.botasaurus_utils import sanitize_text_for_log, sanitize_url_for_log

    dirty = "https://user:pass@www.carmudi.co.id/path?a=1&token=SECRET#frag"
    clean = sanitize_url_for_log(dirty)
    assert clean == "https://www.carmudi.co.id/path"
    assert "SECRET" not in clean and "token" not in clean and "pass" not in clean
    assert sanitize_url_for_log("") == ""
    # malformed/out-of-range ports must not raise while building a log line,
    # and must not leave the bogus port fragment in the output
    for bad in ("https://host:notaport/p?x=1", "https://host:99999/p?x=1"):
        safe = sanitize_url_for_log(bad)
        assert "notaport" not in safe and "99999" not in safe, safe
        assert "x=1" not in safe, safe
    # embedded URLs inside error text are redacted too
    redacted = sanitize_text_for_log("boom at https://x/y?secret=1 more")
    assert "secret=1" not in redacted


def test_cf02_bounded_solver_semantics():
    import logging
    import types
    from cars_scrap_common.botasaurus_utils import attempt_solve_cloudflare_in_session

    # driver whose page always looks challenged; we record solver invocations
    class _ChallengedDriver:
        current_url = "https://www.carmudi.co.id/mobil-bekas-dijual/indonesia?token=SECRET"
        page_html = "<html><head><title>Just a moment...</title></head><body></body></html>"

        def __init__(self):
            self.calls = 0

        def run_js(self, *a, **k):
            raise RuntimeError("offline fixture: no JS runtime")

        def detect_and_bypass_cloudflare(self):
            self.calls += 1

    logger = logging.getLogger("cf02_test")
    logger.addHandler(logging.NullHandler())

    # never clears -> exactly max_attempts solver invocations, then False
    driver = _ChallengedDriver()
    sleep_calls: list[float] = []
    resolved = attempt_solve_cloudflare_in_session(
        driver,
        target_url="https://www.carmudi.co.id/x?token=SECRET",
        logger=logger,
        service_label="carmudi",
        max_attempts=3,
        post_wait_seconds=0.5,
        sleeper=sleep_calls.append,
    )
    assert resolved is False
    assert driver.calls == 3, driver.calls
    assert sleep_calls == [0.5, 0.5, 0.5], sleep_calls

    # a solver that clears the challenge on the first call returns True after 1
    class _ClearingDriver(_ChallengedDriver):
        challenged = True

        @property
        def page_html(self):
            return (
                "<html><head><title>Just a moment...</title></head><body></body></html>"
                if self.challenged
                else "<html><head><title>Carmudi</title></head><body>Rp 1</body></html>"
            )

    clearing = _ClearingDriver()
    invocations: list[int] = []

    def solve():
        invocations.append(1)
        clearing.challenged = False

    assert attempt_solve_cloudflare_in_session(
        clearing,
        target_url="https://www.carmudi.co.id/x?token=SECRET",
        logger=logger,
        service_label="carmudi",
        max_attempts=3,
        post_wait_seconds=0.0,
        sleeper=sleep_calls.append,
        solver=solve,
    ) is True
    assert len(invocations) == 1

    # no-op when there is no challenge: solver never called (bounded, no loop)
    clean = _ExceptionOnlyDriver()
    clean.page_html = "<html><body>Rp 1.000.000</body></html>"
    never = types.SimpleNamespace(calls=0)
    assert attempt_solve_cloudflare_in_session(
        clean,
        target_url="https://www.carmudi.co.id/x",
        logger=logger,
        service_label="carmudi",
        max_attempts=5,
        post_wait_seconds=0.0,
        solver=lambda: setattr(never, "calls", never.calls + 1),
        sleeper=sleep_calls.append,
    ) is True
    assert never.calls == 0


def test_cf02_per_attempt_deadline():
    """Offline proof that a per-attempt wall-clock deadline interrupts a blocking
    solver, restores any prior SIGALRM handler/timer exactly, keeps attempt counts
    exact, and leaks no alarm (no browser/network/DB)."""
    import logging
    import signal
    import threading
    import time as _time

    from cars_scrap_common.botasaurus_utils import (
        CloudflareSolveTimeoutError,
        _arm_attempt_deadline,
        attempt_solve_cloudflare_in_session,
    )

    # This test requires POSIX SIGALRM + main-thread execution (Linux runtime).
    assert hasattr(signal, "SIGALRM"), "SIGALRM required for CARMUDI-CF-02 deadline test"
    assert threading.current_thread() is threading.main_thread()

    class _ChallengedDriver:
        current_url = "https://www.carmudi.co.id/x?token=SECRET"
        page_html = "<html><head><title>Just a moment...</title></head><body></body></html>"

        def run_js(self, *a, **k):
            raise RuntimeError("offline fixture: no JS runtime")

    logger = logging.getLogger("cf02_deadline_test")
    logger.addHandler(logging.NullHandler())

    # (1) blocking solver is interrupted by the deadline; attempts stay exact.
    calls: list[int] = []

    def blocking_solver():
        calls.append(1)
        _time.sleep(30)  # would hang without the deadline; SIGALRM interrupts

    started = _time.monotonic()
    resolved = attempt_solve_cloudflare_in_session(
        _ChallengedDriver(),
        target_url="https://www.carmudi.co.id/x?token=SECRET",
        logger=logger,
        service_label="carmudi",
        max_attempts=2,
        post_wait_seconds=0.0,
        attempt_timeout_seconds=0.25,
        sleeper=lambda _s: None,
        solver=blocking_solver,
    )
    elapsed = _time.monotonic() - started
    assert resolved is False
    assert len(calls) == 2, calls  # interrupted twice, never a 3rd (no unbounded loop)
    assert elapsed < 10, elapsed  # deadline actually fired (far below the 30s sleep)

    # (2) timeout raises the dedicated exception type for callers that prefer it.
    raised: list[str] = []
    try:
        with _arm_attempt_deadline(0.1) as armed:
            assert armed is True
            _time.sleep(5)
    except CloudflareSolveTimeoutError:
        raised.append("timeout")
    assert raised == ["timeout"], raised

    # (3) prior SIGALRM handler and timer are preserved and restored exactly.
    def prior_handler(signum, frame):  # pragma: no cover - never invoked
        pass

    prior = signal.signal(signal.SIGALRM, prior_handler)
    try:
        signal.setitimer(signal.ITIMER_REAL, 20.0, 0.0)
        with _arm_attempt_deadline(0.1) as armed:
            assert armed is True
            # our deadline replaced the timer while inside the scope
            active_delay, _ = signal.getitimer(signal.ITIMER_REAL)
            assert 0 < active_delay <= 0.1, active_delay
        assert signal.getsignal(signal.SIGALRM) is prior_handler
        restored_delay, restored_interval = signal.getitimer(signal.ITIMER_REAL)
        assert restored_delay > 0, restored_delay  # prior 20s timer restored
        assert restored_interval == 0.0, restored_interval
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, prior)

    # (4) no alarm leaks when there was no prior timer.
    with _arm_attempt_deadline(0.1) as armed:
        assert armed is True
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is prior

    # (5) non-positive budget disables the deadline (no signal, no arm).
    with _arm_attempt_deadline(0.0) as armed:
        assert armed is False
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)

    # (6) off-main-thread: no SIGALRM armed (signal.setitimer would raise there),
    # and the context degrades to a harmless no-op instead of leaking a timer.
    thread_armed: list[bool] = []

    def _worker():
        with _arm_attempt_deadline(0.1) as armed_:
            thread_armed.append(armed_)

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert thread_armed == [False], thread_armed
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_cf04a_single_refresh_semantics():
    """Offline proof for CARMUDI-CF-04A: exactly one same-session refresh when the
    first bounded solver attempt is still challenged; zero when it clears or there
    is no challenge; never a second refresh even if the challenge persists; and the
    refresh call is deadline-bound with no alarm leak (no browser/network/DB)."""
    import logging
    import signal
    import time as _time

    from cars_scrap_common.botasaurus_utils import (
        attempt_solve_cloudflare_in_session,
    )

    logger = logging.getLogger("cf04a_refresh_test")
    logger.addHandler(logging.NullHandler())

    class _Driver:
        """page_html stays challenged unless `cleared` is set to True."""

        def __init__(self):
            self.cleared = False
            self.refreshes = 0

        @property
        def page_html(self):
            if self.cleared:
                return "<html><head><title>Carmudi</title></head><body>Rp 1</body></html>"
            return "<html><head><title>Just a moment...</title></head><body></body></html>"

        @property
        def current_url(self):
            return "https://www.carmudi.co.id/mobil-bekas-dijual/indonesia?token=SECRET"

        def run_js(self, *a, **k):
            raise RuntimeError("offline fixture: no JS runtime")

        def reload(self):
            self.refreshes += 1

    target = "https://www.carmudi.co.id/mobil-bekas-dijual/indonesia?token=SECRET"

    # (1) first attempt remains challenged -> exactly one refresh, then clears.
    d = _Driver()
    solver_calls: list[int] = []

    def solver_then_clear():
        solver_calls.append(1)

    def refresh_then_clear():
        d.refreshes += 1
        d.cleared = True

    result = attempt_solve_cloudflare_in_session(
        d,
        target_url=target,
        logger=logger,
        service_label="carmudi",
        max_attempts=3,
        post_wait_seconds=0.0,
        refresh=refresh_then_clear,
        refresh_wait_seconds=0.0,
        sleeper=lambda _s: None,
        solver=solver_then_clear,
    )
    assert result is True
    assert d.refreshes == 1, d.refreshes
    assert len(solver_calls) == 1, solver_calls  # cleared after refresh, no more attempts

    # (2) challenge persists -> exactly ONE refresh total (no refresh loop), and
    # remaining solver attempts still run (bounded by max_attempts).
    d2 = _Driver()
    solver_calls2: list[int] = []

    def persistent_solver():
        solver_calls2.append(1)

    def persistent_refresh():
        d2.refreshes += 1

    result2 = attempt_solve_cloudflare_in_session(
        d2,
        target_url=target,
        logger=logger,
        service_label="carmudi",
        max_attempts=3,
        post_wait_seconds=0.0,
        refresh=persistent_refresh,
        refresh_wait_seconds=0.0,
        sleeper=lambda _s: None,
        solver=persistent_solver,
    )
    assert result2 is False
    assert d2.refreshes == 1, d2.refreshes  # never a second refresh
    assert len(solver_calls2) == 3, solver_calls2  # all bounded attempts ran

    # (3) challenge clears on the first solver attempt -> zero refresh.
    d3 = _Driver()
    refresh_calls3: list[int] = []

    def clearing_solver():
        d3.cleared = True

    result3 = attempt_solve_cloudflare_in_session(
        d3,
        target_url=target,
        logger=logger,
        service_label="carmudi",
        max_attempts=3,
        post_wait_seconds=0.0,
        refresh=lambda: refresh_calls3.append(1),
        refresh_wait_seconds=0.0,
        sleeper=lambda _s: None,
        solver=clearing_solver,
    )
    assert result3 is True
    assert refresh_calls3 == [], refresh_calls3

    # (4) no challenge at all -> solver and refresh never invoked.
    d4 = _Driver()
    d4.cleared = True
    refresh_calls4: list[int] = []
    solver_calls4: list[int] = []
    result4 = attempt_solve_cloudflare_in_session(
        d4,
        target_url=target,
        logger=logger,
        service_label="carmudi",
        max_attempts=3,
        post_wait_seconds=0.0,
        refresh=lambda: refresh_calls4.append(1),
        refresh_wait_seconds=0.0,
        sleeper=lambda _s: None,
        solver=lambda: solver_calls4.append(1),
    )
    assert result4 is True
    assert refresh_calls4 == [] and solver_calls4 == []

    # (5) refresh is deadline-bound: a blocking refresh is interrupted, the wrapper
    # still returns (bounded) and leaves no alarm behind.
    if hasattr(signal, "SIGALRM"):
        d5 = _Driver()
        blocking_refresh_calls: list[int] = []

        def blocking_refresh():
            blocking_refresh_calls.append(1)
            _time.sleep(30)  # would hang without the refresh deadline

        started = _time.monotonic()
        result5 = attempt_solve_cloudflare_in_session(
            d5,
            target_url=target,
            logger=logger,
            service_label="carmudi",
            max_attempts=2,
            post_wait_seconds=0.0,
            refresh=blocking_refresh,
            refresh_wait_seconds=0.0,
            refresh_timeout_seconds=0.25,
            sleeper=lambda _s: None,
            solver=lambda: None,
        )
        elapsed = _time.monotonic() - started
        assert result5 is False
        assert blocking_refresh_calls == [1], blocking_refresh_calls  # one refresh only
        assert elapsed < 10, elapsed
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)  # no alarm leak

    # (6) the "Incompatible browser extension or network configuration" body copy
    # is a named signal, but ordinary listing copy is not a false positive.
    from cars_scrap_common.botasaurus_utils import (
        _detect_cloudflare_signals,
        is_cloudflare_challenge,
    )

    bad_cfg = _ExceptionOnlyDriver()
    bad_cfg.page_html = (
        "<html><body><h2>Performing security verification</h2>"
        "<p>Incompatible browser extension or network configuration</p></body></html>"
    )
    assert is_cloudflare_challenge(bad_cfg) is True
    assert "text:incompatible browser extension" in _detect_cloudflare_signals(bad_cfg)

    normal = _ExceptionOnlyDriver()
    normal.page_html = (
        "<html><head><title>Toyota Avanza 2019</title></head>"
        "<body>Rp 180.000.000 - browser extension included, network configuration normal</body>"
        "</html>"
    )
    assert is_cloudflare_challenge(normal) is False
    assert _detect_cloudflare_signals(normal) == ()


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
        test_carmudi_cf02_detector_signals,
        test_cf02_sanitizer_drops_url_secrets,
        test_cf02_bounded_solver_semantics,
        test_cf02_per_attempt_deadline,
        test_cf04a_single_refresh_semantics,
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
