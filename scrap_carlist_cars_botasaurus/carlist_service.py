from __future__ import annotations

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
from scrap_carlist_cars_botasaurus.database import get_connection

load_dotenv(override=True)

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

SOLD_MESSAGE_XPATH = "/html/body/main/article/section/section[1]/div/div/div/h2"
PAGE_NOT_FOUND_XPATH = "/html/body/main/section[1]/div/div[2]/h1"

CAROUSEL_SECTION_SELECTOR = "section.c-section--key-details"
TAB_SELLER_NOTES_SELECTOR = "#tab-seller-notes"
TAB_SPECIFICATIONS_SELECTOR = "#tab-specifications"
TAB_EQUIPMENTS_SELECTOR = "#tab-equipments"
WHATSAPP_PHONE_RE = re.compile(r"(?:wa\.me/|phone=|tel:)(\+?\d+)", re.IGNORECASE)


class CarlistCarsService:
    def __init__(self):
        self.logger = setup_logger("scrape_carlist_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
        self._logged_listing_id_meta_mismatch = False
        self.listing_url = os.getenv("CARLIST_CARS_LISTING_URL", "").strip()

    def _normalize_engine_cc(self, value: str | None) -> str | None:
        if not value:
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        digits = re.findall(r"\d+", text.replace(",", ""))
        if not digits:
            return None
        return "".join(digits)

    def _page_has_tokens_botasaurus(self, driver, tokens: tuple[str, ...]) -> bool:
        try:
            title = driver.run_js("return (document.title || '').toLowerCase();")
        except Exception:
            title = ""
        try:
            text = driver.run_js("return ((document.body && document.body.innerText) || '').toLowerCase();")
        except Exception:
            try:
                text = (driver.page_html or "").lower()
            except Exception:
                text = ""
        haystack = f"{title}\n{text}".lower()
        return any(token in haystack for token in tokens)

    def _text_by_xpath_botasaurus(self, driver, xpath: str) -> str | None:
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
                const raw = (el.innerText || el.textContent || '').trim();
                return raw || null;
                """,
                {"xpath": xpath},
            )
        except Exception:
            text = None
        return text or None

    def _is_sold_page_botasaurus(self, driver) -> bool:
        sold_text = (self._text_by_xpath_botasaurus(driver, SOLD_MESSAGE_XPATH) or "").strip().lower()
        if sold_text and ("already been sold" in sold_text or "has been sold" in sold_text):
            return True

        not_found_text = (self._text_by_xpath_botasaurus(driver, PAGE_NOT_FOUND_XPATH) or "").strip().lower()
        if not_found_text and ("page not found" in not_found_text or "not found" == not_found_text):
            return True

        return self._page_has_tokens_botasaurus(
            driver,
            (
                "this car has already been sold",
                "car has already been sold",
                "already been sold",
                "page not found",
                "we can’t seem to find the page you’re looking for",
                "we can't seem to find the page you're looking for",
                "cant seem to find the page youre looking for",
            ),
        )

    def _is_http_error_page_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "http error 400",
                "400 bad request",
                "request header or cookie too large",
                "this page isn't working",
                "this page isnt working",
                "this site can't be reached",
                "this site cant be reached",
                "err_tunnel_connection_failed",
                "tunnel_connection_failed",
                "err_proxy_connection_failed",
            ),
        )

    def close(self) -> None:
        return

    def stop_scraping(self) -> None:
        self.stop_flag = True

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

    def _extract_listing_id(self, url: str) -> str:
        numeric = self._extract_numeric_listing_id(url)
        if numeric:
            return numeric
        return sha1(url.encode("utf-8")).hexdigest()[:16]

    def _build_error_result(self, url: str) -> dict:
        return {
            "listing_id": None,
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
            "error": "error",
        }

    def _is_retryable_http_400_error(self, exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        return "retryable error page while scraping carlist" in text

    def _build_retry_cfg_for_http_400(self, *, base_profile: str, retry_index: int, listing_key: str):
        previous_profile = os.getenv("BOTASAURUS_PROFILE")
        suffix = str(retry_index).strip() or "1"
        unique = str(time.time_ns())[-8:]
        retry_profile = f"{base_profile}_http400_r{suffix}_{listing_key}_{unique}"

        try:
            os.environ["BOTASAURUS_PROFILE"] = retry_profile
            reset_cached_proxy()
            cfg = load_botasaurus_config(service_name="carlist")
        finally:
            if previous_profile is None:
                os.environ.pop("BOTASAURUS_PROFILE", None)
            else:
                os.environ["BOTASAURUS_PROFILE"] = previous_profile
            reset_cached_proxy()

        return cfg

    def _retry_http_400_with_new_session(
        self,
        *,
        url: str,
        listing_id: str | None,
        tracking_mode: bool,
        base_profile: str,
    ) -> dict | None:
        if not env_bool("CARLIST_HTTP_400_RETRY_ENABLED", True):
            return None
        try:
            max_retries = max(1, int((os.getenv("CARLIST_HTTP_400_MAX_RETRIES") or "2").strip()))
        except Exception:
            max_retries = 2

        from botasaurus.browser import Driver, browser

        listing_key = (listing_id or self._extract_numeric_listing_id(url) or self._extract_listing_id(url) or "unknown").strip()

        for retry_index in range(1, max_retries + 1):
            cfg = self._build_retry_cfg_for_http_400(
                base_profile=base_profile,
                retry_index=retry_index,
                listing_key=listing_key,
            )
            self.logger.warning(
                "Retrying carlist detail after browser error page. retry=%s/%s profile=%s proxy=%s url=%s",
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
                if retry_index < max_retries:
                    self.logger.warning("Retry session %s returned no result for %s; trying next proxy/profile.", retry_index, url)
                    continue
                return None
            except Exception as exc:
                if self._is_retryable_http_400_error(exc) and retry_index < max_retries:
                    self.logger.warning("Browser error retry %s still failed for %s: %s", retry_index, url, exc)
                    continue
                self.logger.warning("Retry session failed for %s: %s", url, exc)
                return None

        return None

    def _retry_listing_http_400_with_new_session(
        self,
        *,
        url: str,
        max_pages: int,
        base_profile: str,
    ) -> list[str] | None:
        if not env_bool("CARLIST_HTTP_400_RETRY_ENABLED", True):
            return None
        try:
            max_retries = max(1, int((os.getenv("CARLIST_HTTP_400_MAX_RETRIES") or "2").strip()))
        except Exception:
            max_retries = 2

        from botasaurus.browser import Driver, browser

        for retry_index in range(1, max_retries + 1):
            cfg = self._build_retry_cfg_for_http_400(
                base_profile=base_profile,
                retry_index=retry_index,
                listing_key="listing",
            )
            self.logger.warning(
                "Retrying carlist listing after browser error page. retry=%s/%s profile=%s proxy=%s url=%s",
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
                self._navigate_botasaurus(driver, data["url"], cfg)
                time.sleep(max(0.0, cfg.listing_wait_seconds))
                if self._is_http_error_page_botasaurus(driver):
                    current_url = str(getattr(driver, "current_url", "") or "")
                    raise RuntimeError(f"Retryable error page while scraping carlist listing: current_url={current_url!r}")
                return self._extract_listing_urls_botasaurus(driver)

            try:
                links = run({"url": url, "max_pages": max_pages})
                if links is not None:
                    return links
                if retry_index < max_retries:
                    self.logger.warning("Listing retry session %s returned no result for %s; trying next proxy/profile.", retry_index, url)
                    continue
                return None
            except Exception as exc:
                if self._is_retryable_http_400_error(exc) and retry_index < max_retries:
                    self.logger.warning("Listing retry %s still failed for %s: %s", retry_index, url, exc)
                    continue
                self.logger.warning("Listing retry session failed for %s: %s", url, exc)
                return None

        return None

    def _attempt_cloudflare_auto_solve_botasaurus(self, driver, *, target_url: str) -> None:
        """
        Let Botasaurus try solving the active Cloudflare widget in the same browser tab.

        This is specifically useful for the carlist interstitial "Verify you are human"
        page, where a solver running in another browser session would not carry over the
        same clearance/cookies.
        """
        if not env_bool("CARLIST_AUTO_ATTEMPT_CLOUDFLARE", True):
            return
        if not is_cloudflare_challenge(driver):
            return

        try:
            max_attempts = max(1, int((os.getenv("CARLIST_CLOUDFLARE_MAX_ATTEMPTS") or "2").strip()))
        except Exception:
            max_attempts = 2
        try:
            post_wait_seconds = max(
                0.0, float((os.getenv("CARLIST_CLOUDFLARE_POST_SOLVE_WAIT_SECONDS") or "4").strip())
            )
        except Exception:
            post_wait_seconds = 4.0

        for attempt in range(1, max_attempts + 1):
            current_url = str(getattr(driver, "current_url", "") or "")
            self.logger.warning(
                "Cloudflare challenge detected on carlist. Auto-solve attempt %s/%s. target_url=%r current_url=%r",
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

    def _extract_numeric_listing_id(self, url: str) -> str | None:
        match = re.search(r"(?:-|/)(\d{5,})(?:[/?#.]|$)", url)
        if match:
            return match.group(1)
        return None

    def _get_attr_by_xpath_botasaurus(self, driver, xpath: str, attr: str) -> str | None:
        try:
            val = driver.run_js(
                """
                const el = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!el) return null;
                const v = el.getAttribute(args.attr);
                return (v || '').trim() || null;
                """,
                {"xpath": xpath, "attr": attr},
            )
        except Exception:
            val = None
        return val or None

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
                const raw = (el.innerText || el.textContent || '').trim();
                return raw || null;
                """,
                {"xpath": xpath},
            )
        except Exception:
            text = None
        return text or None

    def _parse_int_from_text(self, text: str | None) -> int | None:
        if not text:
            return None
        digits = re.findall(r"\d+", str(text))
        if not digits:
            return None
        try:
            return int("".join(digits))
        except Exception:
            return None

    def _parse_mileage(self, mileage_str: str | None) -> int:
        if not mileage_str or mileage_str.strip() == "- km":
            return 0
        try:
            if "-" in mileage_str:
                right = mileage_str.split("-")[-1].strip()
            else:
                right = mileage_str.strip()
            right = right.replace("km", "").replace("KM", "").replace("Km", "").strip()
            right = right.replace("K", "000").replace("k", "000")
            right = right.replace(" ", "")
            return int(re.sub(r"[^\d]", "", right))
        except Exception:
            return 0

    def _spec_key_to_snake(self, label: str) -> str:
        raw = str(label or "").strip().lower()
        if not raw:
            return ""
        raw = raw.replace("&", " and ")
        raw = re.sub(r"[()]+", " ", raw)
        raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        raw = re.sub(r"_+", "_", raw)
        return raw

    def _parse_information_ads_date(self, information_ads: str | None) -> date | None:
        """
        Converts carlist "Updated on: May 14, 2025" to a DATE.
        Supports:
        - May 14, 2025
        - 14 May 2025
        - 2025-05-14 or 2025/05/14
        Removes prefixes: Updated on:, Posted on:, Date:
        """
        if not information_ads:
            return None

        month_mapping = {
            "january": "01",
            "jan": "01",
            "february": "02",
            "feb": "02",
            "march": "03",
            "mar": "03",
            "april": "04",
            "apr": "04",
            "may": "05",
            "june": "06",
            "jun": "06",
            "july": "07",
            "jul": "07",
            "august": "08",
            "aug": "08",
            "september": "09",
            "sep": "09",
            "sept": "09",
            "october": "10",
            "oct": "10",
            "november": "11",
            "nov": "11",
            "december": "12",
            "dec": "12",
        }

        text = str(information_ads).lower().strip()
        text = re.sub(r"^(updated\s+on:\s*|posted\s+on:\s*|date:\s*)", "", text)

        match1 = re.search(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
        if match1:
            month_str, day, year = match1.groups()
            month_num = month_mapping.get(month_str[:3])
            if month_num:
                try:
                    return datetime(int(year), int(month_num), int(day)).date()
                except ValueError:
                    return None

        match2 = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
        if match2:
            day, month_str, year = match2.groups()
            month_num = month_mapping.get(month_str[:3])
            if month_num:
                try:
                    return datetime(int(year), int(month_num), int(day)).date()
                except ValueError:
                    return None

        match3 = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match3:
            year, month, day = match3.groups()
            try:
                return datetime(int(year), int(month), int(day)).date()
            except ValueError:
                return None

        return None

    def _extract_carousel_dict_botasaurus(self, driver) -> dict[str, object] | None:
        """
        Extract key/value pairs from the carousel (key details cards).
        Label class: 'u-text-7  u-color-muted  u-block'
        Value class: 'u-text-bold  u-block'
        """
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const root = document.querySelector(args.selector);
                if (!root) return [];

                const cards = Array.from(root.querySelectorAll('.c-key-details__item, .c-card'));
                const out = [];
                for (const el of cards) {
                  const labelEl = el.querySelector('span.u-text-7.u-color-muted.u-block');
                  const valueEl = el.querySelector('span.u-text-bold.u-block');
                  const label = norm(labelEl && (labelEl.textContent || labelEl.innerText));
                  const value = norm(valueEl && (valueEl.textContent || valueEl.innerText));
                  if (!label || !value) continue;
                  out.push({ label, value });
                }
                return out;
                """,
                {"selector": CAROUSEL_SECTION_SELECTOR},
            )
        except Exception:
            items = []

        data: dict[str, object] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "") or "").strip()
            value = str(item.get("value", "") or "").strip()
            if not label or not value:
                continue
            key = self._spec_key_to_snake(label)
            if not key:
                continue
            if key not in data:
                data[key] = value
                continue
            existing = data[key]
            if isinstance(existing, list):
                if value not in existing:
                    existing.append(value)
                continue
            if existing != value:
                data[key] = [existing, value]
        return data or None

    def _extract_seller_notes_text_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').trim();
                const tab = document.querySelector(args.selector);
                if (!tab) return null;
                const desc = tab.querySelector('[itemprop=\"description\"]') || tab;
                const text = norm(desc && (desc.innerText || desc.textContent));
                return text || null;
                """,
                {"selector": TAB_SELLER_NOTES_SELECTOR},
            )
        except Exception:
            return None

    def _click_tab_by_href_botasaurus(self, driver, *, href: str) -> bool:
        """
        Some tabs render content lazily only after being activated. Click the tab anchor by href.
        Example href: "#tab-specifications"
        """
        try:
            return bool(
                driver.run_js(
                    """
                    const href = String(args.href || '').trim();
                    if (!href) return false;
                    const links = Array.from(document.querySelectorAll('a.c-tab__item[data-toggle=\"tab\"][href]'));
                    const a = links.find((el) => String(el.getAttribute('href') || '').trim() === href);
                    if (!a) return false;
                    try { a.scrollIntoView({block: 'center'}); } catch (e) {}
                    a.click();
                    return true;
                    """,
                    {"href": href},
                )
            )
        except Exception:
            return False

    def _extract_tab_grouped_kv_botasaurus(self, driver, *, tab_selector: str) -> dict[str, object] | None:
        """
        Extract grouped key/value pairs from Specifications / Equipments tabs.
        - Category: <h3 class="u-text-6 u-text-uppercase ...">...</h3>
        - Row label: <span class="u-width-1/2">Key</span>
        - Row value: <span class="u-text-bold u-width-1/2 u-align-right">Value</span>

        Output example:
        {
          "general": {"doors": "5", "seat_capacity": "5"},
          "engine_specifications": {"engine_cc": "998"}
        }
        """
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const tab = document.querySelector(args.selector);
                if (!tab) return [];

                const groups = Array.from(tab.querySelectorAll('.o-grid')).filter((g) => g.querySelector('h3'));
                const out = [];
                for (const g of groups) {
                  const h3 = g.querySelector('h3');
                  const category = norm(h3 && (h3.textContent || h3.innerText));
                  if (!category) continue;
                  const rows = Array.from(g.querySelectorAll('div.u-flex.u-flex--justify-between'));
                  for (const row of rows) {
                    // NOTE: class "u-width-1/2" contains "/" which breaks CSS selectors unless escaped.
                    // Avoid brittle selectors and instead pick the bold span as value, and the other span as label.
                    const spans = Array.from(row.querySelectorAll('span'));
                    const valueEl = spans.find((s) => s && s.classList && s.classList.contains('u-text-bold')) || null;
                    const labelEl = spans.find((s) => s && s !== valueEl) || null;
                    const label = norm(labelEl && (labelEl.textContent || labelEl.innerText));
                    const value = norm(valueEl && (valueEl.textContent || valueEl.innerText));
                    if (!label || !value) continue;
                    out.push({ category, label, value });
                  }
                }
                return out;
                """,
                {"selector": tab_selector},
            )
        except Exception:
            items = []

        grouped: dict[str, object] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "") or "").strip()
            label = str(item.get("label", "") or "").strip()
            value = str(item.get("value", "") or "").strip()
            if not category or not label or not value:
                continue
            cat_key = self._spec_key_to_snake(category)
            key = self._spec_key_to_snake(label)
            if not cat_key or not key:
                continue
            cat_obj = grouped.get(cat_key)
            if not isinstance(cat_obj, dict):
                cat_obj = {}
                grouped[cat_key] = cat_obj

            if key not in cat_obj:
                cat_obj[key] = value
                continue
            existing = cat_obj[key]
            if isinstance(existing, list):
                if value not in existing:
                    existing.append(value)
                continue
            if existing != value:
                cat_obj[key] = [existing, value]

        return grouped or None

    def _is_on_target_domain(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return ("carlist" in host) or host.endswith("carlist.my")

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
            service_label="carlist",
            prompt_on_cloudflare=cfg.prompt_on_cloudflare,
        )

    def _is_listing_not_found_botasaurus(self, driver) -> bool:
        try:
            title = driver.run_js("return (document.title || '').toLowerCase();")
        except Exception:
            title = ""
        try:
            text = driver.run_js("return ((document.body && document.body.innerText) || '').toLowerCase();")
        except Exception:
            try:
                text = (driver.page_html or "").lower()
            except Exception:
                return False

        if (not title) and (not text):
            return False

        if any(token in (title or "") for token in ("page not found", "404", "not found")):
            return True

        body_tokens = (
            "we can’t seem to find the page you’re looking for",
            "we can't seem to find the page you're looking for",
            "cant seem to find the page youre looking for",
            "this ad is no longer available",
            "ad no longer available",
            "no longer available",
        )
        if any(token in (text or "") for token in body_tokens):
            return True

        return False

    def _is_access_blocked_botasaurus(self, driver) -> bool:
        if is_cloudflare_challenge(driver):
            return True
        if self._is_http_error_page_botasaurus(driver):
            return True
        try:
            text = driver.run_js("return ((document.body && document.body.innerText) || '').toLowerCase();")
        except Exception:
            try:
                text = (driver.page_html or "").lower()
            except Exception:
                return False
        if not text:
            return False
        return any(
            token in text
            for token in (
                "access denied",
                "forbidden",
                "request blocked",
                "your request has been blocked",
                "too many requests",
                "temporarily unavailable",
            )
        )

    def _scrape_single_detail_botasaurus(self, driver, *, url: str, cfg, listing_id: str | None, tracking_mode: bool) -> dict:
        self._navigate_botasaurus(driver, url, cfg)

        current_url = str(getattr(driver, "current_url", "") or "")
        expected_id = (listing_id or self._extract_listing_id(url)).strip()
        redirected_off_detail = bool(current_url) and (not self._is_on_target_domain(current_url))
        is_http_error_page = self._is_http_error_page_botasaurus(driver)

        if is_http_error_page:
            raise RuntimeError(f"Retryable error page while scraping carlist detail: current_url={current_url!r}")

        if self._is_access_blocked_botasaurus(driver):
            raise RuntimeError(f"Blocked while scraping carlist detail: current_url={current_url!r}")

        is_not_found_page = self._is_listing_not_found_botasaurus(driver)
        is_sold_page = self._is_sold_page_botasaurus(driver)

        if is_not_found_page or is_sold_page or redirected_off_detail:
            if expected_id:
                should_mark_sold = bool(is_sold_page) or (tracking_mode and (is_not_found_page or redirected_off_detail))
                if not should_mark_sold:
                    raise RuntimeError(
                        "Detail page unavailable but not confidently sold. "
                        f"current_url={current_url!r} redirected_off_detail={redirected_off_detail} "
                        f"is_not_found_page={is_not_found_page} is_sold_page={is_sold_page}"
                    )
                return {
                    "listing_id": expected_id,
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
                    "error": None,
                    "status": "sold",
                }
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

        condition_value = self._get_attr_by_xpath_botasaurus(driver, CONDITION_META_XPATH, "content")
        brand_value = self._get_attr_by_xpath_botasaurus(driver, BRAND_META_XPATH, "content")
        model_value = self._get_attr_by_xpath_botasaurus(driver, MODEL_META_XPATH, "content")
        variant_value = self._get_attr_by_xpath_botasaurus(driver, VARIANT_META_XPATH, "content")

        info_ads_value = self._get_text_by_xpath_botasaurus(driver, INFORMATION_ADS_XPATH)
        info_ads_date_value = self._parse_information_ads_date(info_ads_value)

        location_value = self._extract_location_botasaurus(driver)
        price_value = self._parse_int_from_text(self._get_attr_by_xpath_botasaurus(driver, PRICE_META_XPATH, "content"))
        year_value = self._parse_int_from_text(self._get_attr_by_xpath_botasaurus(driver, YEAR_META_XPATH, "content"))
        mileage_value = self._parse_mileage(self._get_text_by_xpath_botasaurus(driver, MILEAGE_XPATH))
        transmission_value = self._get_attr_by_xpath_botasaurus(driver, TRANSMISSION_META_XPATH, "content")
        seat_capacity_value = self._get_text_by_xpath_botasaurus(driver, SEAT_CAPACITY_XPATH)
        engine_cc_value = self._normalize_engine_cc(self._get_text_by_xpath_botasaurus(driver, ENGINE_CC_XPATH))
        fuel_type_value = self._get_attr_by_xpath_botasaurus(driver, FUEL_TYPE_META_XPATH, "content")
        images_value = self._extract_images_botasaurus(driver)
        whatsapp_numbers_value = self._extract_whatsapp_numbers_botasaurus(driver, listing_id=listing_id_final)
        contact_seller_value = self._extract_contact_seller_botasaurus(driver, listing_id=listing_id_final)
        seller_name_value = self._extract_seller_name_botasaurus(driver, listing_id=listing_id_final)

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
                location_value,
                price_value,
                year_value,
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
            specifications_value = self._extract_tab_grouped_kv_botasaurus(driver, tab_selector=TAB_SPECIFICATIONS_SELECTOR)

            if self._click_tab_by_href_botasaurus(driver, href=TAB_EQUIPMENTS_SELECTOR):
                time.sleep(0.2)
            equipment_value = self._extract_tab_grouped_kv_botasaurus(driver, tab_selector=TAB_EQUIPMENTS_SELECTOR)

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

    def _extract_location_botasaurus(self, driver) -> str | None:
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

    def _extract_images_botasaurus(self, driver) -> list[str] | None:
        try:
            urls = driver.run_js(
                """
                const isValid = (url) => /^https?:\\/\\//i.test(String(url || '').trim());
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

    def _normalize_whatsapp_number(self, value: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        digits = re.sub(r"\D+", "", text)
        if not digits:
            return None
        if digits.startswith("0") and len(digits) >= 9:
            return f"60{digits[1:]}"
        return digits

    def _extract_whatsapp_numbers_from_candidates(self, candidates: list[dict] | None) -> list[str] | None:
        seen: set[str] = set()
        numbers: list[str] = []
        for item in candidates or []:
            if not isinstance(item, dict):
                continue

            direct = self._normalize_whatsapp_number(item.get("data_whatsapp_number"))
            if direct and direct not in seen:
                seen.add(direct)
                numbers.append(direct)

            direct_phone = self._normalize_whatsapp_number(item.get("data_phone"))
            if direct_phone and direct_phone not in seen:
                seen.add(direct_phone)
                numbers.append(direct_phone)

            href = str(item.get("href") or "").strip()
            if not href:
                continue

            parsed_phone = None
            try:
                parsed = urlparse(href)
                query_phone = parse_qs(parsed.query).get("phone")
                if query_phone:
                    parsed_phone = query_phone[0]
            except Exception:
                parsed_phone = None

            phone = self._normalize_whatsapp_number(parsed_phone)
            if not phone:
                match = WHATSAPP_PHONE_RE.search(href)
                phone = self._normalize_whatsapp_number(match.group(1) if match else None)
            if phone and phone not in seen:
                seen.add(phone)
                numbers.append(phone)
        return numbers or None

    def _extract_whatsapp_candidates_botasaurus(self, driver, *, listing_id: str, modal_only: bool = False) -> list[dict] | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const listingId = String(args.listingId || '').trim();
                if (!listingId) return [];
                const root = args.modalOnly ? (document.querySelector('#modal-contact-seller') || document) : document;
                const nodes = Array.from(root.querySelectorAll('a[href], button, [role="button"]'));
                const out = [];
                for (const el of nodes) {
                  const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                  const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                  const href = el.href || el.getAttribute('href') || '';
                  const dataWhatsappNumber = el.getAttribute('data-whatsapp-number') || '';
                  const dataPhone = el.getAttribute('data-phone') || '';
                  const dataPhones = el.getAttribute('data-phones') || '';
                  const text = norm(el.textContent || el.innerText);
                  const cls = String(el.className || '');
                  const dataTarget = el.getAttribute('data-target') || '';
                  const dataToggle = el.getAttribute('data-toggle') || '';
                  const dataGaEvent = String(el.getAttribute('data-ga-event') || '');
                  const isModalPhoneLink = !!args.modalOnly && /^tel:/i.test(href);
                  const looksWhatsapp =
                    /whatsapp/i.test(text) ||
                    /whatsapp/i.test(cls) ||
                    /api\\.whatsapp\\.com/i.test(href) ||
                    /wa\\.me/i.test(href) ||
                    !!dataWhatsappNumber ||
                    /whatsapp/i.test(dataGaEvent) ||
                    isModalPhoneLink;
                  if (!looksWhatsapp) continue;
                  if (dataListingId && dataListingId !== listingId) continue;
                  if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) continue;
                  out.push({
                    href,
                    text,
                    class_name: cls,
                    data_whatsapp_number: dataWhatsappNumber || null,
                    data_phone: dataPhone || dataPhones || null,
                    data_listing_id: dataListingId || null,
                    data_tml_si: dataTmlSi || null,
                    data_target: dataTarget || null,
                    data_toggle: dataToggle || null,
                    data_ga_event: dataGaEvent || null,
                  });
                }
                return out;
                """,
                {"listingId": listing_id, "modalOnly": bool(modal_only)},
            )
        except Exception:
            items = []
        return items or None

    def _click_whatsapp_button_botasaurus(self, driver, *, listing_id: str) -> bool:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return False
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const listingId = String(args.listingId || '').trim();
                    const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                    const candidates = nodes.map((el) => {
                      const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                      const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                      if (dataListingId && dataListingId !== listingId) return null;
                      if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) return null;
                      const text = norm(el.textContent || el.innerText);
                      const cls = String(el.className || '').toLowerCase();
                      const href = String(el.href || el.getAttribute('href') || '').trim().toLowerCase();
                      const dataTarget = String(el.getAttribute('data-target') || '').trim();
                      const dataWhatsappNumber = String(el.getAttribute('data-whatsapp-number') || '').trim();
                      const dataGaEvent = String(el.getAttribute('data-ga-event') || '').trim().toLowerCase();
                      let score = 0;
                      if (cls.includes('jump-whatsapp')) score += 100;
                      if (cls.includes('whatsapp_name')) score += 90;
                      if (dataWhatsappNumber) score += 80;
                      if (href.includes('api.whatsapp.com') || href.includes('wa.me')) score += 70;
                      if (text.includes('whatsapp')) score += 60;
                      if (cls.includes('whatsapp')) score += 50;
                      if (dataGaEvent.includes('whatsapp')) score += 40;
                      if (dataTarget === '#modal-contact-seller') score += 5;
                      if (score <= 0) return null;
                      return { el, score, isAnchor: el.tagName === 'A' ? 1 : 0 };
                    }).filter(Boolean);
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => (b.score - a.score) || (b.isAnchor - a.isAnchor));
                    for (const item of candidates) {
                      const el = item.el;
                      try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                      try {
                        el.click();
                        return true;
                      } catch (e) {}
                    }
                    return false;
                    """,
                    {"listingId": listing_id},
                )
            )
        except Exception:
            return False

    def _close_contact_seller_modal_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const modal = document.querySelector('#modal-contact-seller');
                    if (!modal) return false;
                    const btn =
                      modal.querySelector('.js-modal-destroy') ||
                      modal.querySelector('[data-dismiss="modal"]') ||
                      modal.querySelector('button');
                    if (!btn) return false;
                    try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
                    try { btn.click(); } catch (e) { return false; }
                    return true;
                    """
                )
            )
        except Exception:
            return False

    def _extract_whatsapp_numbers_botasaurus(self, driver, *, listing_id: str, modal_wait_seconds: float = 4.0) -> list[str] | None:
        numbers = self._extract_whatsapp_numbers_from_candidates(
            self._extract_whatsapp_candidates_botasaurus(driver, listing_id=listing_id, modal_only=False)
        )
        if numbers:
            return numbers

        if not self._click_whatsapp_button_botasaurus(driver, listing_id=listing_id):
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            numbers = self._extract_whatsapp_numbers_from_candidates(
                self._extract_whatsapp_candidates_botasaurus(driver, listing_id=listing_id, modal_only=True)
            )
            if numbers:
                self._close_contact_seller_modal_botasaurus(driver)
                return numbers
        self._close_contact_seller_modal_botasaurus(driver)
        return None

    def _normalize_contact_seller(self, value: str | None) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return None
        if len(re.sub(r"\D+", "", text)) < 8:
            return None
        return text

    def _extract_contact_seller_candidates_botasaurus(
        self, driver, *, listing_id: str, modal_only: bool = False
    ) -> list[dict] | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const listingId = String(args.listingId || '').trim();
                if (!listingId) return [];
                const root = args.modalOnly ? (document.querySelector('#modal-contact-seller') || document) : document;
                const nodes = Array.from(root.querySelectorAll('a[href], button, [role="button"]'));
                const out = [];
                for (const el of nodes) {
                  const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                  const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                  if (dataListingId && dataListingId !== listingId) continue;
                  if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) continue;
                  const href = el.href || el.getAttribute('href') || '';
                  const text = norm(el.textContent || el.innerText);
                  const cls = String(el.className || '');
                  const dataTarget = el.getAttribute('data-target') || '';
                  const dataToggle = el.getAttribute('data-toggle') || '';
                  const dataPhone = el.getAttribute('data-phone') || '';
                  const dataPhones = el.getAttribute('data-phones') || '';
                  const dataGaEvent = String(el.getAttribute('data-ga-event') || '');
                  const looksPhone =
                    /contact/i.test(text) ||
                    /call now/i.test(text) ||
                    /phone/i.test(cls) ||
                    /jump-phone/i.test(cls) ||
                    /^tel:/i.test(href) ||
                    !!dataPhone ||
                    !!dataPhones ||
                    /ctr_call/i.test(dataGaEvent);
                  if (!looksPhone) continue;
                  out.push({
                    href,
                    text,
                    class_name: cls,
                    data_phone: dataPhone || dataPhones || null,
                    data_listing_id: dataListingId || null,
                    data_tml_si: dataTmlSi || null,
                    data_target: dataTarget || null,
                    data_toggle: dataToggle || null,
                    data_ga_event: dataGaEvent || null,
                  });
                }
                return out;
                """,
                {"listingId": listing_id, "modalOnly": bool(modal_only)},
            )
        except Exception:
            items = []
        return items or None

    def _extract_contact_seller_from_candidates(self, candidates: list[dict] | None) -> str | None:
        for item in candidates or []:
            if not isinstance(item, dict):
                continue
            text_value = self._normalize_contact_seller(item.get("text"))
            if text_value:
                return text_value

            phone_value = self._normalize_contact_seller(item.get("data_phone"))
            if phone_value:
                return phone_value

            href = str(item.get("href") or "").strip()
            if href.lower().startswith("tel:"):
                href_value = self._normalize_contact_seller(href.split(":", 1)[-1])
                if href_value:
                    return href_value
        return None

    def _click_contact_button_botasaurus(self, driver, *, listing_id: str) -> bool:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return False
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const listingId = String(args.listingId || '').trim();
                    const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                    const candidates = nodes.map((el) => {
                      const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                      const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                      if (dataListingId && dataListingId !== listingId) return null;
                      if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) return null;
                      const text = norm(el.textContent || el.innerText);
                      const cls = String(el.className || '').toLowerCase();
                      const href = String(el.href || el.getAttribute('href') || '').trim().toLowerCase();
                      const dataPhone = String(el.getAttribute('data-phone') || el.getAttribute('data-phones') || '').trim();
                      const dataGaEvent = String(el.getAttribute('data-ga-event') || '').trim().toLowerCase();
                      let score = 0;
                      if (cls.includes('jump-phone')) score += 100;
                      if (dataGaEvent.includes('ctr_call')) score += 80;
                      if (dataPhone) score += 60;
                      if (href.startsWith('tel:')) score += 50;
                      if (text.includes('contact')) score += 40;
                      if (text.includes('call now')) score += 30;
                      if (cls.includes('phone')) score += 20;
                      if (score <= 0) return null;
                      return { el, score, isAnchor: el.tagName === 'A' ? 1 : 0 };
                    }).filter(Boolean);
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => (b.score - a.score) || (b.isAnchor - a.isAnchor));
                    for (const item of candidates) {
                      const el = item.el;
                      try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                      try {
                        el.click();
                        return true;
                      } catch (e) {}
                    }
                    return false;
                    """,
                    {"listingId": listing_id},
                )
            )
        except Exception:
            return False

    def _extract_contact_seller_botasaurus(self, driver, *, listing_id: str, modal_wait_seconds: float = 3.0) -> str | None:
        if not self._click_contact_button_botasaurus(driver, listing_id=listing_id):
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            contact = self._extract_contact_seller_from_candidates(
                self._extract_contact_seller_candidates_botasaurus(driver, listing_id=listing_id, modal_only=True)
            )
            if contact:
                self._close_contact_seller_modal_botasaurus(driver)
                return contact
        self._close_contact_seller_modal_botasaurus(driver)
        return None

    def _extract_inline_seller_name_botasaurus(self, driver, *, listing_id: str) -> str | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const listingId = String(args.listingId || '').trim();
                if (!listingId) return null;

                const scoreHeading = (el) => {
                  if (!el) return -1;
                  const text = norm(el.textContent || el.innerText);
                  if (!text) return -1;
                  const lower = text.toLowerCase();
                  if (lower === 'contact' || lower === 'whatsapp' || lower === 'save') return -1;
                  if (lower === 'transmission' || lower === 'mileage' || lower === 'color' || lower === 'condition') {
                    return -1;
                  }
                  if (/^rm\\s*[\\d,]+/i.test(text)) return -1;
                  if (/^[\\d\\s+\\-()]+$/.test(text)) return -1;
                  let score = 0;
                  if (el.tagName === 'H6') score += 100;
                  if ((el.className || '').includes('u-margin-bottom-xxs')) score += 30;
                  if (text.length >= 4) score += 10;
                  if (!/\\d{3,}/.test(text)) score += 5;
                  return score;
                };

                const roots = [];
                const pushRoot = (node) => {
                  if (!node) return;
                  if (roots.includes(node)) return;
                  roots.push(node);
                };

                const ctas = Array.from(document.querySelectorAll('a[href], button, [role="button"]')).filter((el) => {
                  const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                  const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                  if (dataListingId && dataListingId !== listingId) return false;
                  if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) return false;
                  const text = norm(el.textContent || el.innerText).toLowerCase();
                  const cls = String(el.className || '').toLowerCase();
                  return (
                    text.includes('contact') ||
                    text.includes('whatsapp') ||
                    cls.includes('jump-phone') ||
                    cls.includes('jump-whatsapp')
                  );
                });

                for (const cta of ctas) {
                  let node = cta;
                  for (let i = 0; i < 6 && node; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const hasCandidateHeading = !!node.querySelector('h6.u-margin-bottom-xxs, h6');
                    const hasContactButtons = Array.from(node.querySelectorAll('a[href], button, [role="button"]')).some((el) => {
                      const text = norm(el.textContent || el.innerText).toLowerCase();
                      const cls = String(el.className || '').toLowerCase();
                      return (
                        text.includes('contact') ||
                        text.includes('whatsapp') ||
                        cls.includes('jump-phone') ||
                        cls.includes('jump-whatsapp')
                      );
                    });
                    if (hasCandidateHeading && hasContactButtons) {
                      pushRoot(node);
                    }
                  }
                }

                for (const root of roots) {
                  const headings = Array.from(root.querySelectorAll('h6.u-margin-bottom-xxs, h6'))
                    .filter((el) => scoreHeading(el) > 0)
                    .sort((a, b) => scoreHeading(b) - scoreHeading(a));
                  if (headings.length) {
                    return norm(headings[0].textContent || headings[0].innerText) || null;
                  }
                }
                return null;
                """,
                {"listingId": listing_id},
            )
        except Exception:
            return None

    def _extract_modal_seller_name_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const modal = document.querySelector('#modal-contact-seller');
                if (!modal) return null;
                const headings = Array.from(modal.querySelectorAll('h6.u-margin-bottom-xxs, h6'));
                for (const heading of headings) {
                  const text = norm(heading.textContent || heading.innerText);
                  if (!text) continue;
                  const lower = text.toLowerCase();
                  if (lower === 'contact' || lower === 'whatsapp' || lower === 'save') continue;
                  if (/^[\\d\\s+\\-()]+$/.test(text)) continue;
                  return text;
                }
                return null;
                """
            )
        except Exception:
            return None

    def _extract_seller_name_botasaurus(self, driver, *, listing_id: str, modal_wait_seconds: float = 3.0) -> str | None:
        name = self._extract_inline_seller_name_botasaurus(driver, listing_id=listing_id)
        if name:
            return name

        opened = self._click_whatsapp_button_botasaurus(driver, listing_id=listing_id)
        if not opened:
            opened = self._click_contact_button_botasaurus(driver, listing_id=listing_id)
        if not opened:
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            name = self._extract_modal_seller_name_botasaurus(driver)
            if name:
                self._close_contact_seller_modal_botasaurus(driver)
                return name
        self._close_contact_seller_modal_botasaurus(driver)
        return None

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
            if self._is_on_target_domain(href):
                if self._extract_numeric_listing_id(href):
                    urls.append(href)

        deduped = list(dict.fromkeys(urls))
        return deduped

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1) -> list[str]:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        url = (start_url or self.listing_url).strip()
        if not url:
            raise ValueError("Missing start URL. Set CARLIST_CARS_LISTING_URL or pass --start-url.")

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="carlist")
        self._maybe_log_proxy_and_ip(cfg)

        @browser(**cfg.browser_kwargs)
        def run(driver: Driver, data):
            if cfg.human_mode:
                try:
                    driver.enable_human_mode()
                except Exception:
                    pass

            self.logger.info("Navigating (botasaurus): %s", data["url"])
            self._navigate_botasaurus(driver, data["url"], cfg)

            time.sleep(max(0.0, cfg.listing_wait_seconds))
            if self._is_http_error_page_botasaurus(driver):
                current_url = str(getattr(driver, "current_url", "") or "")
                raise RuntimeError(f"Retryable error page while scraping carlist listing: current_url={current_url!r}")
            urls = self._extract_listing_urls_botasaurus(driver)
            if not urls and cfg.save_html_on_empty:
                try:
                    from botasaurus import bt

                    os.makedirs("botasaurus_test/output", exist_ok=True)
                    bt.write_html(driver.page_html, "botasaurus_test/output/carlist_cars")
                    self.logger.warning(
                        "No listing links found; saved HTML to botasaurus_test/output/carlist_cars.html"
                    )
                except Exception as exc:
                    self.logger.warning("Failed to save debug HTML: %s", exc)
            return urls

        try:
            links = run({"url": url, "max_pages": max_pages}) or []
        except Exception as exc:
            if self._is_retryable_http_400_error(exc):
                base_profile = str(cfg.browser_kwargs.get("profile") or "carlist")
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
            "status = COALESCE(EXCLUDED.status, cars_scrap_carlistmy.status)"
            if allow_revive_sold
            else """status = CASE
            WHEN cars_scrap_carlistmy.status = 'sold' THEN cars_scrap_carlistmy.status
            ELSE COALESCE(EXCLUDED.status, cars_scrap_carlistmy.status)
          END"""
        )
        upsert_sql = f"""
        INSERT INTO cars_scrap_carlistmy ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          condition = COALESCE(EXCLUDED.condition, cars_scrap_carlistmy.condition),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_carlistmy.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_carlistmy.model),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_carlistmy.variant),
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_carlistmy.information_ads),
          information_ads_date = COALESCE(EXCLUDED.information_ads_date, cars_scrap_carlistmy.information_ads_date),
          location = COALESCE(EXCLUDED.location, cars_scrap_carlistmy.location),
          price = COALESCE(EXCLUDED.price, cars_scrap_carlistmy.price),
          year = COALESCE(EXCLUDED.year, cars_scrap_carlistmy.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_carlistmy.mileage),
          transmission = COALESCE(EXCLUDED.transmission, cars_scrap_carlistmy.transmission),
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_carlistmy.seat_capacity),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_carlistmy.engine_cc),
          fuel_type = COALESCE(EXCLUDED.fuel_type, cars_scrap_carlistmy.fuel_type),
          series = COALESCE(EXCLUDED.series, cars_scrap_carlistmy.series),
          type = COALESCE(EXCLUDED.type, cars_scrap_carlistmy.type),
          images = COALESCE(EXCLUDED.images, cars_scrap_carlistmy.images),
          whatsapp_number = COALESCE(EXCLUDED.whatsapp_number, cars_scrap_carlistmy.whatsapp_number),
          contact_seller = COALESCE(EXCLUDED.contact_seller, cars_scrap_carlistmy.contact_seller),
          seller_name = COALESCE(EXCLUDED.seller_name, cars_scrap_carlistmy.seller_name),
          {status_update_sql},
          version = COALESCE(cars_scrap_carlistmy.version, 0) + 1,
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
        INSERT INTO cars_scrap_carlistmy_details ({", ".join(details_columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          carousel = COALESCE(EXCLUDED.carousel, cars_scrap_carlistmy_details.carousel),
          seller_notes = COALESCE(EXCLUDED.seller_notes, cars_scrap_carlistmy_details.seller_notes),
          specifications = COALESCE(EXCLUDED.specifications, cars_scrap_carlistmy_details.specifications),
          equipment = COALESCE(EXCLUDED.equipment, cars_scrap_carlistmy_details.equipment)
        """

        sold_upsert_sql = """
        UPDATE cars_scrap_carlistmy
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
                # Avoid Postgres "CardinalityViolation" when the same listing_id appears
                # multiple times within a single INSERT ... ON CONFLICT statement.
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

        cfg = load_botasaurus_config(service_name="carlist")
        self._maybe_log_proxy_and_ip(cfg)

        @browser(**cfg.browser_kwargs)
        def run(driver: Driver, data):
            if cfg.human_mode:
                try:
                    driver.enable_human_mode()
                except Exception:
                    pass

            results: list[dict] = []
            for idx, url in enumerate(data["links"], start=1):
                if self.stop_flag:
                    break

                url = (url or "").strip()
                if not url:
                    continue

                self.logger.info("Detail %s/%s: %s", idx, len(data["links"]), url)

                listing_id = None
                if listing_id_by_url:
                    listing_id = listing_id_by_url.get(url)
                if not listing_id:
                    listing_id = None

                result = None
                try:
                    tracking_mode = bool(listing_id_by_url)
                    result = self._scrape_single_detail_botasaurus(
                        driver,
                        url=url,
                        cfg=cfg,
                        listing_id=listing_id,
                        tracking_mode=tracking_mode,
                    )
                except Exception as exc:
                    tracking_mode = bool(listing_id_by_url)
                    if self._is_retryable_http_400_error(exc):
                        base_profile = str(cfg.browser_kwargs.get("profile") or "carlist")
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
                            result = self._build_error_result(url)
                    else:
                        self.logger.warning("Error while scraping %s: %s", url, exc)
                        result = self._build_error_result(url)

                results.append(result)

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
                            result.get("condition"),
                            result.get("brand"),
                            result.get("model"),
                            result.get("variant"),
                            result.get("information_ads"),
                            result.get("information_ads_date"),
                            result.get("location"),
                            result.get("price"),
                            result.get("year"),
                            result.get("mileage"),
                            result.get("transmission"),
                            result.get("seat_capacity"),
                            result.get("engine_cc"),
                            result.get("fuel_type"),
                            result.get("series"),
                            result.get("type"),
                            result.get("images"),
                            result.get("whatsapp_number"),
                            result.get("contact_seller"),
                            result.get("seller_name"),
                            status,
                        )
                    )

                    if status == "active":
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
