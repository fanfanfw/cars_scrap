from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

from cars_scrap_common.botasaurus_utils import (
    is_cloudflare_challenge,
    load_botasaurus_config,
    raise_if_cloudflare_challenge,
    wait_for_css,
    wait_for_xpath,
)
from cars_scrap_common.env_utils import env_bool
from cars_scrap_common.logging_utils import setup_logger
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip
from scrap_mudahmy_cars_botasaurus.database import get_connection

load_dotenv(override=True)

LISTING_DETAIL_RE = re.compile(r"-\d+\.htm(?:$|[?#])", re.IGNORECASE)
WHATSAPP_NUMBER_RE = re.compile(r"wa\.me/(\d+)", re.IGNORECASE)

TOP_CONTAINER_XPATH = "/html/body/div[1]/main/div[2]/div[1]/section[1]/div/div"
# NOTE: Avoid absolute section indexes (they shift when banners appear).
# Car Specifications values are extracted via label-based parsing (see _extract_car_specs_map_botasaurus()).
LOCATION_XPATH = "/html/body/div[1]/main/div[2]/div[1]/section[1]/div/div/div[3]/div[4]/div"
PRICE_XPATH = "/html/body/div[1]/main/div[2]/div[1]/section[1]/div/div/div[2]/div[2]/div"
YEAR_XPATH_PRIMARY = "/html/body/div[1]/main/div[2]/div[1]/section[1]/div/div/div[3]/div[1]/div"
YEAR_XPATH_FALLBACK = "/html/body/div[1]/main/div[2]/div[1]/section[4]/div/div/div/div/div/div[1]/div[3]/div[2]"
TRANSMISSION_XPATH = "/html/body/div[1]/main/div[2]/div[1]/section[1]/div/div/div[3]/div[2]/div"
MILEAGE_XPATH = "/html/body/div[1]/main/div[2]/div[1]/section[1]/div/div/div[3]/div[3]/div"

ENGINE_CC_XPATH = "/html/body/div[1]/main/div[2]/div[1]/section[4]/div/div/div[2]/div/div/div[1]/div[1]/div[2]"

IMAGE_GRID_CONTAINER_XPATH = "/html/body/div[1]/main/div[2]/section/div[2]"

AD_HIGHLIGHTS_SECTION_SELECTOR = "section#ad_view_ad_highlights"
DESCRIPTION_SECTION_SELECTOR = "section#ad_view_description"
CAR_SPECIFICATIONS_SECTION_SELECTOR = "section#ad_view_car_specifications"


