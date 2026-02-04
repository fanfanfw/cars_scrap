from __future__ import annotations

import os
import re
import time
from datetime import date, datetime
from hashlib import sha1
from urllib.parse import urlparse

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
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip
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


class CarlistCarsService:
    def __init__(self):
        self.logger = setup_logger("scrape_carlist_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
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

        links = run({"url": url, "max_pages": max_pages}) or []
        self.logger.info("Collected %s links.", len(links))
        return links

    def scrape_listing_details(self, links: list[str], *, listing_id_by_url: dict[str, str] | None = None) -> list[dict]:
        batch_size = int(os.getenv("DB_BATCH_SIZE", "100"))
        if batch_size < 1:
            batch_size = 1

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
            "status",
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
          status = CASE
            WHEN cars_scrap_carlistmy.status = 'sold' THEN cars_scrap_carlistmy.status
            ELSE COALESCE(EXCLUDED.status, cars_scrap_carlistmy.status)
          END,
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

                try:
                    self._navigate_botasaurus(driver, url, cfg)

                    current_url = str(getattr(driver, "current_url", "") or "")
                    tracking_mode = bool(listing_id_by_url)
                    expected_id = (listing_id or self._extract_listing_id(url)).strip()
                    redirected_off_detail = bool(current_url) and (not self._is_on_target_domain(current_url))

                    if self._is_access_blocked_botasaurus(driver):
                        raise RuntimeError(f"Blocked while scraping carlist detail: current_url={current_url!r}")

                    if self._is_listing_not_found_botasaurus(driver) or self._is_sold_page_botasaurus(driver) or redirected_off_detail:
                        if expected_id:
                            result = {
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
                                "error": None,
                                "status": "sold",
                            }
                            results.append(result)
                            sold_rows.append((result.get("listing_url"), expected_id))
                            if len(sold_rows) >= batch_size:
                                flush_sold_rows()
                            continue
                        raise RuntimeError("Listing not found/redirected (missing listing_id).")

                    wait_for_xpath(
                        driver,
                        INFORMATION_ADS_XPATH,
                        timeout_seconds=cfg.detail_wait_seconds,
                        poll_interval_seconds=cfg.poll_interval_seconds,
                    )

                    listing_id_meta = self._get_attr_by_xpath_botasaurus(driver, LISTING_ID_META_XPATH, "content")
                    listing_id_final = (listing_id_meta or listing_id or self._extract_listing_id(url) or "").strip()
                    if not listing_id_final:
                        raise RuntimeError("Missing listing_id on page")

                    condition_value = self._get_attr_by_xpath_botasaurus(driver, CONDITION_META_XPATH, "content")
                    brand_value = self._get_attr_by_xpath_botasaurus(driver, BRAND_META_XPATH, "content")
                    model_value = self._get_attr_by_xpath_botasaurus(driver, MODEL_META_XPATH, "content")
                    variant_value = self._get_attr_by_xpath_botasaurus(driver, VARIANT_META_XPATH, "content")

                    info_ads_value = self._get_text_by_xpath_botasaurus(driver, INFORMATION_ADS_XPATH)
                    info_ads_date_value = self._parse_information_ads_date(info_ads_value)

                    location_value = self._extract_location_botasaurus(driver)
                    price_value = self._parse_int_from_text(
                        self._get_attr_by_xpath_botasaurus(driver, PRICE_META_XPATH, "content")
                    )
                    year_value = self._parse_int_from_text(self._get_attr_by_xpath_botasaurus(driver, YEAR_META_XPATH, "content"))
                    mileage_value = self._parse_mileage(self._get_text_by_xpath_botasaurus(driver, MILEAGE_XPATH))
                    transmission_value = self._get_attr_by_xpath_botasaurus(driver, TRANSMISSION_META_XPATH, "content")
                    seat_capacity_value = self._get_text_by_xpath_botasaurus(driver, SEAT_CAPACITY_XPATH)
                    engine_cc_value = self._normalize_engine_cc(
                        self._get_text_by_xpath_botasaurus(driver, ENGINE_CC_XPATH)
                    )
                    fuel_type_value = self._get_attr_by_xpath_botasaurus(driver, FUEL_TYPE_META_XPATH, "content")
                    images_value = self._extract_images_botasaurus(driver)

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
                        specifications_value = self._extract_tab_grouped_kv_botasaurus(
                            driver, tab_selector=TAB_SPECIFICATIONS_SELECTOR
                        )

                        if self._click_tab_by_href_botasaurus(driver, href=TAB_EQUIPMENTS_SELECTOR):
                            time.sleep(0.2)
                        equipment_value = self._extract_tab_grouped_kv_botasaurus(
                            driver, tab_selector=TAB_EQUIPMENTS_SELECTOR
                        )

                    result = {
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
                        "error": None,
                        "status": status,
                    }

                    if status != "active":
                        results.append(result)
                        sold_rows.append((result.get("listing_url"), listing_id_final))
                        if len(sold_rows) >= batch_size:
                            flush_sold_rows()
                        continue

                except Exception as exc:
                    self.logger.warning("Error while scraping %s: %s", url, exc)
                    result = {
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
                        "status": "active",
                        "error": "error",
                    }

                results.append(result)

                row_listing_id = result.get("listing_id")
                if row_listing_id and not result.get("error"):
                    status = result.get("status")
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
                            str(status) if status else "active",
                        )
                    )

                    if str(status) == "active":
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
