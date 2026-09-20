from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime
from hashlib import sha1
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

from cars_scrap_common.botasaurus_utils import (
    is_cloudflare_challenge,
    load_botasaurus_config,
    raise_if_cloudflare_challenge,
    wait_for_xpath,
)
from cars_scrap_common.env_utils import env_bool
from cars_scrap_common.logging_utils import setup_logger
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip, reset_cached_proxy
from cars_scrap_common.shared_base_service import SharedBaseService
from scrap_mobil123_cars_botasaurus.database import get_connection

load_dotenv(override=True)

DEFAULT_LISTING_URL = "https://www.mobil123.com/mobil-bekas-dijual/indonesia?sort=modification_date_search.desc"
WHATSAPP_PHONE_RE = re.compile(r"(?:wa\.me/|phone=|tel:)(\+?\d+)", re.IGNORECASE)

LISTING_ID_META_XPATH = "/html/head/meta[47]"
CONDITION_META_XPATH = "/html/head/meta[46]"
BRAND_META_XPATH = "/html/head/meta[57]"
MODEL_META_XPATH = "/html/head/meta[59]"
VARIANT_META_XPATH = "/html/head/meta[60]"
PRICE_META_XPATH = "/html/head/meta[55]"
YEAR_META_XPATH = "/html/head/meta[61]"
TRANSMISSION_META_XPATH = "/html/head/meta[62]"
FUEL_TYPE_META_XPATH = "/html/head/meta[54]"

INFORMATION_ADS_XPATH = "/html/body/main/article/section/section[1]/section[3]/div/span"
INFORMATION_ADS_DATE_CONTAINER_XPATH = "/html/body/main/article/section/section[1]/section[3]/div/div/div[1]"
INFORMATION_ADS_DATE_XPATH = "/html/body/main/article/section/section[1]/section[3]/div/div/div[1]/span"
INFORMATION_ADS_DATE_SECONDARY_XPATH = "/html/body/main/article/section/section[1]/section[3]/div/div/div[1]/span[2]"
LOCATION_CONTAINER_XPATH = "/html/body/main/article/section/section[2]/div/div/div[2]/div[1]/div[1]/div/div/div"
MILEAGE_XPATH = (
    "/html/body/main/article/section/section[1]/section[5]/div/div/div/div/div[1]/div/div[3]/div/div/div/span[2]"
)
ENGINE_CC_XPATH = (
    "/html/body/main/article/section/section[1]/section[5]/div/div/div/div/div[1]/div/div[5]/div/div/div/span[2]"
)
SEAT_CAPACITY_XPATH = (
    "/html/body/main/article/section/section[1]/section[5]/div/div/div/div/div[1]/div/div[7]/div/div/div/span[2]"
)
IMAGES_CONTAINER_XPATH = "/html/body/main/article/section/section[4]/section[1]/div/div/div/div/div[1]/div/div[1]/div[1]"

TAB_SELLER_NOTES_SELECTOR = "#tab-seller-notes"
TAB_SPECIFICATIONS_SELECTOR = "#tab-specifications"
TAB_EQUIPMENTS_SELECTOR = "#tab-equipments"

DETAIL_TITLE_XPATH = "/html/body/main/article/section/section[1]/section[3]/div/h1"
DETAIL_PRICE_XPATH = "/html/body/main/article/section/section[1]/section[4]/div/div/div[1]/div[1]/div[2]/div/div[1]/h3"
DETAIL_LOCATION_CONTAINER_XPATH = "/html/body/main/article/section/section[2]/div/div/div[2]/div[1]/div[1]/div[1]/div/div"
DETAIL_MILEAGE_XPATH = "/html/body/main/article/section/section[1]/section[5]/div/div/div/div/div[1]/div/div[3]/div/div/div/span[2]"
DETAIL_TRANSMISSION_XPATH = "/html/body/main/article/section/section[1]/section[5]/div/div/div/div/div[1]/div/div[6]/div/div/div/span[2]"
DETAIL_SELLER_NAME_XPATH = "/html/body/main/article/section/section[2]/div/div/div[2]/div[1]/div[1]/div[1]/div/h6"
DETAIL_GALLERY_XPATH = "/html/body/main/article/section/section[1]/section[4]/div/div"
DETAIL_SPECIFICATIONS_SELECTOR = "#tab-specifications"
DETAIL_EQUIPMENTS_SELECTOR = "#tab-equipments"


