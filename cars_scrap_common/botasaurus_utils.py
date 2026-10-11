from __future__ import annotations

import contextlib
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cars_scrap_common.env_utils import env_bool, env_float
from cars_scrap_common.proxy_utils import load_proxy_for_run

# CARMUDI-CF-02: concrete Cloudflare challenge signals. The legacy detector only
# inspected `iframe[src*='turnstile']` and `document.body.innerText`, so real
# Carmudi interstitials (title "Just a moment...", hidden `cf-turnstile-response`
# control, "Performing security verification" body copy) went undetected and were
# misclassified as empty listing pages. These constants keep the signal set in one
# place so detection, logging and offline checks agree.
CLOUDFLARE_TITLE_SIGNALS: tuple[str, ...] = ("just a moment",)
CLOUDFLARE_TEXT_SIGNALS: tuple[str, ...] = (
    "verify you are human",
    "cloudflare ray id",
    "just a moment",
    "performing security verification",
    # CARMUDI-CF-04A: distinctive Turnstile interstitial copy seen on Carmudi
    # ("Incompatible browser extension or network configuration"). Kept as the
    # full phrase to avoid false positives on normal listing/detail pages.
    "incompatible browser extension",
)
CLOUDFLARE_CONTROL_SELECTOR = '[name="cf-turnstile-response"]'
CLOUDFLARE_IFRAME_SELECTOR = "iframe[src*='turnstile']"