class MudahMyCarsService:
    def __init__(self):
        self.logger = setup_logger("scrape_mudahmy_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
        self.listing_url = os.getenv("MUDAHMY_CARS_LISTING_URL", "").strip()

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

    def close(self) -> None:
        return

    def stop_scraping(self) -> None:
        self.stop_flag = True

    def _extract_listing_id(self, url: str) -> str:
        match = re.search(r"-(\d+)\.htm", url, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        raise ValueError(f"Unable to extract numeric listing_id from url={url!r}")

    def _is_listing_detail_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        if not host.endswith("mudah.my"):
            return False
        return bool(LISTING_DETAIL_RE.search(parsed.path))

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

        if ("iklan" in text) and ("tidak" in text) and ("dijumpai" in text):
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
            service_label="mudah.my",
            prompt_on_cloudflare=cfg.prompt_on_cloudflare,
        )

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
        digits = re.findall(r"\d+", text.replace(",", ""))
        if not digits:
            return None
        try:
            return int("".join(digits))
        except Exception:
            return None

    def _parse_price(self, text: str | None) -> int | None:
        return self._parse_int_from_text(text)

    def _normalize_label(self, text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
        return re.sub(r"\s+", " ", cleaned)

    def _spec_key_to_snake(self, label: str) -> str:
        raw = str(label or "").strip().lower()
        if not raw:
            return ""
        raw = raw.replace("&", " and ")
        raw = re.sub(r"[()]+", " ", raw)
        raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        raw = re.sub(r"_+", "_", raw)
        return raw

    def convert_mileage(self, mileage_str: str | None) -> int | None:
        """Convert mileage string to integer (km). Example: '<4k' -> 4000."""
        if not mileage_str or mileage_str == "N/A":
            return None
        raw = mileage_str.strip().lower()
        if not raw:
            return None
        try:
            # Handle cases like "<4k"
            if raw.startswith("<"):
                inner = raw[1:].strip()
                if inner.endswith("k"):
                    return int(float(inner[:-1]) * 1000)
                return int(inner.replace(",", "").replace("km", "").strip())

            # Handle ranges like "10k - 20k"
            if " - " in raw:
                parts = [p.strip() for p in raw.split(" - ") if p.strip()]
                max_part = parts[-1] if parts else raw
                if max_part.endswith("k"):
                    return int(float(max_part[:-1]) * 1000)
                return int(max_part.replace(",", "").replace("km", "").strip())

            # Handle ">500k"
            if raw.startswith(">"):
                inner = raw[1:].strip()
                if inner.endswith("k"):
                    return int(float(inner[:-1]) * 1000)
                return int(inner.replace(",", "").replace("km", "").strip())

            # Handle normal cases with "k"
            if raw.endswith("k"):
                return int(float(raw[:-1]) * 1000)

            # Handle pure numbers
            return int(raw.replace(",", "").replace("km", "").strip())
        except Exception:
            return None

    def _clean_condition_or_info(self, text: str | None) -> str | None:
        if not text:
            return None
        cleaned = re.sub(r"^[,\s]+", "", text.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or None

    def _convert_information_ads_to_date(self, info_ads_str: str | None):
        """
        Converts mudah.my "posted ..." string to a DATE (not timestamp).

        Mirrors the logic used in `property_scrap`:
        - "mins ago"/"hours ago"/"today" -> today
        - "yesterday" -> today-1
        - "N days/weeks ago" -> today-N (or today-7*N)
        - "DD Mon" -> inferred year (current year or previous year if month > current month)
        """
        if not info_ads_str:
            return None
        raw = info_ads_str.strip()
        if not raw or raw.upper() == "N/A":
            return None

        info_text = raw.lower().strip()
        now_dt = datetime.now()
        today = now_dt.date()

        if "mins ago" in info_text or "min ago" in info_text:
            return today
        if "hours ago" in info_text or "hour ago" in info_text:
            return today
        if "today" in info_text:
            return today
        if "yesterday" in info_text:
            return (now_dt - timedelta(days=1)).date()

        if "days ago" in info_text:
            days_match = re.search(r"(\d+)\s+days?\s+ago", info_text)
            if not days_match:
                return None
            days_ago = int(days_match.group(1))
            return (now_dt - timedelta(days=days_ago)).date()

        if "day ago" in info_text:
            return (now_dt - timedelta(days=1)).date()

        if "weeks ago" in info_text or "week ago" in info_text:
            weeks_match = re.search(r"(\d+)\s+weeks?\s+ago", info_text)
            if not weeks_match:
                return None
            weeks_ago = int(weeks_match.group(1))
            return (now_dt - timedelta(days=weeks_ago * 7)).date()

        date_match = re.search(r"(\d+)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", info_text)
        if not date_match:
            return None

        day = int(date_match.group(1))
        month_str = date_match.group(2)
        month_map = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        month = month_map.get(month_str)
        if not month:
            return None

        year = now_dt.year - 1 if month > now_dt.month else now_dt.year
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    def _extract_condition_and_information_ads(self, driver) -> tuple[str | None, str | None]:
        """
        Condition + information_ads berada dalam 1 text block (kadang ada URGENT).
        Contoh: "New Car for Sale, posted 3 mins ago"
        """
        try:
            texts = driver.run_js(
                """
                const root = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!root) return [];
                const items = Array.from(root.querySelectorAll("div"))
                  .map((d) => (d.innerText || d.textContent || "").trim())
                  .filter(Boolean);
                const seen = new Set();
                const out = [];
                for (const t of items) {
                  if (seen.has(t)) continue;
                  seen.add(t);
                  out.push(t);
                }
                return out;
                """,
                {"xpath": TOP_CONTAINER_XPATH},
            )
        except Exception:
            texts = []

        meta_text = None
        for t in texts or []:
            if not isinstance(t, str):
                continue
            tl = t.lower()
            if "posted" in tl or "ago" in tl or "today" in tl or "yesterday" in tl:
                meta_text = t
                break

        if not meta_text:
            return None, None

        raw = meta_text.strip()
        if "," in raw:
            left, right = raw.split(",", 1)
            return self._clean_condition_or_info(left), self._clean_condition_or_info(right)

        lower = raw.lower()
        idx = lower.find("posted")
        if idx > 0:
            left = raw[:idx].rstrip(" ,")
            right = raw[idx:].lstrip(" ,")
            return self._clean_condition_or_info(left), self._clean_condition_or_info(right)

        return self._clean_condition_or_info(raw), None

    def _click_show_more_description_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').trim();
                    const section = document.querySelector(args.selector);
                    if (!section) return false;
                    const buttons = Array.from(section.querySelectorAll('button'));
                    const btn = buttons.find((b) => (norm(b.textContent) || '').toUpperCase().includes('SHOW MORE'));
                    if (!btn) return false;
                    try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
                    btn.click();
                    return true;
                    """,
                    {"selector": DESCRIPTION_SECTION_SELECTOR},
                )
            )
        except Exception:
            return False

    def _extract_description_text_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').trim();
                const section = document.querySelector(args.selector);
                if (!section) return null;
                const content =
                  section.querySelector('div.mw44.mw45') ||
                  section.querySelector('[class*=\"mw44\"]') ||
                  section;
                const text = norm(content && (content.innerText || content.textContent));
                return text || null;
                """,
                {"selector": DESCRIPTION_SECTION_SELECTOR},
            )
        except Exception:
            return None

    def _extract_ad_highlights_text_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').trim();
                const section = document.querySelector(args.selector);
                if (!section) return null;
                const text = norm(section.innerText || section.textContent);
                return text || null;
                """,
                {"selector": AD_HIGHLIGHTS_SECTION_SELECTOR},
            )
        except Exception:
            return None

    def _extract_car_specifications_dict_botasaurus(self, driver) -> dict[str, object]:
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                const section = document.querySelector(args.selector);
                if (!section) return [];
                const labels = Array.from(section.querySelectorAll('div.text-mudah-grey'))
                  .filter((el) => (el.className || '').includes('text-sm') && (el.className || '').includes('flex-1'));
                const out = [];
                for (const labelEl of labels) {
                  const valueEl = labelEl.nextElementSibling;
                  if (!valueEl) continue;
                  if (!(valueEl.className || '').includes('text-sm')) continue;
                  if (!(valueEl.className || '').includes('flex-1')) continue;
                  const label = norm(labelEl.textContent || labelEl.innerText);
                  const value = norm(valueEl.textContent || valueEl.innerText);
                  if (!label || !value) continue;
                  out.push({ label, value });
                }
                return out;
                """,
                {"selector": CAR_SPECIFICATIONS_SECTION_SELECTOR},
            )
        except Exception:
            items = []

        specs: dict[str, object] = {}
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
            if key not in specs:
                specs[key] = value
                continue
            existing = specs[key]
            if isinstance(existing, list):
                if value not in existing:
                    existing.append(value)
                continue
            if existing != value:
                specs[key] = [existing, value]
        return specs

    def _extract_car_specs_map_botasaurus(self, driver) -> dict[str, str]:
        """
        Extract key/value pairs from the 'Car Specifications' section by reading labels, to avoid
        brittle absolute XPaths (section indices can shift due to banners like #ad_view_vp_banner).
        """
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').trim();
                const h2s = Array.from(document.querySelectorAll('h2'));
                const h2 = h2s.find((el) => norm(el.textContent) === 'Car Specifications');
                if (!h2) return [];
                let root = h2.closest('div');
                if (!root) return [];
                // The SHOW MORE button is typically a sibling of the <div> containing the <h2>.
                // Walk up a few levels until we find a container that includes both the specs rows and the button.
                for (let i = 0; i < 6; i++) {
                  const btn = Array.from(root.querySelectorAll('button'))
                    .find((b) => (norm(b.textContent) || '').toUpperCase().includes('SHOW MORE'));
                  if (btn) break;
                  if (!root.parentElement) break;
                  root = root.parentElement;
                }

                // Each row looks like: [icon] [label div] [value div]
                const rows = Array.from(root.querySelectorAll('div'))
                  .filter((el) => {
                    const label = el.querySelector('div.text-mudah-grey');
                    const value = label ? label.nextElementSibling : null;
                    return !!(label && value);
                  });

                const out = [];
                for (const row of rows) {
                  const labelEl = row.querySelector('div.text-mudah-grey');
                  const valueEl = labelEl ? labelEl.nextElementSibling : null;
                  const label = norm(labelEl && (labelEl.textContent || labelEl.innerText));
                  const value = norm(valueEl && (valueEl.textContent || valueEl.innerText));
                  if (label && value) out.push({ label, value });
                }
                return out;
                """
            )
        except Exception:
            items = []

        label_map = {
            "brand": "brand",
            "model": "model",
            "year": "year",
            "variant": "variant",
            "series": "series",
            "type": "type",
            "seats": "seat_capacity",
            "fuel type": "fuel_type",
            # Possible variants for engine capacity label (depends on UI variant / show more)
            "engine cc": "engine_cc",
            "engine capacity": "engine_cc",
            "engine": "engine_cc",
            "cc": "engine_cc",
        }

        specs: dict[str, str] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            label = self._normalize_label(str(item.get("label", "")))
            value = str(item.get("value", "")).strip()
            if not label or not value:
                continue
            key = label_map.get(label)
            if key and key not in specs:
                specs[key] = value
        return specs

    def _click_show_more_car_specs_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').trim();
                    const h2s = Array.from(document.querySelectorAll('h2'));
                    const h2 = h2s.find((el) => norm(el.textContent) === 'Car Specifications');
                    if (!h2) return false;
                    let root = h2.closest('div');
                    if (!root) return false;
                    // The button is often outside the inner div that contains the <h2>.
                    let btn = null;
                    for (let i = 0; i < 6; i++) {
                      const btns = Array.from(root.querySelectorAll('button'));
                      btn = btns.find((b) => (norm(b.textContent) || '').toUpperCase().includes('SHOW MORE'));
                      if (btn) break;
                      if (!root.parentElement) break;
                      root = root.parentElement;
                    }
                    if (!btn) return false;
                    try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
                    btn.click();
                    return true;
                    """
                )
            )
        except Exception:
            return False

    def _extract_car_spec_value_by_label_botasaurus(self, driver, *, label_candidates: list[str]) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').trim();
                const labels = (args.labels || []).map((s) => String(s || '').toLowerCase().trim()).filter(Boolean);
                const h2s = Array.from(document.querySelectorAll('h2'));
                const h2 = h2s.find((el) => norm(el.textContent) === 'Car Specifications');
                if (!h2) return null;
                let root = h2.closest('div');
                if (!root) return null;
                for (let i = 0; i < 6; i++) {
                  const btn = Array.from(root.querySelectorAll('button'))
                    .find((b) => (norm(b.textContent) || '').toUpperCase().includes('SHOW MORE'));
                  if (btn) break;
                  if (!root.parentElement) break;
                  root = root.parentElement;
                }

                const labelEls = Array.from(root.querySelectorAll('div.text-mudah-grey'));
                for (const el of labelEls) {
                  const label = (norm(el.textContent) || '').toLowerCase();
                  if (!labels.some((x) => label === x || label.includes(x))) continue;
                  const valueEl = el.nextElementSibling;
                  const value = norm(valueEl && (valueEl.textContent || valueEl.innerText));
                  if (value) return value;
                }
                return null;
                """,
                {"labels": label_candidates},
            )
        except Exception:
            return None

    def _click_by_xpath_botasaurus(self, driver, xpath: str) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const el = document.evaluate(
                      args.xpath,
                      document,
                      null,
                      XPathResult.FIRST_ORDERED_NODE_TYPE,
                      null
                    ).singleNodeValue;
                    if (!el) return false;
                    try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                    try { el.click(); } catch (e) { return false; }
                    return true;
                    """,
                    {"xpath": xpath},
                )
            )
        except Exception:
            return False

    def _extract_whatsapp_numbers_from_hrefs(self, hrefs: list[str] | None) -> list[str] | None:
        seen: set[str] = set()
        numbers: list[str] = []
        for href in hrefs or []:
            if not href:
                continue
            match = WHATSAPP_NUMBER_RE.search(str(href))
            if not match:
                continue
            number = match.group(1).strip()
            if not number or number in seen:
                continue
            seen.add(number)
            numbers.append(number)
        return numbers or None

    def _extract_whatsapp_links_from_dom_botasaurus(self, driver) -> list[str] | None:
        try:
            hrefs = driver.run_js(
                """
                const out = [];
                for (const anchor of Array.from(document.querySelectorAll("a[href*='wa.me/']"))) {
                  const href = anchor.href || anchor.getAttribute('href') || '';
                  if (href) out.push(href);
                }
                return Array.from(new Set(out));
                """
            )
        except Exception:
            hrefs = []
        return hrefs or None

    def _click_whatsapp_button_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const controls = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const buttons = controls.filter((el) => {
                      const text = norm(el.textContent || el.innerText);
                      return text === 'whatsapp' || text.includes('whatsapp');
                    });
                    if (!buttons.length) return false;
                    for (const button of buttons) {
                      try { button.scrollIntoView({block: 'center'}); } catch (e) {}
                      try {
                        button.click();
                        return true;
                      } catch (e) {}
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _close_whatsapp_modal_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const isVisible = (el) => {
                      if (!el) return false;
                      const style = window.getComputedStyle(el);
                      if (!style) return false;
                      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                        return false;
                      }
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    };
                    const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const closeButton = buttons.find((el) => {
                      const text = norm(el.textContent || el.innerText);
                      return text === 'close' || text.includes('close');
                    });
                    if (!closeButton || !isVisible(closeButton)) return false;
                    try { closeButton.scrollIntoView({block: 'center'}); } catch (e) {}
                    closeButton.click();
                    return true;
                    """
                )
            )
        except Exception:
            return False

    def _extract_whatsapp_numbers_botasaurus(self, driver, *, modal_wait_seconds: float = 1.5) -> list[str] | None:
        numbers = self._extract_whatsapp_numbers_from_hrefs(self._extract_whatsapp_links_from_dom_botasaurus(driver))
        if numbers:
            return numbers

        if not self._click_whatsapp_button_botasaurus(driver):
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            numbers = self._extract_whatsapp_numbers_from_hrefs(self._extract_whatsapp_links_from_dom_botasaurus(driver))
            if numbers:
                self._close_whatsapp_modal_botasaurus(driver)
                return numbers
        return None

    def _looks_like_contact_seller_number(self, value: str | None) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if any(ch.isalpha() for ch in text):
            return False
        digits = re.sub(r"\D+", "", text)
        if len(digits) < 7:
            return False
        return bool(re.fullmatch(r"[0-9][0-9\s+\-()]+", text))

    def _extract_contact_seller_from_buttons_botasaurus(self, driver) -> str | None:
        try:
            texts = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
                return Array.from(document.querySelectorAll('button, [role="button"]'))
                  .map((el) => norm(el.textContent || el.innerText))
                  .filter(Boolean);
                """
            )
        except Exception:
            texts = []

        for text in texts or []:
            if not isinstance(text, str):
                continue
            value = text.strip()
            if self._looks_like_contact_seller_number(value):
                return value
        return None

    def _click_view_number_button_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const controls = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const buttons = controls.filter((el) => {
                      const text = norm(el.textContent || el.innerText);
                      return text === 'view number' || text.includes('view number');
                    });
                    if (!buttons.length) return false;
                    for (const button of buttons) {
                      try { button.scrollIntoView({block: 'center'}); } catch (e) {}
                      try {
                        button.click();
                        return true;
                      } catch (e) {}
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _extract_contact_seller_botasaurus(self, driver, *, reveal_wait_seconds: float = 1.5) -> str | None:
        number = self._extract_contact_seller_from_buttons_botasaurus(driver)
        if number:
            return number

        if not self._click_view_number_button_botasaurus(driver):
            return None

        deadline = time.time() + max(0.0, reveal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            number = self._extract_contact_seller_from_buttons_botasaurus(driver)
            if number:
                return number
        return None

    def _extract_images_from_dom_botasaurus(self, driver) -> list[str] | None:
        try:
            urls = driver.run_js(
                """
                const isImage = (url) => /\\.(jpg|jpeg|png|webp)(\\?|$)/i.test(url || '');
                const isCdn = (url) => /cdn\\.rn?udah\\.com\\/(?:image_hd\\/)?(?:images\\/)?plain\\//i.test(url || '');
                const attrs = ['src', 'data-src', 'data-lazy', 'data-original'];
                const out = [];
                for (const img of Array.from(document.querySelectorAll('img'))) {
                  for (const attr of attrs) {
                    const val = img.getAttribute(attr);
                    if (val && isImage(val) && isCdn(val)) out.push(val);
                  }
                  const srcset = img.getAttribute('srcset');
                  if (srcset) {
                    for (const part of srcset.split(',')) {
                      const url = part.trim().split(' ')[0];
                      if (url && isImage(url) && isCdn(url)) out.push(url);
                    }
                  }
                }
                return out;
                """
            )
        except Exception:
            urls = []
        seen: set[str] = set()
        results: list[str] = []
        for url in urls or []:
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append(url)
        return results or None

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
                urls.append(href)

        deduped = list(dict.fromkeys(urls))
        return deduped

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1) -> list[str]:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")

        url = (start_url or self.listing_url).strip()
        if not url:
            raise ValueError("Missing start URL. Set MUDAHMY_CARS_LISTING_URL or pass --start-url.")

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="mudahmy")
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
                    bt.write_html(driver.page_html, "botasaurus_test/output/mudahmy_cars")
                    self.logger.warning(
                        "No listing links found; saved HTML to botasaurus_test/output/mudahmy_cars.html"
                    )
                except Exception as exc:
                    self.logger.warning("Failed to save debug HTML: %s", exc)
            return urls

        links = run({"url": url, "max_pages": max_pages}) or []
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
            "status",
        )

        status_update_sql = (
            "status = COALESCE(EXCLUDED.status, cars_scrap_mudahmy.status)"
            if allow_revive_sold
            else """status = CASE
            WHEN cars_scrap_mudahmy.status = 'sold' THEN cars_scrap_mudahmy.status
            ELSE COALESCE(EXCLUDED.status, cars_scrap_mudahmy.status)
          END"""
        )
        upsert_sql = f"""
        INSERT INTO cars_scrap_mudahmy ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          condition = COALESCE(EXCLUDED.condition, cars_scrap_mudahmy.condition),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_mudahmy.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_mudahmy.model),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_mudahmy.variant),
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_mudahmy.information_ads),
          information_ads_date = COALESCE(EXCLUDED.information_ads_date, cars_scrap_mudahmy.information_ads_date),
          location = COALESCE(EXCLUDED.location, cars_scrap_mudahmy.location),
          price = COALESCE(EXCLUDED.price, cars_scrap_mudahmy.price),
          year = COALESCE(EXCLUDED.year, cars_scrap_mudahmy.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_mudahmy.mileage),
          transmission = COALESCE(EXCLUDED.transmission, cars_scrap_mudahmy.transmission),
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_mudahmy.seat_capacity),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_mudahmy.engine_cc),
          fuel_type = COALESCE(EXCLUDED.fuel_type, cars_scrap_mudahmy.fuel_type),
          series = COALESCE(EXCLUDED.series, cars_scrap_mudahmy.series),
          type = COALESCE(EXCLUDED.type, cars_scrap_mudahmy.type),
          images = COALESCE(EXCLUDED.images, cars_scrap_mudahmy.images),
          whatsapp_number = COALESCE(EXCLUDED.whatsapp_number, cars_scrap_mudahmy.whatsapp_number),
          contact_seller = COALESCE(EXCLUDED.contact_seller, cars_scrap_mudahmy.contact_seller),
          {status_update_sql},
          version = COALESCE(cars_scrap_mudahmy.version, 0) + 1,
          last_scraped_at = NOW()
        """

        details_columns = (
            "listing_id",
            "listing_url",
            "ad_highlights",
            "description",
            "car_specifications",
        )
        details_upsert_sql = f"""
        INSERT INTO cars_scrap_mudahmy_details ({", ".join(details_columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          ad_highlights = COALESCE(EXCLUDED.ad_highlights, cars_scrap_mudahmy_details.ad_highlights),
          description = COALESCE(EXCLUDED.description, cars_scrap_mudahmy_details.description),
          car_specifications = COALESCE(EXCLUDED.car_specifications, cars_scrap_mudahmy_details.car_specifications)
        """

        sold_upsert_sql = """
        UPDATE cars_scrap_mudahmy
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

        cfg = load_botasaurus_config(service_name="mudahmy")
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

                raw_listing_id = None
                if listing_id_by_url:
                    raw_listing_id = listing_id_by_url.get(url)
                if not raw_listing_id:
                    try:
                        raw_listing_id = self._extract_listing_id(url)
                    except Exception:
                        raw_listing_id = None

                listing_id: int | None = None
                if raw_listing_id is not None:
                    try:
                        listing_id = int(str(raw_listing_id))
                    except Exception:
                        listing_id = None

                try:
                    self._navigate_botasaurus(driver, url, cfg)

                    current_url = str(getattr(driver, "current_url", "") or "")
                    tracking_mode = bool(listing_id_by_url)
                    redirected_off_detail = not self._is_listing_detail_url(current_url)

                    if self._is_access_blocked_botasaurus(driver):
                        raise RuntimeError(f"Blocked while scraping mudah.my detail: current_url={current_url!r}")

                    if self._is_listing_not_found_botasaurus(driver):
                        if listing_id:
                            result = {
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
                                "ad_highlights": None,
                                "description": None,
                                "car_specifications": None,
                                "images": None,
                                "whatsapp_number": None,
                                "contact_seller": None,
                                "error": None,
                                "status": "sold",
                            }
                            results.append(result)
                            if tracking_mode:
                                sold_rows.append((result.get("listing_url"), listing_id))
                                if len(sold_rows) >= batch_size:
                                    flush_sold_rows()
                            continue
                        raise RuntimeError("Listing not found (treating as error, not sold).")

                    if redirected_off_detail:
                        if listing_id:
                            result = {
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
                                "ad_highlights": None,
                                "description": None,
                                "car_specifications": None,
                                "images": None,
                                "whatsapp_number": None,
                                "contact_seller": None,
                                "error": None,
                                "status": "sold",
                            }
                            results.append(result)
                            if tracking_mode:
                                sold_rows.append((result.get("listing_url"), listing_id))
                                if len(sold_rows) >= batch_size:
                                    flush_sold_rows()
                            continue
                        raise RuntimeError(f"Unexpected redirect off detail page: current_url={current_url!r}")

                    wait_for_xpath(
                        driver,
                        TOP_CONTAINER_XPATH,
                        timeout_seconds=cfg.detail_wait_seconds,
                        poll_interval_seconds=cfg.poll_interval_seconds,
                    )
                    wait_for_css(
                        driver,
                        "css=h1[itemprop='name']",
                        timeout_seconds=cfg.detail_wait_seconds,
                        poll_interval_seconds=cfg.poll_interval_seconds,
                    )

                    condition_value, info_value = self._extract_condition_and_information_ads(driver)
                    specs = self._extract_car_specs_map_botasaurus(driver)

                    location_value = self._get_text_by_xpath_botasaurus(driver, LOCATION_XPATH)
                    price_value = self._parse_price(self._get_text_by_xpath_botasaurus(driver, PRICE_XPATH))
                    year_value = self._parse_int_from_text(
                        self._get_text_by_xpath_botasaurus(driver, YEAR_XPATH_PRIMARY)
                        or self._get_text_by_xpath_botasaurus(driver, YEAR_XPATH_FALLBACK)
                        or specs.get("year")
                    )
                    mileage_value = self.convert_mileage(self._get_text_by_xpath_botasaurus(driver, MILEAGE_XPATH))
                    transmission_value = self._get_text_by_xpath_botasaurus(driver, TRANSMISSION_XPATH)
                    seat_capacity_value = specs.get("seat_capacity")
                    engine_cc_value = self._normalize_engine_cc(specs.get("engine_cc"))
                    fuel_type_value = specs.get("fuel_type")
                    series_value = specs.get("series")
                    type_value = specs.get("type")

                    def has_value(v) -> bool:
                        if v is None:
                            return False
                        if isinstance(v, str):
                            return bool(v.strip())
                        if isinstance(v, (list, tuple, set, dict)):
                            return bool(v)
                        return True

                    has_any_data = any(
                        has_value(v)
                        for v in (
                            condition_value,
                            specs.get("brand"),
                            specs.get("model"),
                            specs.get("variant"),
                            info_value,
                            location_value,
                            price_value,
                            year_value,
                            mileage_value,
                            transmission_value,
                            seat_capacity_value,
                            engine_cc_value,
                            fuel_type_value,
                            series_value,
                            type_value,
                        )
                    )
                    if not has_any_data:
                        if listing_id and self._is_listing_not_found_botasaurus(driver):
                            status = "sold"
                        else:
                            raise RuntimeError("No extractable data on detail page (treating as error, not sold).")
                    else:
                        status = "active"

                    ad_highlights_text = None
                    description_text = None
                    car_specifications_value: dict[str, object] | None = None
                    if status == "active":
                        wait_for_css(
                            driver,
                            f"css={AD_HIGHLIGHTS_SECTION_SELECTOR}",
                            timeout_seconds=2,
                            poll_interval_seconds=cfg.poll_interval_seconds,
                        )
                        wait_for_css(
                            driver,
                            f"css={DESCRIPTION_SECTION_SELECTOR}",
                            timeout_seconds=2,
                            poll_interval_seconds=cfg.poll_interval_seconds,
                        )
                        wait_for_css(
                            driver,
                            f"css={CAR_SPECIFICATIONS_SECTION_SELECTOR}",
                            timeout_seconds=2,
                            poll_interval_seconds=cfg.poll_interval_seconds,
                        )

                        ad_highlights_text = self._extract_ad_highlights_text_botasaurus(driver)

                        if self._click_show_more_description_botasaurus(driver):
                            time.sleep(0.25)
                        description_text = self._extract_description_text_botasaurus(driver)

                        if self._click_show_more_car_specs_botasaurus(driver):
                            time.sleep(0.25)
                        car_specifications_raw = self._extract_car_specifications_dict_botasaurus(driver)
                        car_specifications_value = car_specifications_raw or None

                    result = {
                        "listing_id": listing_id,
                        "listing_url": url,
                        "condition": condition_value if status == "active" else None,
                        "brand": specs.get("brand") if status == "active" else None,
                        "model": specs.get("model") if status == "active" else None,
                        "variant": specs.get("variant") if status == "active" else None,
                        "information_ads": info_value if status == "active" else None,
                        "information_ads_date": self._convert_information_ads_to_date(info_value)
                        if status == "active"
                        else None,
                        "location": location_value if status == "active" else None,
                        "price": price_value if status == "active" else None,
                        "year": year_value if status == "active" else None,
                        "mileage": mileage_value if status == "active" else None,
                        "transmission": transmission_value if status == "active" else None,
                        "seat_capacity": seat_capacity_value if status == "active" else None,
                        "engine_cc": engine_cc_value if status == "active" else None,
                        "fuel_type": fuel_type_value if status == "active" else None,
                        "series": series_value if status == "active" else None,
                        "type": type_value if status == "active" else None,
                        "ad_highlights": ad_highlights_text if status == "active" else None,
                        "description": description_text if status == "active" else None,
                        "car_specifications": car_specifications_value if status == "active" else None,
                        "images": None,
                        "whatsapp_number": None,
                        "contact_seller": None,
                        "error": None,
                        "status": status,
                    }

                    if status != "active":
                        results.append(result)
                        if tracking_mode and listing_id:
                            sold_rows.append((result.get("listing_url"), listing_id))
                            if len(sold_rows) >= batch_size:
                                flush_sold_rows()
                        continue

                    if not result.get("engine_cc"):
                        if self._click_show_more_car_specs_botasaurus(driver):
                            time.sleep(0.25)
                            specs2 = self._extract_car_specs_map_botasaurus(driver)
                            result["engine_cc"] = self._normalize_engine_cc(
                                specs2.get("engine_cc")
                                or self._extract_car_spec_value_by_label_botasaurus(
                                    driver, label_candidates=["engine cc", "engine", "engine capacity", "cc"]
                                )
                                or self._get_text_by_xpath_botasaurus(driver, ENGINE_CC_XPATH)
                            )

                    result["whatsapp_number"] = self._extract_whatsapp_numbers_botasaurus(driver)
                    result["contact_seller"] = self._extract_contact_seller_botasaurus(driver)
                    wait_for_css(
                        driver,
                        "css=img[src*='cdn.rnudah.com']",
                        timeout_seconds=2,
                        poll_interval_seconds=cfg.poll_interval_seconds,
                    )
                    self._open_images_gallery_botasaurus(driver)
                    time.sleep(0.75)
                    images = self._extract_gallery_images_botasaurus(driver) or self._extract_images_from_dom_botasaurus(
                        driver
                    )
                    result["images"] = images
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
                        "ad_highlights": None,
                        "description": None,
                        "car_specifications": None,
                        "images": None,
                        "whatsapp_number": None,
                        "contact_seller": None,
                        "status": "active",
                        "error": "error",
                    }

                results.append(result)

                row_listing_id = result.get("listing_id")
                if row_listing_id and not result.get("error"):
                    status = str(result.get("status") or "active")
                    if require_brand_model and status == "active":
                        if not has_text(result.get("brand")) or not has_text(result.get("model")):
                            result["skipped_upsert"] = True
                            result["skip_reason"] = "missing_brand_or_model"
                            continue
                    rows.append(
                        (
                            int(row_listing_id),
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
                            status,
                        )
                    )

                    if status == "active":
                        car_specs_obj = result.get("car_specifications")
                        details_rows.append(
                            (
                                int(row_listing_id),
                                result.get("listing_url"),
                                result.get("ad_highlights"),
                                result.get("description"),
                                Json(car_specs_obj) if car_specs_obj else None,
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

    def _open_images_gallery_botasaurus(self, driver) -> None:
        # Try the provided XPath first, then fallback to robust selectors.
        self._click_by_xpath_botasaurus(driver, IMAGE_GRID_CONTAINER_XPATH)
        try:
            driver.run_js(
                """
                const click = (el) => {
                  if (!el) return false;
                  try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                  try { el.click(); return true; } catch (e) { return false; }
                };
                // Prefer the first big tile.
                if (click(document.querySelector("div[data-action-step='1']"))) return true;
                // Fallback to any tile.
                if (click(document.querySelector("div[data-action-step]"))) return true;
                // Fallback to first image.
                if (click(document.querySelector("img[src*='cdn.rnudah.com']"))) return true;
                return false;
                """
            )
        except Exception:
            pass

    def _extract_gallery_images_botasaurus(self, driver) -> list[str] | None:
        """
        After opening the gallery, try to return a dense cluster of cdn.rnudah.com images.
        """
        try:
            urls = driver.run_js(
                """
                const isImage = (url) => /\\.(jpg|jpeg|png|webp)(\\?|$)/i.test(url || '');
                const isCdn = (url) =>
                  /cdn\\.rn?udah\\.com\\/(?:image_hd\\/)?(?:images\\/)?plain\\//i.test(url || '');
                const getUrls = (img) => {
                  const urls = [];
                  const attrs = ['src', 'data-src', 'data-lazy', 'data-original'];
                  for (const attr of attrs) {
                    const val = img.getAttribute(attr);
                    if (val && isImage(val) && isCdn(val)) urls.push(val);
                  }
                  const srcset = img.getAttribute('srcset');
                  if (srcset) {
                    const first = srcset.split(',')[0].trim().split(' ')[0];
                    if (first && isImage(first) && isCdn(first)) urls.push(first);
                  }
                  return urls;
                };
                const isVisible = (el) => {
                  const style = window.getComputedStyle(el);
                  return (
                    style &&
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    el.getClientRects().length > 0
                  );
                };
                const candidates = Array.from(document.querySelectorAll('div,section,ul'))
                  .filter(isVisible)
                  .map(el => {
                    const imgs = Array.from(el.querySelectorAll('img'));
                    const urls = imgs.flatMap(getUrls);
                    return { el, urls };
                  })
                  .filter(item => item.urls.length >= 2);
                if (!candidates.length) return [];
                candidates.sort((a, b) => b.urls.length - a.urls.length);
                return candidates[0].urls;
                """
            )
        except Exception:
            urls = []
        seen: set[str] = set()
        results: list[str] = []
        for url in urls or []:
            if not url:
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append(url)
        return results or None