class Mobil123CarsService(SharedBaseService):
    def __init__(self):
        self.logger = setup_logger("scrape_mobil123_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
        self._logged_listing_id_meta_mismatch = False
        self.listing_url = os.getenv("MOBIL123_CARS_LISTING_URL", DEFAULT_LISTING_URL).strip()

    def _mobil123_env_int(self, key: str, default: int, *, minimum: int | None = None) -> int:
        value = os.getenv(f"MOBIL123_{key}")
        if value is None:
            value = os.getenv(f"CARLIST_{key}")
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

    def _mobil123_env_float(self, key: str, default: float, *, minimum: float | None = None) -> float:
        value = os.getenv(f"MOBIL123_{key}")
        if value is None:
            value = os.getenv(f"CARLIST_{key}")
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

    def _mobil123_env_bool(self, key: str, default: bool) -> bool:
        value = os.getenv(f"MOBIL123_{key}")
        if value is None:
            value = os.getenv(f"CARLIST_{key}")
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

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

    def _save_debug_capture_botasaurus(self, driver, *, slug: str, reason: str, url: str) -> None:
        try:
            from botasaurus import bt

            out_dir = "botasaurus_test/output/mobil123_debug"
            os.makedirs(out_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_slug = re.sub(r"[^a-z0-9_.-]+", "_", str(slug or "page").lower()).strip("_") or "page"
            base_path = os.path.join(out_dir, f"{timestamp}_{safe_slug}")

            html = ""
            try:
                html = str(driver.page_html or "")
            except Exception:
                html = ""
            if html:
                bt.write_html(html, base_path)

            snapshot = {
                "captured_at": datetime.now().isoformat(),
                "reason": reason,
                "target_url": url,
                "current_url": str(getattr(driver, "current_url", "") or ""),
                "title": self._get_page_title_botasaurus(driver),
                "text_excerpt": self._get_page_text_botasaurus(driver)[:4000],
            }
            with open(f"{base_path}.json", "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2, ensure_ascii=False)

            self.logger.warning("Saved mobil123 debug capture: %s.html and %s.json", base_path, base_path)
        except Exception as exc:
            self.logger.warning("Failed to save mobil123 debug capture for %s: %s", url, exc)

    def _extract_listing_id(self, url: str) -> str:
        numeric = self._extract_numeric_listing_id(url)
        if numeric:
            return numeric
        return sha1(url.encode("utf-8")).hexdigest()[:16]

    def _extract_numeric_listing_id(self, url: str) -> str | None:
        match = re.search(r"(?:-|/)(\d{5,})(?:[/?#.]|$)", url)
        if match:
            return match.group(1)
        match = re.search(r"(?:listing_id|ad_id|id)=([0-9]{5,})", url, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _is_on_target_domain(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return host == "mobil123.com" or host.endswith(".mobil123.com")

    def _is_listing_detail_url(self, url: str) -> bool:
        if not self._is_on_target_domain(url):
            return False
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        path = (parsed.path or "").rstrip("/")
        if not path.startswith("/dijual/"):
            return False
        return bool(re.search(r"/\d{5,}$", path))

    def _dismiss_mobil123_popups_botasaurus(self, driver) -> None:
        try:
            driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const click = (el) => {
                  if (!el) return false;
                  try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                  try { el.click(); return true; } catch (e) { return false; }
                };

                const candidates = Array.from(document.querySelectorAll(
                  'button, a, [role="button"], [aria-label], [class*="close"], [class*="Close"]'
                ));
                for (const el of candidates) {
                  const text = norm(el.innerText || el.textContent);
                  const aria = norm(el.getAttribute('aria-label'));
                  const title = norm(el.getAttribute('title'));
                  const cls = norm(String(el.className || ''));
                  if (
                    text === 'x' || text === '×' || text === 'close' || text === 'tutup' ||
                    aria.includes('close') || aria.includes('tutup') ||
                    title.includes('close') || title.includes('tutup') ||
                    cls.includes('close')
                  ) {
                    click(el);
                  }
                }
                """
            )
        except Exception:
            pass

    def _is_retryable_http_400_error(self, exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        return any(
            token in text
            for token in (
                "retryable error page while scraping mobil123",
                "retryable soft block page while scraping mobil123",
                "blocked while scraping mobil123",
            )
        )

    def _classify_retryable_browser_error(self, exc: Exception | None) -> str:
        text = str(exc or "").strip().lower()
        if "soft block" in text:
            return "soft_block"
        if "blocked while scraping mobil123" in text:
            return "blocked"
        return "browser_error"

    def _is_sold_page_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "this car has already been sold",
                "car has already been sold",
                "already been sold",
                "sudah terjual",
                "mobil ini telah terjual",
                "iklan tidak tersedia",
                "mobil ini saat ini tidak tersedia",
            ),
        )

    def _is_listing_not_found_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "page not found",
                "404",
                "not found",
                "halaman tidak ditemukan",
                "tidak ditemukan",
                "iklan tidak tersedia",
                "ad no longer available",
                "no longer available",
            ),
        )

    def _is_access_blocked_botasaurus(self, driver) -> bool:
        if is_cloudflare_challenge(driver):
            return True
        if self._is_http_error_page_botasaurus(driver):
            return True
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "access denied",
                "forbidden",
                "request blocked",
                "your request has been blocked",
                "too many requests",
                "temporarily unavailable",
                "captcha",
            ),
        )

    def _attempt_cloudflare_auto_solve_botasaurus(self, driver, *, target_url: str) -> None:
        if not self._mobil123_env_bool("AUTO_ATTEMPT_CLOUDFLARE", True):
            return
        if not is_cloudflare_challenge(driver):
            return

        max_attempts = self._mobil123_env_int("CLOUDFLARE_MAX_ATTEMPTS", 2, minimum=1)
        post_wait_seconds = self._mobil123_env_float("CLOUDFLARE_POST_SOLVE_WAIT_SECONDS", 4.0, minimum=0.0)

        for attempt in range(1, max_attempts + 1):
            current_url = str(getattr(driver, "current_url", "") or "")
            self.logger.warning(
                "Cloudflare challenge detected on mobil123. Auto-solve attempt %s/%s. target_url=%r current_url=%r",
                attempt,
                max_attempts,
                target_url,
                current_url,
            )
            try:
                driver.detect_and_bypass_cloudflare()
            except Exception as exc:
                self.logger.warning("Botasaurus auto-solve attempt %s failed: %s", attempt, exc)

            if post_wait_seconds:
                time.sleep(post_wait_seconds)

            if not is_cloudflare_challenge(driver):
                self.logger.info("Cloudflare challenge cleared after auto-solve attempt %s.", attempt)
                return

    def _navigate_botasaurus(self, driver, url: str, cfg) -> None:
        try:
            driver.get_via(url, referer="https://www.google.com/")
        except Exception:
            pass
        driver.google_get(url, bypass_cloudflare=cfg.bypass_cloudflare)
        self._attempt_cloudflare_auto_solve_botasaurus(driver, target_url=url)
        raise_if_cloudflare_challenge(
            driver,
            target_url=url,
            logger=self.logger,
            service_label="mobil123",
            prompt_on_cloudflare=cfg.prompt_on_cloudflare,
        )

    def _build_retry_cfg_for_http_400(self, *, base_profile: str, retry_index: int, listing_key: str):
        previous_profile = os.getenv("BOTASAURUS_PROFILE")
        suffix = str(retry_index).strip() or "1"
        unique = str(time.time_ns())[-8:]
        retry_profile = f"{base_profile}_http400_r{suffix}_{listing_key}_{unique}"

        try:
            os.environ["BOTASAURUS_PROFILE"] = retry_profile
            reset_cached_proxy()
            cfg = load_botasaurus_config(service_name="mobil123")
        finally:
            if previous_profile is None:
                os.environ.pop("BOTASAURUS_PROFILE", None)
            else:
                os.environ["BOTASAURUS_PROFILE"] = previous_profile
            reset_cached_proxy()

        return cfg

    def _scrape_detail_with_same_session_retries(
        self,
        driver,
        *,
        url: str,
        cfg,
        listing_id: str | None,
        tracking_mode: bool,
    ) -> dict:
        sticky_retries = self._mobil123_env_int("STICKY_BROWSER_RETRIES", 1, minimum=0)
        retry_wait_seconds = self._mobil123_env_float("STICKY_BROWSER_RETRY_WAIT_SECONDS", 2.5, minimum=0.0)
        max_attempts = sticky_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.logger.warning(
                    "Retrying mobil123 detail in same session. retry=%s/%s url=%s",
                    attempt - 1,
                    sticky_retries,
                    url,
                )
                if retry_wait_seconds:
                    time.sleep(retry_wait_seconds)
            try:
                return self._scrape_single_detail_botasaurus(
                    driver,
                    url=url,
                    cfg=cfg,
                    listing_id=listing_id,
                    tracking_mode=tracking_mode,
                )
            except Exception as exc:
                last_exc = exc
                if self._is_retryable_http_400_error(exc) and attempt < max_attempts:
                    continue
                raise

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Failed to scrape detail for {url}")

    def _load_listing_urls_with_same_session_retries(self, driver, *, url: str, cfg) -> list[str]:
        sticky_retries = self._mobil123_env_int("STICKY_BROWSER_RETRIES", 1, minimum=0)
        retry_wait_seconds = self._mobil123_env_float("STICKY_BROWSER_RETRY_WAIT_SECONDS", 2.5, minimum=0.0)
        max_attempts = sticky_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.logger.warning(
                    "Retrying mobil123 listing in same session. retry=%s/%s url=%s",
                    attempt - 1,
                    sticky_retries,
                    url,
                )
                if retry_wait_seconds:
                    time.sleep(retry_wait_seconds)

            self._navigate_botasaurus(driver, url, cfg)
            time.sleep(max(0.0, cfg.listing_wait_seconds))
            self._dismiss_mobil123_popups_botasaurus(driver)

            current_url = str(getattr(driver, "current_url", "") or "")
            if self._is_http_error_page_botasaurus(driver):
                last_exc = RuntimeError(
                    f"Retryable error page while scraping mobil123 listing: current_url={current_url!r}"
                )
                if attempt < max_attempts:
                    continue
                raise last_exc

            if self._is_soft_block_page_botasaurus(driver):
                self._save_debug_capture_botasaurus(
                    driver,
                    slug="mobil123_listing_soft_block",
                    reason="soft_block",
                    url=url,
                )
                last_exc = RuntimeError(
                    f"Retryable soft block page while scraping mobil123 listing: current_url={current_url!r}"
                )
                if attempt < max_attempts:
                    continue
                raise last_exc

            urls = self._extract_listing_urls_botasaurus(driver)
            if urls:
                return urls

            self._save_debug_capture_botasaurus(
                driver,
                slug="mobil123_listing_empty",
                reason="no_listing_links",
                url=url,
            )
            last_exc = RuntimeError(
                f"Retryable soft block page while scraping mobil123 listing: current_url={current_url!r} reason='no_listing_links'"
            )
            if attempt < max_attempts:
                continue
            raise last_exc

        if last_exc is not None:
            raise last_exc
        return []

    def _retry_http_400_with_new_session(
        self,
        *,
        url: str,
        listing_id: str | None,
        tracking_mode: bool,
        base_profile: str,
    ) -> dict | None:
        if not self._mobil123_env_bool("HTTP_400_RETRY_ENABLED", True):
            return None
        max_retries = self._mobil123_env_int("HTTP_400_MAX_RETRIES", 2, minimum=1)

        from botasaurus.browser import Driver, browser

        listing_key = (listing_id or self._extract_numeric_listing_id(url) or self._extract_listing_id(url) or "unknown").strip()

        for retry_index in range(1, max_retries + 1):
            cfg = self._build_retry_cfg_for_http_400(
                base_profile=base_profile,
                retry_index=retry_index,
                listing_key=listing_key,
            )
            self.logger.warning(
                "Retrying mobil123 detail after browser error page. retry=%s/%s profile=%s proxy=%s url=%s",
                retry_index,
                max_retries,
                cfg.browser_kwargs.get("profile"),
                mask_proxy_url(str(cfg.proxy_url)) if getattr(cfg, "proxy_url", None) else "direct",
                url,
            )

            @browser(**cfg.browser_kwargs)
            def run(driver: Driver, data):
                if cfg.human_mode:
                    try:
                        driver.enable_human_mode()
                    except Exception:
                        pass
                return self._scrape_single_detail_botasaurus(
                    driver,
                    url=data["url"],
                    cfg=cfg,
                    listing_id=data.get("listing_id"),
                    tracking_mode=tracking_mode,
                )

            try:
                result = run({"url": url, "listing_id": listing_id})
                if result is not None:
                    return result
            except Exception as exc:
                if self._is_retryable_http_400_error(exc) and retry_index < max_retries:
                    self.logger.warning("Browser error retry %s still failed for %s: %s", retry_index, url, exc)
                    continue
                self.logger.warning("Retry session failed for %s: %s", url, exc)
                return None

        return None

    def _retry_listing_http_400_with_new_session(self, *, url: str, max_pages: int, base_profile: str) -> list[str] | None:
        if not self._mobil123_env_bool("HTTP_400_RETRY_ENABLED", True):
            return None
        max_retries = self._mobil123_env_int("HTTP_400_MAX_RETRIES", 2, minimum=1)

        from botasaurus.browser import Driver, browser

        for retry_index in range(1, max_retries + 1):
            cfg = self._build_retry_cfg_for_http_400(
                base_profile=base_profile,
                retry_index=retry_index,
                listing_key="listing",
            )
            self.logger.warning(
                "Retrying mobil123 listing after browser error page. retry=%s/%s profile=%s proxy=%s url=%s",
                retry_index,
                max_retries,
                cfg.browser_kwargs.get("profile"),
                mask_proxy_url(str(cfg.proxy_url)) if getattr(cfg, "proxy_url", None) else "direct",
                url,
            )

            @browser(**cfg.browser_kwargs)
            def run(driver: Driver, data):
                if cfg.human_mode:
                    try:
                        driver.enable_human_mode()
                    except Exception:
                        pass
                self.logger.info("Navigating (botasaurus retry): %s", data["url"])
                return self._load_listing_urls_with_same_session_retries(driver, url=data["url"], cfg=cfg)

            try:
                links = run({"url": url, "max_pages": max_pages})
                if links is not None:
                    return links
            except Exception as exc:
                if self._is_retryable_http_400_error(exc) and retry_index < max_retries:
                    self.logger.warning("Listing retry %s still failed for %s: %s", retry_index, url, exc)
                    continue
                self.logger.warning("Listing retry session failed for %s: %s", url, exc)
                return None

        return None

    def _parse_rupiah_price(self, text: object | None) -> int | None:
        if isinstance(text, str) and text.lstrip().startswith("-"):
            return None
        return super()._parse_rupiah_price(text)

    def _parse_mileage(self, mileage_str: str | None) -> int:
        text = str(mileage_str or "").strip()
        if not text or text == "- km":
            return 0
        try:
            if "-" in text:
                right = text.split("-")[-1].strip()
            else:
                right = text
            right = right.replace("km", "").replace("KM", "").replace("Km", "").strip()
            right = right.replace("K", "000").replace("k", "000")
            right = right.replace(" ", "")
            return int(re.sub(r"[^\d]", "", right) or "0")
        except Exception:
            return 0

    def _parse_first_mileage(self, *values: str | None) -> int:
        for value in values:
            parsed = self._parse_mileage(value)
            if parsed > 0:
                return parsed
        return 0

    def _normalize_seat_capacity(self, value: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"\d+", text)
        return match.group(0) if match else None

    def _parse_information_ads_date(self, information_ads: str | None) -> date | None:
        if not information_ads:
            return None
        month_mapping = {
            "january": "01",
            "januari": "01",
            "jan": "01",
            "february": "02",
            "februari": "02",
            "feb": "02",
            "march": "03",
            "maret": "03",
            "mar": "03",
            "april": "04",
            "apr": "04",
            "may": "05",
            "mei": "05",
            "june": "06",
            "juni": "06",
            "jun": "06",
            "july": "07",
            "juli": "07",
            "jul": "07",
            "august": "08",
            "agustus": "08",
            "agu": "08",
            "aug": "08",
            "september": "09",
            "sep": "09",
            "sept": "09",
            "october": "10",
            "oktober": "10",
            "okt": "10",
            "oct": "10",
            "november": "11",
            "nov": "11",
            "december": "12",
            "desember": "12",
            "des": "12",
            "dec": "12",
        }
        text = str(information_ads).lower().strip()
        text = re.sub(
            r"^(diperbarui(?:\s+pada)?\s*:|diposting(?:\s+pada)?\s*:|tanggal\s*:|updated\s+on:|posted\s+on:|date:)\s*",
            "",
            text,
        )

        match = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
        if match:
            day, month_str, year = match.groups()
            month_num = month_mapping.get(month_str[:3]) or month_mapping.get(month_str)
            if month_num:
                try:
                    return datetime(int(year), int(month_num), int(day)).date()
                except ValueError:
                    return None

        match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match:
            year, month, day = match.groups()
            try:
                return datetime(int(year), int(month), int(day)).date()
            except ValueError:
                return None

        return None

    def _extract_legacy_location_botasaurus(self, driver) -> str | None:
        try:
            parts = driver.run_js(
                """
                const root = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!root) return [];
                const texts = Array.from(root.querySelectorAll('span'))
                  .map((el) => (el.innerText || el.textContent || '').trim())
                  .filter(Boolean);
                return texts;
                """,
                {"xpath": LOCATION_CONTAINER_XPATH},
            )
        except Exception:
            parts = []

        items = [p for p in (parts or []) if isinstance(p, str) and p.strip()]
        if len(items) >= 3:
            return f"{items[1]} - {items[2]}"
        if len(items) >= 2:
            return items[1]
        return None

    def _extract_legacy_images_botasaurus(self, driver) -> list[str] | None:
        try:
            urls = driver.run_js(
                """
                const isValid = (url) => /^https?:\/\//i.test(String(url || '').trim());
                const root = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!root) return [];
                const out = [];
                const attrs = ['src', 'data-src', 'data-lazy', 'data-original'];
                for (const img of Array.from(root.querySelectorAll('img'))) {
                  for (const attr of attrs) {
                    const v = img.getAttribute(attr);
                    if (v && isValid(v)) out.push(v);
                  }
                  const srcset = img.getAttribute('srcset');
                  if (srcset) {
                    for (const part of srcset.split(',')) {
                      const url = part.trim().split(' ')[0];
                      if (url && isValid(url)) out.push(url);
                    }
                  }
                }
                return Array.from(new Set(out.filter(Boolean)));
                """,
                {"xpath": IMAGES_CONTAINER_XPATH},
            )
        except Exception:
            urls = []

        deduped: list[str] = []
        seen: set[str] = set()
        for u in urls or []:
            if not isinstance(u, str):
                continue
            url = u.strip()
            if not url.startswith(("http://", "https://")):
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped or None

    def _extract_key_details_dict_botasaurus(self, driver) -> dict[str, str]:
        data = self._extract_carousel_dict_botasaurus(driver) or {}
        return {str(key): str(value).strip() for key, value in data.items() if value is not None and str(value).strip()}

    def _extract_flat_tab_fields_botasaurus(self, driver, *, tab_selector: str) -> dict[str, str]:
        grouped = self._extract_tab_grouped_kv_botasaurus(driver, tab_selector=tab_selector)
        return self._flatten_grouped_kv(grouped)

    def _extract_text_by_nearby_label_botasaurus(self, driver, labels: tuple[str, ...]) -> str | None:
        try:
            return driver.run_js(
                """
                const labels = args.labels.map((s) => String(s || '').toLowerCase());
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const nodes = Array.from(document.querySelectorAll('span, div, li, dt, dd'));
                const isLabel = (text) => labels.some((label) => text === label || text.includes(label));
                for (const el of nodes) {
                  const text = norm(el.textContent || el.innerText);
                  const lower = text.toLowerCase();
                  if (!lower || !isLabel(lower)) continue;
                  const parent = el.parentElement;
                  if (!parent) continue;
                  const siblings = Array.from(parent.querySelectorAll('span, div, dd'))
                    .map((node) => norm(node.textContent || node.innerText))
                    .filter(Boolean);
                  for (const value of siblings) {
                    const valueLower = value.toLowerCase();
                    if (valueLower && valueLower !== lower && !isLabel(valueLower)) return value;
                  }
                }
                return null;
                """,
                {"labels": list(labels)},
            )
        except Exception:
            return None

    def _extract_mileage_from_bold_span_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const mileageRe = /\b\d[\d.,\s]*(?:-\s*\d[\d.,\s]*k?)?\s*km\b/i;
                const nodes = Array.from(document.querySelectorAll('span.u-text-bold.u-block, span.u-text-bold, .u-text-bold'));
                for (const el of nodes) {
                  const text = norm(el.textContent || el.innerText);
                  if (text && mileageRe.test(text)) return text;
                }
                return null;
                """
            )
        except Exception:
            return None

    def _extract_contact_seller_fallback_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const normalizePhone = (value) => {
                  const text = norm(value);
                  if (!text) return null;
                  const digits = text.replace(/\D+/g, '');
                  if (digits.length < 8) return null;
                  return text;
                };
                const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                for (const el of nodes) {
                  const href = String(el.href || el.getAttribute('href') || '').trim();
                  if (/^tel:/i.test(href)) {
                    const phone = normalizePhone(href.split(':').slice(1).join(':'));
                    if (phone) return phone;
                  }
                  const dataPhone = el.getAttribute('data-phone') || el.getAttribute('data-phones') || '';
                  const phone = normalizePhone(dataPhone);
                  if (phone) return phone;
                }
                return null;
                """
            )
        except Exception:
            return None

    def _click_contact_seller_fallback_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                    const candidates = nodes.map((el) => {
                      const text = norm(el.textContent || el.innerText);
                      const cls = String(el.className || '').toLowerCase();
                      const href = String(el.href || el.getAttribute('href') || '').trim().toLowerCase();
                      const dataPhone = String(el.getAttribute('data-phone') || el.getAttribute('data-phones') || '').trim();
                      let score = 0;
                      if (href.startsWith('tel:')) score += 100;
                      if (dataPhone) score += 90;
                      if (cls.includes('jump-phone') || cls.includes('phone')) score += 70;
                      if (text.includes('contact') || text.includes('call')) score += 50;
                      if (text.includes('hubungi') || text.includes('telepon')) score += 50;
                      if (text.includes('lihat nomor') || text.includes('nomor')) score += 40;
                      if (score <= 0) return null;
                      return {el, score, isAnchor: el.tagName === 'A' ? 1 : 0};
                    }).filter(Boolean);
                    candidates.sort((a, b) => (b.score - a.score) || (b.isAnchor - a.isAnchor));
                    for (const item of candidates) {
                      try { item.el.scrollIntoView({block: 'center'}); } catch (e) {}
                      try { item.el.click(); return true; } catch (e) {}
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _extract_contact_seller_value_botasaurus(self, driver, *, listing_id: str) -> str | None:
        value = self._extract_contact_seller_botasaurus(
            driver,
            listing_id=listing_id,
        )
        if value:
            return value

        value = self._extract_contact_seller_fallback_botasaurus(driver)
        if value:
            return value

        if self._click_contact_seller_fallback_botasaurus(driver):
            deadline = time.time() + 3.0
            while time.time() < deadline:
                time.sleep(0.25)
                value = self._extract_contact_seller_fallback_botasaurus(driver)
                if value:
                    self._close_contact_seller_modal_botasaurus(driver)
                    return value
            self._close_contact_seller_modal_botasaurus(driver)

        return None

    def _json_ld_objects_botasaurus(self, driver) -> list[dict]:
        return super()._json_ld_objects_botasaurus(driver)

    def _first_meta_content_botasaurus(self, driver, names: list[str]) -> str | None:
        return super()._first_meta_content_botasaurus(driver, names)

    def _first_json_ld_value(self, items: list[dict], key: str) -> str | None:
        return super()._first_json_ld_value(items, key)

    def _normalize_condition_value(self, value: str | None) -> str | None:
        return super()._normalize_condition_value(value)

    def _clean_short_text(self, value: object | None, *, max_len: int = 100) -> str | None:
        return super()._clean_short_text(value, max_len=max_len)

    def _clean_year(self, value: object | None) -> int | None:
        return super()._clean_year(value)

    def _get_text_by_xpath_botasaurus(self, driver, xpath: str) -> str | None:
        try:
            text = driver.run_js(
                """
                const el = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!el) return null;
                const raw = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                return raw || null;
                """,
                {"xpath": xpath},
            )
        except Exception:
            text = None
        return text or None

    def _extract_information_ads_botasaurus(self, driver) -> str | None:
        def clean(value: str | None) -> str | None:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if not text:
                return None
            if text in {"-", "·", "•"}:
                return None
            lower = text.lower()
            if (
                "verified ready stock" in lower
                or "verified for information authenticity" in lower
                or "view registration card" in lower
            ):
                return None
            if not self._parse_information_ads_date(text):
                return None
            return text

        try:
            candidates = driver.run_js(
                """
                const root = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!root) return [];
                return Array.from(root.querySelectorAll('span'))
                  .map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
                  .filter(Boolean);
                """,
                {"xpath": INFORMATION_ADS_DATE_CONTAINER_XPATH},
            )
        except Exception:
            candidates = []

        for candidate in candidates or []:
            value = clean(candidate)
            if value:
                return value

        for xpath in (INFORMATION_ADS_DATE_SECONDARY_XPATH, INFORMATION_ADS_DATE_XPATH, INFORMATION_ADS_XPATH):
            value = clean(self._get_text_by_xpath_botasaurus(driver, xpath))
            if value:
                return value
        return None

    def _extract_title_botasaurus(self, driver) -> str | None:
        title = self._get_text_by_xpath_botasaurus(driver, DETAIL_TITLE_XPATH)
        if title:
            return title
        title = self._first_meta_content_botasaurus(driver, ["og:title", "twitter:title"])
        if title:
            return title
        page_title = self._get_page_title_botasaurus(driver)
        return page_title or None

    def _extract_price_botasaurus(self, driver, json_ld_items: list[dict]) -> int | None:
        xpath_price = self._parse_rupiah_price(self._get_text_by_xpath_botasaurus(driver, DETAIL_PRICE_XPATH))
        if xpath_price:
            return xpath_price

        for item in json_ld_items:
            offers = item.get("offers") if isinstance(item, dict) else None
            candidates = []
            if isinstance(offers, dict):
                candidates.extend([offers.get("price"), offers.get("lowPrice"), offers.get("highPrice")])
            candidates.extend([item.get("price"), item.get("lowPrice"), item.get("highPrice")])
            for candidate in candidates:
                price = self._parse_rupiah_price(candidate)
                if price:
                    return price

        meta_price = self._first_meta_content_botasaurus(
            driver,
            ["product:price:amount", "og:price:amount", "twitter:data1"],
        )
        price = self._parse_rupiah_price(meta_price)
        if price:
            return price

        text = self._get_page_text_botasaurus(driver)
        match = re.search(r"Rp\s*[\d.]+", text, re.IGNORECASE)
        return self._parse_rupiah_price(match.group(0) if match else None)

    def _extract_location_botasaurus(self, driver) -> str | None:
        try:
            chips = driver.run_js(
                """
                const root = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!root) return [];
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                return Array.from(root.querySelectorAll('span.c-chip, span'))
                  .map((el) => norm(el.innerText || el.textContent))
                  .filter(Boolean);
                """,
                {"xpath": DETAIL_LOCATION_CONTAINER_XPATH},
            )
        except Exception:
            chips = []

        items = [str(item).strip() for item in chips or [] if str(item or "").strip()]
        items = [item for item in items if item.lower() not in {"dealer", "private", "broker"}]
        if len(items) >= 2:
            return f"{items[-2]} - {items[-1]}"
        if items:
            return items[-1]
        return None

    def _extract_grouped_kv_botasaurus(self, driver, *, tab_selector: str) -> dict[str, object] | None:
        try:
            rows = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const tab = document.querySelector(args.selector);
                if (!tab) return [];
                const out = [];
                for (const group of Array.from(tab.querySelectorAll('.o-grid'))) {
                  const heading = norm(group.querySelector('h3') && group.querySelector('h3').textContent);
                  for (const row of Array.from(group.querySelectorAll('.u-flex.u-flex--justify-between'))) {
                    const spans = Array.from(row.querySelectorAll('span'));
                    if (spans.length < 2) continue;
                    const valueEl = spans.find((el) => String(el.className || '').includes('u-text-bold')) || spans[spans.length - 1];
                    const labelEl = spans.find((el) => el !== valueEl) || spans[0];
                    const label = norm(labelEl && labelEl.textContent);
                    const value = norm(valueEl && valueEl.textContent);
                    if (label && value) out.push({heading, label, value});
                  }
                }
                return out;
                """,
                {"selector": tab_selector},
            )
        except Exception:
            rows = []

        grouped: dict[str, object] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            heading = self._spec_key_to_snake(str(row.get("heading") or "general")) or "general"
            label = self._spec_key_to_snake(str(row.get("label") or ""))
            value = str(row.get("value") or "").strip()
            if not label or not value:
                continue
            section = grouped.get(heading)
            if not isinstance(section, dict):
                section = {}
                grouped[heading] = section
            section[label] = value
        return grouped or None

    def _flatten_grouped_kv(self, grouped: dict[str, object] | None) -> dict[str, str]:
        flat: dict[str, str] = {}
        for value in (grouped or {}).values():
            if not isinstance(value, dict):
                continue
            for key, item in value.items():
                if item is not None and key not in flat:
                    flat[str(key)] = str(item)
        return flat

    def _extract_structured_fields_botasaurus(self, driver) -> dict[str, str]:
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const out = [];
                const nodes = Array.from(document.querySelectorAll('li, dl, tr, div, span'));
                for (const el of nodes) {
                  const text = norm(el.innerText || el.textContent);
                  if (!text || text.length > 180) continue;
                  let m = text.match(/^([^:\n]{2,40})\s*:\s*(.{1,120})$/);
                  if (m) {
                    out.push({label: m[1], value: m[2]});
                    continue;
                  }
                  const children = Array.from(el.children || []);
                  if (children.length < 2 || children.length > 4) continue;
                  const parts = children.map((child) => norm(child.innerText || child.textContent)).filter(Boolean);
                  if (parts.length === 2 && parts[0].length <= 40 && parts[1].length <= 120) {
                    out.push({label: parts[0], value: parts[1]});
                  }
                }
                return out.slice(0, 300);
                """
            )
        except Exception:
            items = []

        data: dict[str, str] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip().lower()
            value = str(item.get("value") or "").strip()
            if not label or not value:
                continue
            key = self._spec_key_to_snake(label)
            if key and key not in data:
                data[key] = value
        return data

    def _pick_field(self, fields: dict[str, str], candidates: tuple[str, ...]) -> str | None:
        for key in candidates:
            value = fields.get(key)
            if value:
                return value
        return None

    def _infer_brand_model_from_title(self, title: str | None, year: int | None) -> tuple[str | None, str | None, str | None]:
        text = re.sub(r"\s+", " ", str(title or "")).strip()
        if not text:
            return None, None, None
        text = re.sub(r"\b(dijual|bekas|baru|mobil|harga|indonesia)\b", " ", text, flags=re.IGNORECASE)
        if year:
            text = re.sub(rf"\b{year}\b", " ", text)
        text = re.sub(r"\bRp\s*[\d.]+\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"[|,-].*$", "", text).strip()
        parts = [p for p in text.split() if p]
        if not parts:
            return None, None, None
        brand = parts[0]
        model = parts[1] if len(parts) > 1 else None
        variant = " ".join(parts[2:]) if len(parts) > 2 else None
        return brand, model, variant or None

    def _infer_title_fields(self, title: str | None) -> dict[str, object]:
        text = re.sub(r"\s+", " ", str(title or "")).strip()
        if not text:
            return {}

        out: dict[str, object] = {}
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
        if year_match:
            out["year"] = int(year_match.group(1))
            text = re.sub(rf"\b{year_match.group(1)}\b", " ", text).strip()

        color_words = (
            "Putih", "Hitam", "Silver", "Abu", "Merah", "Biru", "Coklat", "Hijau", "Kuning", "Oranye", "Gold", "Grey", "Gray", "White", "Black"
        )
        for color in color_words:
            if re.search(rf"\b{re.escape(color)}\b$", text, re.IGNORECASE):
                out["color"] = color
                text = re.sub(rf"\b{re.escape(color)}\b$", "", text, flags=re.IGNORECASE).strip()
                break

        body_types = ("SUV", "MPV", "Sedan", "Hatchback", "Coupe", "Van", "Wagon", "Pickup", "Truck")
        for body_type in body_types:
            if re.search(rf"\b{re.escape(body_type)}\b", text, re.IGNORECASE):
                out["body_type"] = body_type
                text = re.sub(rf"\b{re.escape(body_type)}\b", "", text, flags=re.IGNORECASE).strip()
                break

        transmissions = ("Automatic", "Manual", "CVT", "Matic")
        for transmission in transmissions:
            if re.search(rf"\b{re.escape(transmission)}\b", text, re.IGNORECASE):
                out["transmission"] = "Automatic" if transmission.lower() in {"matic", "cvt"} else transmission
                text = re.sub(rf"\b{re.escape(transmission)}\b", "", text, flags=re.IGNORECASE).strip()
                break

        text = re.sub(r"\bdi\s+.+$", "", text, flags=re.IGNORECASE).strip()
        parts = [part for part in text.split() if part]
        if parts:
            out["brand"] = parts[0]
        if len(parts) > 1:
            out["model"] = parts[1]
        if len(parts) > 2:
            out["variant"] = " ".join(parts[2:])
        return out

    def _extract_images_botasaurus(self, driver) -> list[str] | None:
        try:
            urls = driver.run_js(
                """
                const isValid = (url) => /^https?:\/\//i.test(String(url || '').trim());
                const root = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue || document.querySelector('.c-main-gallery') || document.querySelector('.c-gallery');
                if (!root) return [];
                const out = [];
                const attrs = ['src', 'data-src', 'data-lazy', 'data-original'];
                for (const img of Array.from(root.querySelectorAll('img'))) {
                  for (const attr of attrs) {
                    const v = img.getAttribute(attr);
                    if (v && isValid(v)) out.push(v);
                  }
                  const srcset = img.getAttribute('srcset');
                  if (srcset) {
                    for (const part of srcset.split(',')) {
                      const url = part.trim().split(' ')[0];
                      if (url && isValid(url)) out.push(url);
                    }
                  }
                }
                return Array.from(new Set(out.filter(Boolean)));
                """,
                {"xpath": DETAIL_GALLERY_XPATH},
            )
        except Exception:
            urls = []

        deduped: list[str] = []
        seen: set[str] = set()
        for u in urls or []:
            if not isinstance(u, str):
                continue
            url = u.strip()
            if not url.startswith(("http://", "https://")):
                continue
            if "icarcdn.com" not in url or "mobil-bekas-mobil123" not in url:
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        return deduped or None

    def _normalize_whatsapp_number(self, value: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        digits = re.sub(r"\D+", "", text)
        if not digits:
            return None
        if digits.startswith("0") and len(digits) >= 9:
            return f"62{digits[1:]}"
        return digits

    def _extract_listing_urls_botasaurus(self, driver) -> list[str]:
        try:
            hrefs = driver.run_js(
                """
                const links = Array.from(document.querySelectorAll("a[href]"))
                  .map((a) => a.href)
                  .filter(Boolean);
                return Array.from(new Set(links));
                """
            )
        except Exception:
            hrefs = []

        urls: list[str] = []
        for href in hrefs or []:
            if not isinstance(href, str):
                continue
            if self._is_listing_detail_url(href):
                urls.append(href.split("#", 1)[0])

        return list(dict.fromkeys(urls))

    def _build_error_result(
        self,
        url: str,
        *,
        listing_id: str | None = None,
        error: str = "error",
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        return {
            "listing_id": listing_id,
            "listing_url": url,
            "condition": None,
            "brand": None,
            "model": None,
            "variant": None,
            "information_ads": None,
            "information_ads_date": None,
            "location": None,
            "price": None,
            "year": None,
            "mileage": None,
            "transmission": None,
            "seat_capacity": None,
            "engine_cc": None,
            "fuel_type": None,
            "series": None,
            "type": None,
            "carousel": None,
            "seller_notes": None,
            "specifications": None,
            "equipment": None,
            "images": None,
            "whatsapp_number": None,
            "contact_seller": None,
            "seller_name": None,
            "status": "active",
            "error": error,
            "error_type": error_type,
            "error_message": error_message,
        }

    def _sold_result(self, *, listing_id: str, url: str) -> dict:
        result = self._build_error_result(url, listing_id=listing_id, error=None)
        result["status"] = "sold"
        return result

    def _scrape_single_detail_botasaurus(self, driver, *, url: str, cfg, listing_id: str | None, tracking_mode: bool) -> dict:
        self._navigate_botasaurus(driver, url, cfg)
        time.sleep(1.0)
        self._dismiss_mobil123_popups_botasaurus(driver)

        current_url = str(getattr(driver, "current_url", "") or "")
        expected_id = (listing_id or self._extract_listing_id(url)).strip()
        redirected_off_detail = bool(current_url) and (not self._is_on_target_domain(current_url))
        is_http_error_page = self._is_http_error_page_botasaurus(driver)

        if is_http_error_page:
            raise RuntimeError(f"Retryable error page while scraping mobil123 detail: current_url={current_url!r}")

        if self._is_access_blocked_botasaurus(driver):
            raise RuntimeError(f"Blocked while scraping mobil123 detail: current_url={current_url!r}")

        is_not_found_page = self._is_listing_not_found_botasaurus(driver)
        is_sold_page = self._is_sold_page_botasaurus(driver)

        if is_not_found_page or is_sold_page or redirected_off_detail:
            if expected_id:
                # MOBIL123-TRACK-02: only an explicit sold page may mark sold.
                # not_found / redirected pages must never be treated as sold.
                should_mark_sold = bool(is_sold_page)
                if not should_mark_sold:
                    raise RuntimeError(
                        "Detail page unavailable but not confidently sold. "
                        f"current_url={current_url!r} redirected_off_detail={redirected_off_detail} "
                        f"is_not_found_page={is_not_found_page} is_sold_page={is_sold_page}"
                    )
                return self._sold_result(listing_id=expected_id, url=url)
            raise RuntimeError("Listing not found/redirected (missing listing_id).")

        wait_for_xpath(
            driver,
            INFORMATION_ADS_XPATH,
            timeout_seconds=cfg.detail_wait_seconds,
            poll_interval_seconds=cfg.poll_interval_seconds,
        )

        listing_id_meta = self._get_attr_by_xpath_botasaurus(driver, LISTING_ID_META_XPATH, "content")
        listing_id_from_url = (listing_id or self._extract_numeric_listing_id(url) or "").strip()

        listing_id_meta_numeric = None
        if listing_id_meta:
            match = re.search(r"\d{5,}", str(listing_id_meta))
            if match:
                listing_id_meta_numeric = match.group(0)

        if listing_id_meta_numeric and listing_id_from_url and listing_id_meta_numeric != listing_id_from_url:
            if not self._logged_listing_id_meta_mismatch:
                self._logged_listing_id_meta_mismatch = True
                self.logger.warning(
                    "Ignoring mismatched listing_id from meta (meta=%r url=%r) for %s",
                    listing_id_meta_numeric,
                    listing_id_from_url,
                    url,
                )
            listing_id_meta_numeric = None

        listing_id_final = (
            listing_id_from_url
            or listing_id_meta_numeric
            or (self._extract_numeric_listing_id(current_url) or "").strip()
            or self._extract_listing_id(url)
            or ""
        ).strip()
        if not listing_id_final:
            raise RuntimeError("Missing listing_id on page")

        json_ld_items = self._json_ld_objects_botasaurus(driver)
        condition_value = self._normalize_condition_value(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:type", "variant_type"])
            or self._first_json_ld_value(json_ld_items, "itemCondition")
            or self._get_attr_by_xpath_botasaurus(driver, CONDITION_META_XPATH, "content")
        )
        brand_value = self._clean_short_text(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:make", "WT.z_make"])
            or self._first_json_ld_value(json_ld_items, "brand")
            or self._get_attr_by_xpath_botasaurus(driver, BRAND_META_XPATH, "content")
        )
        model_value = self._clean_short_text(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:model", "WT.z_model"])
            or self._first_json_ld_value(json_ld_items, "model")
            or self._get_attr_by_xpath_botasaurus(driver, MODEL_META_XPATH, "content")
        )
        variant_value = self._clean_variant_text(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:variant"])
            or self._get_attr_by_xpath_botasaurus(driver, VARIANT_META_XPATH, "content")
        )

        info_ads_value = self._extract_information_ads_botasaurus(driver)
        info_ads_date_value = self._parse_information_ads_date(info_ads_value)

        location_value = (
            self._extract_legacy_location_botasaurus(driver)
            or self._extract_location_botasaurus(driver)
        )
        price_value = self._parse_rupiah_price(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:price", "WT.z_price"])
            or self._get_attr_by_xpath_botasaurus(driver, PRICE_META_XPATH, "content")
        )
        year_value = self._clean_year(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:year", "WT.z_year"])
            or self._first_json_ld_value(json_ld_items, "vehicleModelDate")
            or self._get_attr_by_xpath_botasaurus(driver, YEAR_META_XPATH, "content")
        )
        key_details_fields = self._extract_key_details_dict_botasaurus(driver)
        mileage_value = self._parse_first_mileage(
            self._get_text_by_xpath_botasaurus(driver, MILEAGE_XPATH),
            key_details_fields.get("mileage"),
            self._extract_mileage_from_bold_span_botasaurus(driver),
            self._extract_text_by_nearby_label_botasaurus(driver, ("mileage", "kilometer", "kilometres", "kilometers")),
        )
        transmission_value = self._clean_short_text(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:transmission", "WT.z_transmission"])
            or self._first_json_ld_value(json_ld_items, "vehicleTransmission")
            or self._get_attr_by_xpath_botasaurus(driver, TRANSMISSION_META_XPATH, "content")
        )
        seat_capacity_value = self._normalize_seat_capacity(
            self._get_text_by_xpath_botasaurus(driver, SEAT_CAPACITY_XPATH)
            or key_details_fields.get("seat_capacity")
            or key_details_fields.get("seats")
            or self._extract_text_by_nearby_label_botasaurus(driver, ("seat capacity", "seats", "kursi"))
        )
        engine_cc_value = self._normalize_engine_cc(
            self._get_text_by_xpath_botasaurus(driver, ENGINE_CC_XPATH)
            or key_details_fields.get("engine_cc")
            or key_details_fields.get("engine_capacity")
            or self._extract_text_by_nearby_label_botasaurus(driver, ("engine cc", "engine capacity", "kapasitas mesin"))
        )
        fuel_type_value = self._clean_short_text(
            self._first_meta_content_botasaurus(driver, ["ga:cad:details:fuel_type", "WT.z_Fueltype"])
            or self._first_json_ld_value(json_ld_items, "fuelType")
            or self._get_attr_by_xpath_botasaurus(driver, FUEL_TYPE_META_XPATH, "content")
            or key_details_fields.get("fuel_type")
            or key_details_fields.get("fuel")
            or self._extract_text_by_nearby_label_botasaurus(driver, ("fuel type", "fuel", "bahan bakar"))
        )
        images_value = self._extract_legacy_images_botasaurus(driver) or self._extract_images_botasaurus(driver)
        whatsapp_numbers_value = self._extract_whatsapp_numbers_botasaurus(
            driver,
            listing_id=listing_id_final,
        )
        contact_seller_value = self._extract_contact_seller_value_botasaurus(driver, listing_id=listing_id_final)
        seller_name_value = (
            self._extract_seller_name_botasaurus(driver, listing_id=listing_id_final)
            or self._get_text_by_xpath_botasaurus(driver, DETAIL_SELLER_NAME_XPATH)
        )

        def has_value(v) -> bool:
            if v is None:
                return False
            if isinstance(v, str):
                return bool(v.strip())
            if isinstance(v, (list, tuple, set, dict)):
                return bool(v)
            if isinstance(v, (int, float)):
                return v != 0
            return True

        has_any_data = any(
            has_value(v)
            for v in (
                condition_value,
                brand_value,
                model_value,
                variant_value,
                info_ads_value,
                info_ads_date_value,
                price_value,
                year_value,
                location_value,
                mileage_value,
                transmission_value,
                seat_capacity_value,
                engine_cc_value,
                fuel_type_value,
                images_value,
            )
        )
        if not has_any_data:
            if listing_id_final and self._is_sold_page_botasaurus(driver):
                status = "sold"
            else:
                raise RuntimeError("No extractable data on detail page (treating as error, not sold).")
        else:
            status = "active"

        carousel_value = None
        seller_notes_value = None
        specifications_value: dict[str, object] | None = None
        equipment_value: dict[str, object] | None = None
        if status == "active":
            carousel_value = self._extract_carousel_dict_botasaurus(driver)

            if self._click_tab_by_href_botasaurus(driver, href=TAB_SELLER_NOTES_SELECTOR):
                time.sleep(0.2)
            seller_notes_value = self._extract_seller_notes_text_botasaurus(driver)

            if self._click_tab_by_href_botasaurus(driver, href=TAB_SPECIFICATIONS_SELECTOR):
                time.sleep(0.2)
            specifications_value = self._extract_tab_grouped_kv_botasaurus(
                driver,
                tab_selector=TAB_SPECIFICATIONS_SELECTOR,
            )
            flat_specifications = self._flatten_grouped_kv(specifications_value)
            if not mileage_value:
                mileage_value = self._parse_first_mileage(
                    self._pick_field(flat_specifications, ("mileage", "kilometer", "kilometers", "kilometres"))
                )
            if not seat_capacity_value:
                seat_capacity_value = self._normalize_seat_capacity(
                    self._pick_field(flat_specifications, ("seat_capacity", "seats", "kursi"))
                )
            if not engine_cc_value:
                engine_cc_value = self._normalize_engine_cc(
                    self._pick_field(flat_specifications, ("engine_cc", "engine_capacity", "kapasitas_mesin"))
                )
            if not fuel_type_value:
                fuel_type_value = self._pick_field(flat_specifications, ("fuel_type", "fuel", "bahan_bakar"))

            if self._click_tab_by_href_botasaurus(driver, href=TAB_EQUIPMENTS_SELECTOR):
                time.sleep(0.2)
            equipment_value = self._extract_tab_grouped_kv_botasaurus(
                driver,
                tab_selector=TAB_EQUIPMENTS_SELECTOR,
            )

        return {
            "listing_id": listing_id_final,
            "listing_url": url,
            "condition": condition_value if status == "active" else None,
            "brand": brand_value if status == "active" else None,
            "model": model_value if status == "active" else None,
            "variant": variant_value if status == "active" else None,
            "information_ads": info_ads_value if status == "active" else None,
            "information_ads_date": info_ads_date_value if status == "active" else None,
            "location": location_value if status == "active" else None,
            "price": price_value if status == "active" else None,
            "year": year_value if status == "active" else None,
            "mileage": mileage_value if status == "active" else None,
            "transmission": transmission_value if status == "active" else None,
            "seat_capacity": seat_capacity_value if status == "active" else None,
            "engine_cc": engine_cc_value if status == "active" else None,
            "fuel_type": fuel_type_value if status == "active" else None,
            "series": None,
            "type": None,
            "carousel": carousel_value if status == "active" else None,
            "seller_notes": seller_notes_value if status == "active" else None,
            "specifications": specifications_value if status == "active" else None,
            "equipment": equipment_value if status == "active" else None,
            "images": images_value if status == "active" else None,
            "whatsapp_number": whatsapp_numbers_value if status == "active" else None,
            "contact_seller": contact_seller_value if status == "active" else None,
            "seller_name": seller_name_value if status == "active" else None,
            "error": None,
            "status": status,
        }

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1) -> list[str]:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        url = (start_url or self.listing_url).strip()
        if not url:
            raise ValueError("Missing start URL. Set MOBIL123_CARS_LISTING_URL or pass --start-url.")

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="mobil123")
        self._maybe_log_proxy_and_ip(cfg)

        @browser(**cfg.browser_kwargs)
        def run(driver: Driver, data):
            if cfg.human_mode:
                try:
                    driver.enable_human_mode()
                except Exception:
                    pass

            self.logger.info("Navigating (botasaurus): %s", data["url"])
            return self._load_listing_urls_with_same_session_retries(driver, url=data["url"], cfg=cfg)

        try:
            links = run({"url": url, "max_pages": max_pages}) or []
        except Exception as exc:
            if self._is_retryable_http_400_error(exc):
                base_profile = str(cfg.browser_kwargs.get("profile") or "mobil123")
                links = self._retry_listing_http_400_with_new_session(
                    url=url,
                    max_pages=max_pages,
                    base_profile=base_profile,
                ) or []
            else:
                raise
        self.logger.info("Collected %s links.", len(links))
        return links

    def scrape_listing_details(
        self,
        links: list[str],
        *,
        listing_id_by_url: dict[str, str] | None = None,
        allow_revive_sold: bool = False,
        require_brand_model: bool = False,
    ) -> list[dict]:
        batch_size = int(os.getenv("DB_BATCH_SIZE", "100"))
        if batch_size < 1:
            batch_size = 1

        def has_text(value: object) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            return True

        def bounded_text(value: object, *, field: str, listing_id: str, max_len: int) -> str | None:
            if value is None:
                return None
            text = re.sub(r"\s+", " ", str(value)).strip()
            if not text:
                return None
            if len(text) <= max_len:
                return text
            self.logger.warning(
                "Truncating mobil123 %s for listing_id=%s from %s to %s chars: %r",
                field,
                listing_id,
                len(text),
                max_len,
                text[:160],
            )
            return text[:max_len]

        columns = (
            "listing_id",
            "listing_url",
            "condition",
            "brand",
            "model",
            "variant",
            "information_ads",
            "information_ads_date",
            "location",
            "price",
            "year",
            "mileage",
            "transmission",
            "seat_capacity",
            "engine_cc",
            "fuel_type",
            "series",
            "type",
            "images",
            "whatsapp_number",
            "contact_seller",
            "seller_name",
            "status",
        )

        status_update_sql = (
            "status = COALESCE(EXCLUDED.status, cars_scrap_mobil123.status)"
            if allow_revive_sold
            else """status = CASE
            WHEN cars_scrap_mobil123.status = 'sold' THEN cars_scrap_mobil123.status
            ELSE COALESCE(EXCLUDED.status, cars_scrap_mobil123.status)
          END"""
        )
        upsert_sql = f"""
        INSERT INTO cars_scrap_mobil123 ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          condition = COALESCE(EXCLUDED.condition, cars_scrap_mobil123.condition),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_mobil123.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_mobil123.model),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_mobil123.variant),
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_mobil123.information_ads),
          information_ads_date = COALESCE(EXCLUDED.information_ads_date, cars_scrap_mobil123.information_ads_date),
          location = COALESCE(EXCLUDED.location, cars_scrap_mobil123.location),
          price = COALESCE(EXCLUDED.price, cars_scrap_mobil123.price),
          year = COALESCE(EXCLUDED.year, cars_scrap_mobil123.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_mobil123.mileage),
          transmission = COALESCE(EXCLUDED.transmission, cars_scrap_mobil123.transmission),
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_mobil123.seat_capacity),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_mobil123.engine_cc),
          fuel_type = COALESCE(EXCLUDED.fuel_type, cars_scrap_mobil123.fuel_type),
          series = COALESCE(EXCLUDED.series, cars_scrap_mobil123.series),
          type = COALESCE(EXCLUDED.type, cars_scrap_mobil123.type),
          images = COALESCE(EXCLUDED.images, cars_scrap_mobil123.images),
          whatsapp_number = COALESCE(EXCLUDED.whatsapp_number, cars_scrap_mobil123.whatsapp_number),
          contact_seller = COALESCE(EXCLUDED.contact_seller, cars_scrap_mobil123.contact_seller),
          seller_name = COALESCE(EXCLUDED.seller_name, cars_scrap_mobil123.seller_name),
          {status_update_sql},
          version = COALESCE(cars_scrap_mobil123.version, 0) + 1,
          last_scraped_at = NOW()
        """

        details_columns = (
            "listing_id",
            "listing_url",
            "carousel",
            "seller_notes",
            "specifications",
            "equipment",
        )
        details_upsert_sql = f"""
        INSERT INTO cars_scrap_mobil123_details ({", ".join(details_columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          carousel = COALESCE(EXCLUDED.carousel, cars_scrap_mobil123_details.carousel),
          seller_notes = COALESCE(EXCLUDED.seller_notes, cars_scrap_mobil123_details.seller_notes),
          specifications = COALESCE(EXCLUDED.specifications, cars_scrap_mobil123_details.specifications),
          equipment = COALESCE(EXCLUDED.equipment, cars_scrap_mobil123_details.equipment)
        """

        sold_upsert_sql = """
        UPDATE cars_scrap_mobil123
        SET
          listing_url = COALESCE(%s, listing_url),
          status = 'sold',
          version = COALESCE(version, 0) + 1,
          last_scraped_at = NOW()
        WHERE listing_id = %s
        """

        conn = get_connection()
        cursor = conn.cursor()
        rows: list[tuple] = []
        details_rows: list[tuple] = []
        sold_rows: list[tuple] = []

        def flush_rows() -> None:
            if (not rows) and (not details_rows):
                return
            if rows:
                deduped: dict[str, tuple] = {}
                dup_count = 0
                for row in rows:
                    key = str(row[0] or "")
                    if key in deduped:
                        dup_count += 1
                    deduped[key] = row
                if dup_count:
                    self.logger.warning("Deduped %s duplicate cars rows by listing_id before upsert.", dup_count)
                rows[:] = list(deduped.values())

            if details_rows:
                deduped_details: dict[str, tuple] = {}
                dup_details_count = 0
                for row in details_rows:
                    key = str(row[0] or "")
                    if key in deduped_details:
                        dup_details_count += 1
                    deduped_details[key] = row
                if dup_details_count:
                    self.logger.warning(
                        "Deduped %s duplicate car details rows by listing_id before upsert.", dup_details_count
                    )
                details_rows[:] = list(deduped_details.values())

            try:
                if rows:
                    execute_values(cursor, upsert_sql, rows, page_size=100)
                if details_rows:
                    execute_values(cursor, details_upsert_sql, details_rows, page_size=100)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            rows.clear()
            details_rows.clear()

        def flush_sold_rows() -> None:
            if not sold_rows:
                return
            try:
                cursor.executemany(sold_upsert_sql, sold_rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            sold_rows.clear()

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="mobil123")
        self._maybe_log_proxy_and_ip(cfg)

        circuit_breaker_enabled = self._mobil123_env_bool("DETAIL_CIRCUIT_BREAKER_ENABLED", True)
        circuit_breaker_min_samples = self._mobil123_env_int("DETAIL_CIRCUIT_BREAKER_MIN_SAMPLES", 5, minimum=1)
        circuit_breaker_error_rate = self._mobil123_env_float("DETAIL_CIRCUIT_BREAKER_ERROR_RATE", 0.7, minimum=0.0)
        circuit_breaker_consecutive_errors = self._mobil123_env_int(
            "DETAIL_CIRCUIT_BREAKER_CONSECUTIVE_ERRORS",
            4,
            minimum=1,
        )

        @browser(**cfg.browser_kwargs)
        def run(driver: Driver, data):
            if cfg.human_mode:
                try:
                    driver.enable_human_mode()
                except Exception:
                    pass

            results: list[dict] = []
            processed_count = 0
            retryable_browser_error_count = 0
            consecutive_retryable_browser_errors = 0
            for idx, url in enumerate(data["links"], start=1):
                if self.stop_flag:
                    break

                url = (url or "").strip()
                if not url:
                    continue

                self.logger.info("Detail %s/%s: %s", idx, len(data["links"]), url)

                listing_id = listing_id_by_url.get(url) if listing_id_by_url else None
                result = None
                try:
                    tracking_mode = bool(listing_id_by_url)
                    result = self._scrape_detail_with_same_session_retries(
                        driver,
                        url=url,
                        cfg=cfg,
                        listing_id=listing_id,
                        tracking_mode=tracking_mode,
                    )
                except Exception as exc:
                    tracking_mode = bool(listing_id_by_url)
                    if self._is_retryable_http_400_error(exc):
                        base_profile = str(cfg.browser_kwargs.get("profile") or "mobil123")
                        retried = self._retry_http_400_with_new_session(
                            url=url,
                            listing_id=listing_id,
                            tracking_mode=tracking_mode,
                            base_profile=base_profile,
                        )
                        if retried is not None:
                            result = retried
                        else:
                            self.logger.warning("Error while scraping %s: %s", url, exc)
                            result = self._build_error_result(
                                url,
                                listing_id=listing_id,
                                error_type=self._classify_retryable_browser_error(exc),
                                error_message=str(exc),
                            )
                    else:
                        self.logger.warning("Error while scraping %s: %s", url, exc)
                        result = self._build_error_result(
                            url,
                            listing_id=listing_id,
                            error_type="scrape_error",
                            error_message=str(exc),
                        )

                results.append(result)
                processed_count += 1

                if result.get("error"):
                    error_type = str(result.get("error_type") or "").strip().lower()
                    if error_type in {"browser_error", "blocked", "soft_block"}:
                        retryable_browser_error_count += 1
                        consecutive_retryable_browser_errors += 1
                    else:
                        consecutive_retryable_browser_errors = 0
                else:
                    consecutive_retryable_browser_errors = 0

                if circuit_breaker_enabled and processed_count >= circuit_breaker_min_samples:
                    browser_error_rate = retryable_browser_error_count / processed_count if processed_count else 0.0
                    if (
                        browser_error_rate >= circuit_breaker_error_rate
                        or consecutive_retryable_browser_errors >= circuit_breaker_consecutive_errors
                    ):
                        self.logger.warning(
                            "Mobil123 detail circuit breaker tripped. processed=%s retryable_browser_errors=%s error_rate=%.2f consecutive=%s",
                            processed_count,
                            retryable_browser_error_count,
                            browser_error_rate,
                            consecutive_retryable_browser_errors,
                        )
                        break

                row_listing_id = result.get("listing_id")
                if row_listing_id and not result.get("error"):
                    status = str(result.get("status") or "active")
                    if status != "active":
                        if tracking_mode:
                            sold_rows.append((result.get("listing_url"), str(row_listing_id)))
                            if len(sold_rows) >= batch_size:
                                flush_sold_rows()
                        continue
                    if require_brand_model and status == "active":
                        if not has_text(result.get("brand")) or not has_text(result.get("model")):
                            result["skipped_upsert"] = True
                            result["skip_reason"] = "missing_brand_or_model"
                            continue
                    rows.append(
                        (
                            str(row_listing_id),
                            result.get("listing_url"),
                            bounded_text(result.get("condition"), field="condition", listing_id=str(row_listing_id), max_len=50),
                            bounded_text(result.get("brand"), field="brand", listing_id=str(row_listing_id), max_len=100),
                            bounded_text(result.get("model"), field="model", listing_id=str(row_listing_id), max_len=100),
                            bounded_text(result.get("variant"), field="variant", listing_id=str(row_listing_id), max_len=100),
                            result.get("information_ads"),
                            result.get("information_ads_date"),
                            result.get("location"),
                            result.get("price"),
                            result.get("year"),
                            result.get("mileage"),
                            result.get("transmission"),
                            bounded_text(result.get("seat_capacity"), field="seat_capacity", listing_id=str(row_listing_id), max_len=10),
                            bounded_text(result.get("engine_cc"), field="engine_cc", listing_id=str(row_listing_id), max_len=50),
                            result.get("fuel_type"),
                            bounded_text(result.get("series"), field="series", listing_id=str(row_listing_id), max_len=100),
                            bounded_text(result.get("type"), field="type", listing_id=str(row_listing_id), max_len=100),
                            result.get("images"),
                            result.get("whatsapp_number"),
                            result.get("contact_seller"),
                            result.get("seller_name"),
                            status,
                        )
                    )

                    details_rows.append(
                        (
                            str(row_listing_id),
                            result.get("listing_url"),
                            Json(result.get("carousel")) if result.get("carousel") else None,
                            result.get("seller_notes"),
                            Json(result.get("specifications")) if result.get("specifications") else None,
                            Json(result.get("equipment")) if result.get("equipment") else None,
                        )
                    )

                    if len(rows) >= batch_size or len(details_rows) >= batch_size:
                        flush_rows()

            flush_rows()
            flush_sold_rows()
            return results

        try:
            return run({"links": links}) or []
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def scrape_listing_details_with_run_retries(
        self,
        links: list[str],
        *,
        listing_id_by_url: dict[str, str] | None = None,
        allow_revive_sold: bool = False,
        require_brand_model: bool = False,
    ) -> list[dict]:
        original_urls = [str(url or "").strip() for url in links if str(url or "").strip()]
        if not original_urls:
            return []

        max_attempts = self._mobil123_env_int("RUN_MAX_ATTEMPTS", 2, minimum=1)
        retry_wait_seconds = self._mobil123_env_float("RUN_RETRY_WAIT_SECONDS", 5.0, minimum=0.0)

        initial_profile = (os.getenv("BOTASAURUS_PROFILE") or "").strip()
        results_by_url: dict[str, dict] = {}
        pending_urls = list(original_urls)

        for attempt in range(1, max_attempts + 1):
            if not pending_urls:
                break

            previous_profile = os.getenv("BOTASAURUS_PROFILE")
            if attempt > 1:
                base_profile = initial_profile or "mobil123"
                retry_profile = f"{base_profile}_run_r{attempt}_{str(time.time_ns())[-8:]}"
                os.environ["BOTASAURUS_PROFILE"] = retry_profile
                self._logged_proxy_ip = False
                self.logger.warning(
                    "Retrying mobil123 run for %s error listing(s). attempt=%s/%s profile=%s",
                    len(pending_urls),
                    attempt,
                    max_attempts,
                    retry_profile,
                )

            try:
                reset_cached_proxy()
                subset_map = None
                if listing_id_by_url:
                    subset_map = {url: listing_id_by_url[url] for url in pending_urls if url in listing_id_by_url}

                attempt_results = self.scrape_listing_details(
                    pending_urls,
                    listing_id_by_url=subset_map,
                    allow_revive_sold=allow_revive_sold,
                    require_brand_model=require_brand_model,
                )
            finally:
                reset_cached_proxy()
                if previous_profile is None:
                    os.environ.pop("BOTASAURUS_PROFILE", None)
                else:
                    os.environ["BOTASAURUS_PROFILE"] = previous_profile

            indexed_results: dict[str, dict] = {}
            for row in attempt_results or []:
                item = row or {}
                url = str(item.get("listing_url") or "").strip()
                if url:
                    indexed_results[url] = item

            next_pending: list[str] = []
            for url in pending_urls:
                row = indexed_results.get(url)
                if row is None:
                    row = self._build_error_result(
                        url,
                        listing_id=listing_id_by_url.get(url) if listing_id_by_url else None,
                        error_type="missing_result",
                        error_message="missing_result_from_run",
                    )
                results_by_url[url] = row
                if row.get("error"):
                    next_pending.append(url)

            pending_urls = next_pending
            if pending_urls and attempt < max_attempts and retry_wait_seconds:
                time.sleep(retry_wait_seconds)

        for url in original_urls:
            if url not in results_by_url:
                results_by_url[url] = self._build_error_result(
                    url,
                    listing_id=listing_id_by_url.get(url) if listing_id_by_url else None,
                    error_type="missing_result",
                    error_message="missing_result_after_all_attempts",
                )

        return [results_by_url[url] for url in original_urls]
