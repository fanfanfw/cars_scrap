from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from cars_scrap_common.botasaurus_utils import (
    load_botasaurus_config,
    raise_if_cloudflare_challenge,
    wait_for_css,
    wait_for_xpath,
)
from cars_scrap_common.logging_utils import setup_logger
from scrap_mudahmy_cars_botasaurus.database import get_connection

load_dotenv(override=True)

LISTING_DETAIL_RE = re.compile(r"-\d+\.htm(?:$|[?#])", re.IGNORECASE)

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


class MudahMyCarsService:
    def __init__(self):
        self.logger = setup_logger("scrape_mudahmy_cars")
        self.stop_flag = False
        self.listing_url = os.getenv("MUDAHMY_CARS_LISTING_URL", "").strip()

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

    def scrape_listing_details(self, links: list[str]) -> list[dict]:
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
        INSERT INTO cars_scrap_mudahmy ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          condition = COALESCE(EXCLUDED.condition, cars_scrap_mudahmy.condition),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_mudahmy.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_mudahmy.model),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_mudahmy.variant),
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_mudahmy.information_ads),
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
          status = COALESCE(EXCLUDED.status, cars_scrap_mudahmy.status),
          version = cars_scrap_mudahmy.version + 1,
          last_scraped_at = NOW()
        """

        conn = get_connection()
        cursor = conn.cursor()
        rows: list[tuple] = []

        def flush_rows() -> None:
            if not rows:
                return
            try:
                execute_values(cursor, upsert_sql, rows, page_size=100)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            rows.clear()

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="mudahmy")

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
                try:
                    listing_id = int(self._extract_listing_id(url))
                except Exception as exc:
                    self.logger.warning("Skipping URL (no listing_id): %s (%s)", url, exc)
                    continue

                try:
                    self._navigate_botasaurus(driver, url, cfg)
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
                    result = {
                        "listing_id": listing_id,
                        "listing_url": url,
                        "condition": condition_value,
                        "brand": specs.get("brand"),
                        "model": specs.get("model"),
                        "variant": specs.get("variant"),
                        "information_ads": info_value,
                        "location": self._get_text_by_xpath_botasaurus(driver, LOCATION_XPATH),
                        "price": self._parse_price(self._get_text_by_xpath_botasaurus(driver, PRICE_XPATH)),
                        "year": self._parse_int_from_text(
                            self._get_text_by_xpath_botasaurus(driver, YEAR_XPATH_PRIMARY)
                            or self._get_text_by_xpath_botasaurus(driver, YEAR_XPATH_FALLBACK)
                            or specs.get("year")
                        ),
                        "mileage": self.convert_mileage(self._get_text_by_xpath_botasaurus(driver, MILEAGE_XPATH)),
                        "transmission": self._get_text_by_xpath_botasaurus(driver, TRANSMISSION_XPATH),
                        "seat_capacity": specs.get("seat_capacity"),
                        "engine_cc": specs.get("engine_cc"),
                        "fuel_type": specs.get("fuel_type"),
                        "series": specs.get("series"),
                        "type": specs.get("type"),
                        "images": None,
                        "status": "active",
                        "error": None,
                    }

                    # Some fields (e.g. engine cc) may require expanding the section.
                    if not result.get("engine_cc"):
                        if self._click_show_more_car_specs_botasaurus(driver):
                            time.sleep(0.25)
                            # Try re-parsing first; fallback to provided XPath if needed.
                            specs2 = self._extract_car_specs_map_botasaurus(driver)
                            result["engine_cc"] = (
                                specs2.get("engine_cc")
                                or self._extract_car_spec_value_by_label_botasaurus(
                                    driver, label_candidates=["engine cc", "engine", "engine capacity", "cc"]
                                )
                                or self._get_text_by_xpath_botasaurus(driver, ENGINE_CC_XPATH)
                            )

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
                        "listing_id": listing_id,
                        "listing_url": url,
                        "condition": None,
                        "brand": None,
                        "model": None,
                        "variant": None,
                        "information_ads": None,
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
                        "images": None,
                        "status": "active",
                        "error": "error",
                    }

                results.append(result)
                rows.append(
                    (
                        result.get("listing_id"),
                        result.get("listing_url"),
                        result.get("condition"),
                        result.get("brand"),
                        result.get("model"),
                        result.get("variant"),
                        result.get("information_ads"),
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
                        result.get("status") or "active",
                    )
                )

                if len(rows) >= batch_size:
                    flush_rows()

            flush_rows()
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