def sanitize_url_for_log(url: object) -> str:
    """
    Return a URL safe for logs: scheme/host/path only.

    Drops query strings, fragments and any userinfo so stage logs never leak
    tokens embedded in URLs (CARMUDI-CF-02).
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme and not parsed.netloc:
            # Relative/non-absolute value: strip query/fragment defensively.
            return raw.split("?", 1)[0].split("#", 1)[0]
        host = parsed.hostname or ""
        if ":" in host:  # IPv6 literal: re-add brackets for a valid-looking host
            host = f"[{host}]"
        # `.port` raises ValueError for malformed/out-of-range ports; this helper
        # must never raise while building a log line.
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port:
            host = f"{host}:{port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return "<invalid-url>"


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"<>)\]]+", re.IGNORECASE)


def sanitize_text_for_log(text: object) -> str:
    """
    Redact any embedded URLs in free-form text (e.g. exception messages) so
    stage logs and raised errors cannot leak query/fragment secrets.
    """
    raw = str(text or "")
    return _URL_IN_TEXT_RE.sub(lambda m: sanitize_url_for_log(m.group(0)), raw)


@dataclass(frozen=True)
class BotasaurusRuntimeConfig:
    browser_kwargs: dict[str, Any]
    bypass_cloudflare: bool
    human_mode: bool
    close_on_crash: bool
    prompt_on_cloudflare: bool
    save_html_on_empty: bool
    listing_wait_seconds: float
    detail_wait_seconds: float
    poll_interval_seconds: float
    proxy_url: str | None


def load_botasaurus_config(*, service_name: str) -> BotasaurusRuntimeConfig:
    """
    Loads a single global Botasaurus driver configuration for all scrapers.

    `service_name` is used to pick a default profile name and proxy pool.
    """
    xvfb_sources = {
        value.strip().lower()
        for value in os.getenv("BOTASAURUS_XVFB_ENABLED_SOURCES", "").split(",")
        if value.strip()
    }
    enable_xvfb = service_name.lower() in xvfb_sources
    headless = False

    profile = (os.getenv("BOTASAURUS_PROFILE") or "").strip() or service_name
    tiny_profile = env_bool("BOTASAURUS_TINY_PROFILE", True)
    bypass_cloudflare = env_bool("BOTASAURUS_BYPASS_CLOUDFLARE", True)

    human_mode = env_bool("BOTASAURUS_HUMAN_MODE", True)
    close_on_crash = env_bool("BOTASAURUS_CLOSE_ON_CRASH", True)
    prompt_on_cloudflare = env_bool("BOTASAURUS_PROMPT_ON_CLOUDFLARE", False)
    save_html_on_empty = env_bool("BOTASAURUS_SAVE_HTML_ON_EMPTY", False)

    listing_wait_seconds = env_float("BOTASAURUS_LISTING_WAIT_SECONDS", 8.0)
    detail_wait_seconds = env_float("BOTASAURUS_DETAIL_WAIT_SECONDS", 12.0)
    poll_interval_seconds = max(0.1, env_float("BOTASAURUS_POLL_INTERVAL_SECONDS", 0.25))

    browser_kwargs: dict[str, Any] = {
        "headless": headless,
        "output": None,
        "profile": profile,
        "tiny_profile": tiny_profile,
        "close_on_crash": close_on_crash,
    }
    if enable_xvfb:
        browser_kwargs["enable_xvfb_virtual_display"] = True
        # Xvfb only provides an X11 display. On Wayland desktops Chrome can ignore
        # DISPLAY and still open on the real desktop unless Ozone is forced to X11.
        browser_kwargs["add_arguments"] = ["--ozone-platform=x11"]
    proxy = load_proxy_for_run(service_name=service_name)
    if proxy:
        browser_kwargs["proxy"] = proxy

    return BotasaurusRuntimeConfig(
        browser_kwargs=browser_kwargs,
        bypass_cloudflare=bypass_cloudflare,
        human_mode=human_mode,
        close_on_crash=close_on_crash,
        prompt_on_cloudflare=prompt_on_cloudflare,
        save_html_on_empty=save_html_on_empty,
        listing_wait_seconds=listing_wait_seconds,
        detail_wait_seconds=detail_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        proxy_url=proxy,
    )


def wait_for_css(driver, selector: str, *, timeout_seconds: float, poll_interval_seconds: float) -> bool:
    selector = selector.replace("css=", "")
    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        try:
            exists = driver.run_js("return !!document.querySelector(args.selector);", {"selector": selector})
            if exists:
                return True
        except Exception:
            pass
        time.sleep(poll_interval_seconds)
    return False


def wait_for_xpath(driver, xpath: str, *, timeout_seconds: float, poll_interval_seconds: float) -> bool:
    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        try:
            exists = driver.run_js(
                """
                const el = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                return !!el;
                """,
                {"xpath": xpath},
            )
            if exists:
                return True
        except Exception:
            pass
        time.sleep(poll_interval_seconds)
    return False


def cloudflare_signals_from_facts(
    *,
    title: object = "",
    text: object = "",
    has_control: bool = False,
    has_iframe: bool = False,
) -> tuple[str, ...]:
    """
    Pure function mapping observed page facts to challenge signal labels.

    Kept free of driver/browser concerns so the detector semantics are fully
    testable offline by scripts/check_listing_failure.py (CARMUDI-CF-02).
    """
    title_l = str(title or "").lower()
    text_l = str(text or "").lower()

    signals: list[str] = []
    for token in CLOUDFLARE_TITLE_SIGNALS:
        if token in title_l:
            signals.append(f"title:{token}")
    for token in CLOUDFLARE_TEXT_SIGNALS:
        if token in text_l:
            signals.append(f"text:{token}")
    if has_control:
        signals.append("control:cf-turnstile-response")
    if has_iframe:
        signals.append("iframe:turnstile")
    # De-duplicate while keeping first-seen order.
    return tuple(dict.fromkeys(signals))


def _facts_from_page_html(html: str) -> dict[str, object]:
    """
    Best-effort title/text/control/iframe facts from raw HTML (offline path).

    When the JS runtime is unavailable we scan the whole document for the text
    and control/iframe signals, matching the previous HTML fallback so no legacy
    signal is lost; the title is extracted separately for a precise label.
    """
    lowered = html.lower()
    title_match = re.search(r"<title[^>]*>(.*?)</title>", lowered, re.DOTALL)
    title = title_match.group(1) if title_match else ""
    return {
        "title": title,
        "text": lowered,
        "has_control": "cf-turnstile-response" in lowered,
        "has_iframe": "turnstile" in lowered,
    }


def _detect_cloudflare_signals(driver) -> tuple[str, ...]:
    """
    Return the concrete Cloudflare challenge signals observed on `driver`.

    Signals are gathered from the live DOM first (title, body text, the Turnstile
    iframe and the hidden `cf-turnstile-response` control). If the JS runtime is
    unavailable (offline fixtures, closed shadow root) it falls back to scanning
    raw `page_html`, preserving the previous behaviour.
    """
    try:
        facts = driver.run_js(
            """
            return {
              title: String((document.title) || ""),
              text: String((document.body && document.body.innerText) || ""),
              has_control: !!document.querySelector(args.controlSelector),
              has_iframe: !!document.querySelector(args.iframeSelector)
            };
            """,
            {
                "controlSelector": CLOUDFLARE_CONTROL_SELECTOR,
                "iframeSelector": CLOUDFLARE_IFRAME_SELECTOR,
            },
        )
        if isinstance(facts, dict):
            return cloudflare_signals_from_facts(
                title=facts.get("title"),
                text=facts.get("text"),
                has_control=bool(facts.get("has_control")),
                has_iframe=bool(facts.get("has_iframe")),
            )
    except Exception:
        pass

    try:
        html = str(driver.page_html or "")
    except Exception:
        return ()
    if not html:
        return ()
    return cloudflare_signals_from_facts(**_facts_from_page_html(html))


def is_cloudflare_challenge(driver) -> bool:
    return bool(_detect_cloudflare_signals(driver))


class CloudflareSolveTimeoutError(TimeoutError):
    """Raised when the in-session Cloudflare solver exceeds its per-attempt deadline."""


# CARMUDI-CF-02: Linux/POSIX-only scoped deadline. The installed
# `detect_and_bypass_cloudflare()` can block in its own iframe/wait loops, so the
# wrapper must interrupt a single invocation after a wall-clock budget. We use a
# main-thread SIGALRM timer rather than a worker thread/process: the Botasaurus
# `driver` is not picklable/serializable, and an orphan thread could keep driving
# the shared browser after we give up. SIGALRM only works on the main thread; any
# prior handler and timer are captured and restored exactly.
@contextlib.contextmanager
def _arm_attempt_deadline(seconds: float):
    """
    Yield True when a SIGALRM deadline was armed, else False.

    Safe no-op (yields False) when the budget is non-positive, SIGALRM is
    unavailable (non-POSIX), or we are not on the main thread. Restores the prior
    SIGALRM handler and any pre-existing timer in all cases.
    """
    try:
        budget = float(seconds or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if budget <= 0 or not hasattr(signal, "SIGALRM"):
        yield False
        return
    seconds = budget
    if threading.current_thread() is not threading.main_thread():
        # setitimer/signal only take effect on the main thread; do not pretend to
        # enforce a deadline we cannot deliver, and never start an orphan timer.
        yield False
        return

    prior_handler = signal.getsignal(signal.SIGALRM)
    # Reading a timer requires disarming it; capture then re-arm in finally.
    prior_timer = signal.setitimer(signal.ITIMER_REAL, 0)

    def _on_timeout(signum, frame):
        raise CloudflareSolveTimeoutError(f"solver exceeded {seconds}s deadline")

    try:
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.setitimer(signal.ITIMER_REAL, float(seconds))
        yield True
    finally:
        # Disarm our timer first so no alarm can fire after we restore state.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)
        prior_delay, prior_interval = prior_timer
        if prior_delay and prior_delay > 0:
            signal.setitimer(signal.ITIMER_REAL, prior_delay, prior_interval)


def attempt_solve_cloudflare_in_session(
    driver,
    *,
    target_url: str,
    logger,
    service_label: str,
    max_attempts: int,
    post_wait_seconds: float,
    attempt_timeout_seconds: float = 0.0,
    refresh=None,
    refresh_wait_seconds: float = 0.0,
    refresh_timeout_seconds: float = 0.0,
    sleeper=time.sleep,
    solver=None,
) -> bool:
    """
    Bounded, application-level in-session Cloudflare solve with explicit stage logs.

    Delegates the actual interaction to the driver's own solver (normal browser
    interaction only). This wrapper exists so callers get a hard, non-unbounded
    attempt ceiling, an optional per-attempt wall-clock deadline, sanitized stage
    logging, and a deterministic return value.

    CARMUDI-CF-04A: when `refresh` is provided and the challenge survives the first
    solver attempt, the wrapper performs exactly ONE controlled same-session
    browser refresh (e.g. `driver.reload()`), waits a bounded interval, re-detects,
    then continues any remaining attempts. At most one refresh happens per call
    (i.e. per navigation), never once per attempt — so there is no refresh loop.

    Returns True when the challenge is no longer detected, else False after the
    bounded number of attempts. Never creates an unbounded retry loop.
    """
    attempts = max(1, int(max_attempts))
    invoke = solver if solver is not None else driver.detect_and_bypass_cloudflare
    safe_target = sanitize_url_for_log(target_url)

    signals = _detect_cloudflare_signals(driver)
    if not signals:
        return True

    refreshed = False
    for attempt in range(1, attempts + 1):
        current_url = sanitize_url_for_log(getattr(driver, "current_url", ""))
        logger.warning(
            "cloudflare_challenge_detected service=%s attempt=%s/%s signals=%s target_url=%s current_url=%s",
            service_label,
            attempt,
            attempts,
            ",".join(signals) or "none",
            safe_target,
            current_url,
        )
        logger.info(
            "cloudflare_solver_invoked service=%s attempt=%s/%s target_url=%s",
            service_label,
            attempt,
            attempts,
            safe_target,
        )
        try:
            with _arm_attempt_deadline(attempt_timeout_seconds) as armed:
                if armed:
                    logger.info(
                        "cloudflare_solver_deadline_armed service=%s attempt=%s/%s seconds=%s target_url=%s",
                        service_label,
                        attempt,
                        attempts,
                        attempt_timeout_seconds,
                        safe_target,
                    )
                invoke()
        except CloudflareSolveTimeoutError as exc:
            logger.warning(
                "cloudflare_solver_timeout service=%s attempt=%s/%s seconds=%s error=%s target_url=%s",
                service_label,
                attempt,
                attempts,
                attempt_timeout_seconds,
                sanitize_text_for_log(exc),
                safe_target,
            )
        except Exception as exc:
            # Sanitize the error text too: driver errors can embed full URLs.
            logger.warning(
                "cloudflare_solver_error service=%s attempt=%s/%s error=%s",
                service_label,
                attempt,
                attempts,
                sanitize_text_for_log(exc),
            )

        if post_wait_seconds:
            sleeper(max(0.0, float(post_wait_seconds)))

        signals = _detect_cloudflare_signals(driver)
        if not signals:
            logger.info(
                "cloudflare_solver_succeeded service=%s attempt=%s/%s target_url=%s",
                service_label,
                attempt,
                attempts,
                safe_target,
            )
            return True
        logger.info(
            "cloudflare_solver_still_challenged service=%s attempt=%s/%s target_url=%s",
            service_label,
            attempt,
            attempts,
            safe_target,
        )

        # CARMUDI-CF-04A: one controlled same-session refresh, only after the FIRST
        # attempt is still challenged, and never more than once per call.
        if refresh is not None and attempt == 1 and not refreshed:
            refreshed = True
            logger.warning(
                "cloudflare_refresh_invoked service=%s attempt=%s/%s target_url=%s",
                service_label,
                attempt,
                attempts,
                safe_target,
            )
            try:
                with _arm_attempt_deadline(refresh_timeout_seconds):
                    refresh()
            except CloudflareSolveTimeoutError as exc:
                logger.warning(
                    "cloudflare_refresh_timeout service=%s seconds=%s error=%s target_url=%s",
                    service_label,
                    refresh_timeout_seconds,
                    sanitize_text_for_log(exc),
                    safe_target,
                )
            except Exception as exc:
                logger.warning(
                    "cloudflare_refresh_error service=%s error=%s target_url=%s",
                    service_label,
                    sanitize_text_for_log(exc),
                    safe_target,
                )

            if refresh_wait_seconds:
                sleeper(max(0.0, float(refresh_wait_seconds)))

            signals = _detect_cloudflare_signals(driver)
            if not signals:
                logger.info(
                    "cloudflare_refresh_succeeded service=%s attempt=%s/%s target_url=%s",
                    service_label,
                    attempt,
                    attempts,
                    safe_target,
                )
                return True
            logger.info(
                "cloudflare_refresh_still_challenged service=%s attempt=%s/%s target_url=%s",
                service_label,
                attempt,
                attempts,
                safe_target,
            )
            # Continue with the remaining bounded solver attempts.

    logger.warning(
        "cloudflare_solver_exhausted service=%s attempts=%s target_url=%s",
        service_label,
        attempts,
        safe_target,
    )
    return False


def raise_if_cloudflare_challenge(
    driver,
    *,
    target_url: str,
    logger,
    service_label: str,
    prompt_on_cloudflare: bool,
) -> None:
    if not is_cloudflare_challenge(driver):
        return

    # Keep the stable "Blocked by Cloudflare Turnstile while scraping <label>."
    # prefix (downstream classification depends on it) but sanitize URLs so no
    # query/fragment secrets reach logs or error payloads (CARMUDI-CF-02).
    base = (
        f"Blocked by Cloudflare Turnstile while scraping {service_label}. "
        f"target_url={sanitize_url_for_log(target_url)!r} "
        f"current_url={sanitize_url_for_log(getattr(driver, 'current_url', None))!r}. "
    )

    if prompt_on_cloudflare:
        logger.warning("%sWaiting for manual solve (driver.prompt)...", base)
        try:
            driver.prompt()
        except Exception:
            pass
        if not is_cloudflare_challenge(driver):
            return

    raise RuntimeError(
        base
        + "Run with BOTASAURUS_PROMPT_ON_CLOUDFLARE=true to solve it manually (headful), "
        "or switch to a residential proxy / different IP."
    )
