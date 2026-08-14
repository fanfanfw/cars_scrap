from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from cars_scrap_common.env_utils import env_bool, env_float
from cars_scrap_common.proxy_utils import load_proxy_for_run


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


def is_cloudflare_challenge(driver) -> bool:
    checks = (
        "verify you are human",
        "cloudflare ray id",
        "just a moment",
    )
    try:
        return bool(
            driver.run_js(
                """
                const hasTurnstile = !!document.querySelector("iframe[src*='turnstile']");
                const text = ((document.body && document.body.innerText) || "").toLowerCase();
                return hasTurnstile || args.checks.some((s) => text.includes(s));
                """,
                {"checks": list(checks)},
            )
        )
    except Exception:
        try:
            html = (driver.page_html or "").lower()
        except Exception:
            return False
        return ("turnstile" in html) or any(s in html for s in checks)


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

    base = (
        f"Blocked by Cloudflare Turnstile while scraping {service_label}. "
        f"target_url={target_url!r} current_url={getattr(driver, 'current_url', None)!r}. "
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
