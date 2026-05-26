from __future__ import annotations

import os
import re
import time
from hashlib import sha1
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv

from cars_scrap_common.botasaurus_utils import load_botasaurus_config
from cars_scrap_common.env_utils import env_bool
from cars_scrap_common.logging_utils import setup_logger
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip, reset_cached_proxy
from cars_scrap_common.shared_base_service import SharedBaseService

load_dotenv(override=True)

DEFAULT_LISTING_URL = "https://www.carsome.id/beli-mobil-bekas?sortKey=2"
BASE_URL = "https://www.carsome.id"


class CarsomeIdCarsService(SharedBaseService):
    def __init__(self):
        self.logger = setup_logger("scrape_carsomeid_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
        self.listing_url = os.getenv("CARSOMEID_CARS_LISTING_URL", DEFAULT_LISTING_URL).strip()

    def _carsomeid_env_int(self, key: str, default: int, *, minimum: int | None = None) -> int:
        value = os.getenv(f"CARSOMEID_{key}")
        previous = os.getenv(key)
        try:
            if value is not None:
                os.environ[key] = value
            return self._env_int(key, default, minimum=minimum)
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    def _carsomeid_env_float(self, key: str, default: float, *, minimum: float | None = None) -> float:
        value = os.getenv(f"CARSOMEID_{key}")
        previous = os.getenv(key)
        try:
            if value is not None:
                os.environ[key] = value
            return self._env_float(key, default, minimum=minimum)
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    def _maybe_log_proxy_and_ip(self, cfg) -> None:
        if self._logged_proxy_ip:
            return
        self._logged_proxy_ip = True

        if getattr(cfg, "proxy_url", None):
            self.logger.info("Proxy selected: %s", mask_proxy_url(str(cfg.proxy_url)))
        else:
            self.logger.info("Proxy disabled (direct connection).")

        if not env_bool("PROXY_CHECK_IP", False):
            return

        check_url = (os.getenv("PROXY_CHECK_URL") or "https://ifconfig.me/ip").strip()
        proxy_url = str(getattr(cfg, "proxy_url", "") or "").strip() or None
        ip = probe_public_ip(check_url=check_url, proxy_url=proxy_url)
        if ip:
            self.logger.info("Outbound IP via %s: %s", "proxy" if proxy_url else "direct", ip)
        else:
            self.logger.warning("Failed to probe outbound IP via %s.", "proxy" if proxy_url else "direct")

    def _load_botasaurus_config(self):
        previous = os.getenv("PROXY_ENABLED")
        proxy_enabled = os.getenv("CARSOMEID_PROXY_ENABLED", "false")
        try:
            os.environ["PROXY_ENABLED"] = proxy_enabled
            reset_cached_proxy()
            return load_botasaurus_config(service_name="carsomeid")
        finally:
            reset_cached_proxy()
            if previous is None:
                os.environ.pop("PROXY_ENABLED", None)
            else:
                os.environ["PROXY_ENABLED"] = previous

    def _extract_listing_id(self, url: str) -> str:
        parsed = urlparse(url)
        path = (parsed.path or "").rstrip("/")
        slug = path.rsplit("/", 1)[-1].strip()
        if slug:
            return slug
        return sha1(url.encode("utf-8")).hexdigest()[:16]

    def _is_on_target_domain(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        return host == "carsome.id" or host.endswith(".carsome.id")

    def _is_listing_detail_url(self, url: str) -> bool:
        if not self._is_on_target_domain(url):
            return False
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        path = (parsed.path or "").rstrip("/")
        return bool(re.match(r"^/beli-mobil-bekas/[^/]+/[^/]+/[^/]+/[^/]+$", path))

    def _dismiss_popups_botasaurus(self, driver) -> None:
        try:
            clicked_location = driver.run_js(
                """
                const modal = document.querySelector('.location-model, .location-model__mask');
                if (!modal) return false;
                const button = modal.querySelector('button.location-model-box__btn, .location-model-box__btn button, .location-model-box__btn');
                if (!button) return false;
                try { button.click(); return true; } catch (e) { return false; }
                """
            )
            if clicked_location:
                time.sleep(1.0)
                return
        except Exception:
            pass

        try:
            driver.run_js(
                r"""
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const click = (el) => {
                  if (!el) return false;
                  try { el.click(); return true; } catch (e) { return false; }
                };
                const candidates = Array.from(document.querySelectorAll(
                  'button, a, [role="button"], [aria-label], [class*="close"], [class*="Close"]'
                ));
                for (const el of candidates) {
                  const text = norm(el.innerText || el.textContent);
                  const aria = norm(el.getAttribute('aria-label'));
                  const cls = norm(String(el.className || ''));
                  if (
                    text === 'x' || text === '×' || text === 'close' || text === 'tutup' ||
                    aria.includes('close') || aria.includes('tutup') || cls.includes('close')
                  ) {
                    click(el);
                  }
                }
                """
            )
        except Exception:
            pass

    def _navigate_botasaurus(self, driver, url: str) -> None:
        if hasattr(driver, "google_get"):
            driver.google_get(url)
            return
        driver.get(url)

    def _extract_listing_urls_botasaurus(self, driver) -> list[str]:
        try:
            hrefs = driver.run_js(
                """
                const selectors = [
                  '.list-card__wrapper .list-card__item a.mod-b-card__header[href]',
                  '.list-card__item a.mod-b-card__header[href]',
                  'a.mod-b-card__header[href]',
                  'a[href^="/beli-mobil-bekas/"]'
                ];
                const links = [];
                for (const selector of selectors) {
                  for (const a of document.querySelectorAll(selector)) {
                    const href = String(a.getAttribute('href') || a.href || '').trim();
                    if (href) links.push(href);
                  }
                }
                return Array.from(new Set(links));
                """
            )
        except Exception:
            hrefs = []

        urls: list[str] = []
        for href in hrefs or []:
            if not isinstance(href, str):
                continue
            url = urljoin(BASE_URL, href.strip()).split("#", 1)[0]
            if self._is_listing_detail_url(url):
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _load_listing_urls_with_scrolls(self, driver, *, url: str, cfg, scroll_rounds: int) -> list[str]:
        self._navigate_botasaurus(driver, url)
        time.sleep(max(0.0, cfg.listing_wait_seconds))
        self._dismiss_popups_botasaurus(driver)

        collected: list[str] = []
        stable_rounds = 0
        scroll_pause = self._carsomeid_env_float("LISTING_SCROLL_PAUSE_SECONDS", 2.0, minimum=0.0)

        for round_index in range(0, max(1, scroll_rounds)):
            urls = self._extract_listing_urls_botasaurus(driver)
            before = len(collected)
            collected = list(dict.fromkeys([*collected, *urls]))

            if len(collected) == before:
                stable_rounds += 1
            else:
                stable_rounds = 0

            if round_index >= scroll_rounds - 1 or stable_rounds >= 2:
                break

            try:
                driver.run_js("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
            if scroll_pause:
                time.sleep(scroll_pause)

        return collected

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1) -> list[str]:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        url = (start_url or self.listing_url).strip()
        if not url:
            raise ValueError("Missing start URL. Set CARSOMEID_CARS_LISTING_URL or pass --start-url.")

        from botasaurus.browser import Driver, browser

        cfg = self._load_botasaurus_config()
        self._maybe_log_proxy_and_ip(cfg)

        @browser(**cfg.browser_kwargs)
        def run(driver: Driver, data):
            if cfg.human_mode:
                try:
                    driver.enable_human_mode()
                except Exception:
                    pass

            self.logger.info("Navigating (botasaurus): %s", data["url"])
            return self._load_listing_urls_with_scrolls(
                driver,
                url=data["url"],
                cfg=cfg,
                scroll_rounds=int(data["max_pages"]),
            )

        links = run({"url": url, "max_pages": max_pages}) or []
        self.logger.info("Collected %s links.", len(links))
        return links
