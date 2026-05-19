from __future__ import annotations

import os
import re
import time
from datetime import date
from hashlib import sha1
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

from cars_scrap_common.botasaurus_utils import load_botasaurus_config, raise_if_cloudflare_challenge, wait_for_css
from cars_scrap_common.env_utils import env_bool
from cars_scrap_common.logging_utils import setup_logger
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip
from cars_scrap_common.shared_base_service import SharedBaseService
from scrap_olx_cars_botasaurus.database import get_connection

load_dotenv(override=True)

DEFAULT_LISTING_URL = "https://www.olx.co.id/mobil-bekas_c198"
ITEM_TITLE_SELECTOR = '[data-aut-id="itemTitle"]'
ITEM_PRICE_SELECTOR = '[data-aut-id="itemPrice"]'
ITEM_MILEAGE_SELECTOR = '[data-aut-id="itemAttribute_mileage"]'
VARIANT_SELECTOR = "div.BxCeR"
SELLER_NAME_SELECTOR = "div.HtnZ6"
DETAIL_ROOT_SELECTOR = "#main_content"


class OlxCarsService(SharedBaseService):
    def __init__(self):
        self.logger = setup_logger("scrape_olx_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
        self.listing_url = os.getenv("OLX_CARS_LISTING_URL", DEFAULT_LISTING_URL).strip()

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
            service_label="olx",
            prompt_on_cloudflare=cfg.prompt_on_cloudflare,
        )

    def _extract_listing_id(self, url: str) -> str:
        match = re.search(r"iid-(\d+)", url, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"(?:-|/)(\d{6,})(?:[/?#.]|$)", url)
        if match:
            return match.group(1)
        return sha1(url.encode("utf-8")).hexdigest()[:16]

    def _is_on_target_domain(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        return host == "olx.co.id" or host.endswith(".olx.co.id")

    def _is_listing_detail_url(self, url: str) -> bool:
        if not self._is_on_target_domain(url):
            return False
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        path = parsed.path or ""
        return "/item/" in path and bool(re.search(r"iid-\d+", path, re.IGNORECASE))

    def _normalize_title_token(self, value: str | None) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text or None

    def _parse_title(self, title: str | None) -> tuple[str | None, str | None, int | None]:
        text = self._normalize_title_token(title)
        if not text:
            return None, None, None

        year = None
        match = re.search(r"\((\d{4})\)\s*$", text)
        if match:
            year = int(match.group(1))
            text = text[: match.start()].strip()

        parts = text.split()
        if not parts:
            return None, None, year

        brand = parts[0].strip() or None
        model = " ".join(parts[1:]).strip() or None
        return brand, model, year

    def _parse_variant_specs(self, text: str | None) -> tuple[str | None, str | None, str | None]:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return None, None, None

        match = re.search(r"\b(Bensin|Diesel|Hybrid|Listrik|Electric)\s*-\s*([A-Za-z0-9]+)\b", value, re.IGNORECASE)
        if not match:
            return value, None, None

        variant = value[: match.start()].strip(" -") or None
        fuel_type = match.group(1).strip()
        transmission = match.group(2).strip().upper()
        return variant, fuel_type, transmission

    def _parse_price(self, text: str | None) -> int | None:
        return self._parse_int_from_text(text)

    def _parse_olx_engine_cc(self, text: str | None) -> str | None:
        value = str(text or "").strip()
        if not value:
            return None
        numbers = re.findall(r"\d+(?:[.,]\d+)*", value)
        if not numbers:
            return None
        last = numbers[-1].replace(".", "").replace(",", "")
        return last or None

    def _text_by_css_botasaurus(self, driver, selector: str) -> str | None:
        try:
            text = driver.run_js(
                r"""
                const el = document.querySelector(args.selector);
                if (!el) return null;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                return text || null;
                """,
                {"selector": selector},
            )
        except Exception:
            text = None
        return text or None

    def _extract_label_value_botasaurus(self, driver, labels: tuple[str, ...]) -> str | None:
        try:
            return driver.run_js(
                r"""
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const wanted = args.labels.map((s) => String(s || '').toLowerCase());
                const cards = Array.from(document.querySelectorAll('#main_content ._3dS7E'));
                for (const card of cards) {
                  const labelEl = card.querySelector('._CCSn');
                  const valueEl = card.querySelector('._3VRXh');
                  const label = norm(labelEl && (labelEl.innerText || labelEl.textContent)).toLowerCase();
                  const value = norm(valueEl && (valueEl.innerText || valueEl.textContent));
                  if (label && value && wanted.some((wantedLabel) => label === wantedLabel)) {
                    return value;
                  }
                }
                return null;
                """,
                {"labels": list(labels)},
            )
        except Exception:
            return None

    def _extract_location_botasaurus(self, driver) -> str | None:
        value = self._extract_label_value_botasaurus(driver, ("lokasi", "location"))
        if value:
            return value
        return None

    def _extract_engine_cc_botasaurus(self, driver) -> str | None:
        value = self._extract_label_value_botasaurus(driver, ("kapasitas mesin", "engine capacity", "engine"))
        if value:
            return self._parse_olx_engine_cc(value)
        return None

    def _click_next_gallery_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    r"""
                    const direct = document.querySelector('[data-aut-id="rightArrow"]');
                    if (direct) {
                      try { direct.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                      try { direct.click(); return true; } catch (e) {}
                    }
                    const root = document.querySelector('[data-aut-id="imageGallery"]') || document;
                    const buttons = Array.from(root.querySelectorAll('button, [role="button"], span, div, svg'));
                    const visible = (el) => {
                      const rect = el.getBoundingClientRect();
                      return rect.width > 20 && rect.height > 20;
                    };
                    const candidates = buttons.filter((el) => {
                      const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
                      const cls = String(el.className || '').toLowerCase();
                      const text = String(el.innerText || el.textContent || '').trim().toLowerCase();
                      return visible(el) && (
                        aria.includes('next') || aria.includes('selanjut') || cls.includes('next') || text === '›' || text === '>'
                      );
                    });
                    const picked = candidates[candidates.length - 1];
                    if (!picked) return false;
                    try { picked.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                    try { picked.click(); return true; } catch (e) { return false; }
                    """
                )
            )
        except Exception:
            return False

    def _open_gallery_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    r"""
                    const root = document.querySelector('[data-aut-id="imageGallery"]') || document;
                    const current = root.querySelector('.slick-slide.slick-active.slick-current img, img[data-aut-id="defaultImg"], img');
                    const target = current && (current.closest('.slick-slide') || current.closest('figure') || current);
                    if (!target) return false;
                    try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                    try { target.click(); return true; } catch (e) { return false; }
                    """
                )
            )
        except Exception:
            return False

    def _gallery_image_total_botasaurus(self, driver) -> int | None:
        try:
            value = driver.run_js(
                r"""
                const haystack = [
                  document.body && document.body.innerText,
                  ...Array.from(document.querySelectorAll('[data-aut-id="imageGallery"], .slick-slider, [role="dialog"]'))
                    .map((el) => el.innerText || el.textContent || '')
                ].join('\n');
                const matches = Array.from(haystack.matchAll(/\b(\d+)\s*\/\s*(\d+)\b/g));
                if (!matches.length) return null;
                const totals = matches
                  .map((m) => ({ current: Number(m[1]), total: Number(m[2]) }))
                  .filter((m) => m.total > 0 && m.total < 200 && m.current <= m.total);
                if (!totals.length) return null;
                totals.sort((a, b) => b.total - a.total);
                return totals[0].total;
                """
            )
        except Exception:
            value = None
        try:
            parsed = int(value) if value is not None else None
        except Exception:
            parsed = None
        return parsed if parsed and parsed > 0 else None

    def _extract_images_once_botasaurus(self, driver) -> list[str]:
        try:
            raw = driver.run_js(
                r"""
                const urls = [];
                const add = (value) => {
                  if (!value) return;
                  const text = String(value);
                  for (const part of text.split(',')) {
                    const token = part.trim().split(/\s+/)[0];
                    if (/^https?:\/\//i.test(token)) urls.push(token);
                  }
                };
                const root = document.querySelector('[data-aut-id="imageGallery"]') || document;
                for (const img of Array.from(root.querySelectorAll('img'))) {
                  add(img.currentSrc || img.src || img.getAttribute('src'));
                  add(img.getAttribute('srcset'));
                  add(img.getAttribute('data-src'));
                }
                for (const el of Array.from(root.querySelectorAll('[style]'))) {
                  const style = String(el.getAttribute('style') || '');
                  const matches = style.match(/url\(["']?([^"')]+)["']?\)/ig) || [];
                  for (const match of matches) {
                    const cleaned = match.replace(/^url\(["']?/i, '').replace(/["']?\)$/i, '');
                    add(cleaned);
                  }
                }
                return Array.from(new Set(urls));
                """
            )
        except Exception:
            raw = []
        best_by_file_id: dict[str, str] = {}
        fallback: list[str] = []
        for item in raw or []:
            url = str(item or "").strip()
            if not url or "olx" not in url.lower():
                continue
            if url.startswith("//"):
                url = "https:" + url
            url = self._normalize_olx_image_url(url)
            match = re.search(r"/files/([^/]+)/image", url)
            if not match:
                fallback.append(url)
                continue
            file_id = match.group(1)
            current = best_by_file_id.get(file_id)
            if current is None or self._image_quality_score(url) > self._image_quality_score(current):
                best_by_file_id[file_id] = url
        return list(best_by_file_id.values()) or list(dict.fromkeys(fallback))

    def _normalize_olx_image_url(self, url: str) -> str:
        match = re.match(r"^(https?://apollo\.olx\.co\.id/v1/files/[^/]+/image)(?:[;?][^#]*)?", url)
        if not match:
            return url
        return match.group(1)

    def _image_quality_score(self, url: str) -> int:
        score = 0
        match = re.search(r"[;?]s=(\d+)x", url)
        if match:
            try:
                score += int(match.group(1))
            except Exception:
                pass
        if ";f=avif" in url:
            score += 10000
        return score

    def _extract_images_botasaurus(self, driver) -> list[str] | None:
        seen: dict[str, None] = {}
        self._open_gallery_botasaurus(driver)
        time.sleep(self._env_float("OLX_IMAGE_GALLERY_OPEN_WAIT_SECONDS", 0.5, minimum=0.0))
        image_total = self._gallery_image_total_botasaurus(driver)
        max_clicks = self._env_int("OLX_IMAGE_NEXT_CLICKS", 40, minimum=0)
        if image_total:
            max_clicks = min(max_clicks, image_total + 2)
        wait_seconds = self._env_float("OLX_IMAGE_NEXT_WAIT_SECONDS", 0.25, minimum=0.0)
        for idx in range(max_clicks + 1):
            for image_url in self._extract_images_once_botasaurus(driver):
                seen.setdefault(image_url, None)
            if image_total and len(seen) >= image_total:
                break
            if idx >= max_clicks:
                break
            if not self._click_next_gallery_botasaurus(driver):
                break
            if wait_seconds:
                time.sleep(wait_seconds)
        return list(seen.keys()) or None

    def _extract_description_botasaurus(self, driver) -> str | None:
        self._click_show_more_description_botasaurus(driver)
        deadline = time.time() + self._env_float("OLX_DESCRIPTION_MODAL_WAIT_SECONDS", 4.0, minimum=0.1)
        while time.time() < deadline:
            description = self._read_modal_description_botasaurus(driver)
            if description:
                self._close_description_modal_botasaurus(driver)
                return description
            time.sleep(0.2)
        return None

    def _click_show_more_description_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    r"""
                    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, span'));
                    for (const node of nodes) {
                      const text = norm(node.innerText || node.textContent);
                      if (text !== 'selengkapnya') continue;
                      const target = node.closest('button, [role="button"], a') || node;
                      try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                      try { target.click(); return true; } catch (e) {}
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _read_modal_description_botasaurus(self, driver) -> str | None:
        try:
            text = driver.run_js(
                r"""
                const root = document.querySelector('[data-aut-id="modalDescriptionContent"]');
                if (!root) return null;
                const parts = Array.from(root.querySelectorAll('p'))
                  .map((el) => (el.innerText || el.textContent || '').trim())
                  .filter(Boolean);
                const text = (parts.length ? parts.join('\n') : (root.innerText || root.textContent || '').trim()).trim();
                return text || null;
                """
            )
        except Exception:
            text = None
        return text or None

    def _close_description_modal_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    r"""
                    const root = document.querySelector('[data-aut-id="modalDescriptionContent"]');
                    const modal = root && (root.closest('[role="dialog"]') || root.parentElement);
                    const scope = modal || document;
                    const candidates = Array.from(scope.querySelectorAll('button, [role="button"], span, div'));
                    for (const el of candidates) {
                      const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
                      const text = String(el.innerText || el.textContent || '').trim().toLowerCase();
                      const rect = el.getBoundingClientRect();
                      if (rect.width < 12 || rect.height < 12) continue;
                      if (aria.includes('close') || aria.includes('tutup') || text === 'x' || text === '×') {
                        try { el.click(); return true; } catch (e) {}
                      }
                    }
                    return false;
                    """
                )
            )
        except Exception:
            return False

    def _is_listing_not_found_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "iklan tidak ditemukan",
                "iklan sudah tidak tersedia",
                "ad is no longer available",
                "page not found",
                "404",
            ),
        )

    def _extract_listing_urls_botasaurus(self, driver) -> list[str]:
        try:
            hrefs = driver.run_js(
                """
                return Array.from(document.querySelectorAll('a[href]'))
                  .map((a) => a.href || a.getAttribute('href'))
                  .filter(Boolean);
                """
            )
        except Exception:
            hrefs = []
        urls: list[str] = []
        for href in hrefs or []:
            url = urljoin("https://www.olx.co.id", str(href or "").strip())
            if self._is_listing_detail_url(url):
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _scrape_single_detail_botasaurus(self, driver, *, url: str, cfg, listing_id: str | None = None) -> dict:
        self._navigate_botasaurus(driver, url, cfg)
        wait_for_css(
            driver,
            DETAIL_ROOT_SELECTOR,
            timeout_seconds=cfg.detail_wait_seconds,
            poll_interval_seconds=cfg.poll_interval_seconds,
        )
        wait_for_css(
            driver,
            ITEM_TITLE_SELECTOR,
            timeout_seconds=cfg.detail_wait_seconds,
            poll_interval_seconds=cfg.poll_interval_seconds,
        )

        listing_id_final = (listing_id or self._extract_listing_id(url) or "").strip()
        if self._is_listing_not_found_botasaurus(driver):
            return self._build_sold_result(url=url, listing_id=listing_id_final)

        title_value = self._text_by_css_botasaurus(driver, ITEM_TITLE_SELECTOR)
        brand_value, model_value, year_value = self._parse_title(title_value)

        variant_raw = self._text_by_css_botasaurus(driver, VARIANT_SELECTOR)
        variant_value, fuel_type_value, transmission_value = self._parse_variant_specs(variant_raw)

        price_value = self._parse_price(self._text_by_css_botasaurus(driver, ITEM_PRICE_SELECTOR))
        location_value = self._extract_location_botasaurus(driver)
        mileage_value = self._parse_mileage(self._text_by_css_botasaurus(driver, ITEM_MILEAGE_SELECTOR))
        engine_cc_value = self._extract_engine_cc_botasaurus(driver)
        images_value = self._extract_images_botasaurus(driver)
        description_value = self._extract_description_botasaurus(driver)
        seller_name_value = self._text_by_css_botasaurus(driver, SELLER_NAME_SELECTOR)

        if not listing_id_final:
            raise RuntimeError("Missing OLX listing_id")
        if not any([brand_value, model_value, price_value, year_value, location_value, mileage_value, engine_cc_value]):
            raise RuntimeError("No extractable OLX detail data")

        return {
            "listing_id": listing_id_final,
            "listing_url": url,
            "condition": None,
            "brand": brand_value,
            "model": model_value,
            "variant": variant_value,
            "information_ads": None,
            "information_ads_date": None,
            "location": location_value,
            "price": price_value,
            "year": year_value,
            "mileage": mileage_value,
            "transmission": transmission_value,
            "seat_capacity": None,
            "engine_cc": engine_cc_value,
            "fuel_type": fuel_type_value,
            "series": None,
            "type": None,
            "images": images_value,
            "whatsapp_number": None,
            "contact_seller": None,
            "seller_name": seller_name_value,
            "raw_detail": {
                "title": title_value,
                "variant_specs": variant_raw,
                "description": description_value,
            },
            "error": None,
            "status": "active",
        }

    def _build_sold_result(self, *, url: str, listing_id: str | None) -> dict:
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
            "images": None,
            "whatsapp_number": None,
            "contact_seller": None,
            "seller_name": None,
            "raw_detail": None,
            "error": None,
            "status": "sold",
        }

    def _build_error_result(self, url: str, *, listing_id: str | None = None, error_message: str | None = None) -> dict:
        result = self._build_sold_result(url=url, listing_id=listing_id or self._extract_listing_id(url))
        result["status"] = "active"
        result["error"] = error_message or "error"
        return result

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1) -> list[str]:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        url = (start_url or self.listing_url).strip()
        if not url:
            raise ValueError("Missing start URL. Set OLX_CARS_LISTING_URL or pass --start-url.")

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="olx")
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
            return self._extract_listing_urls_botasaurus(driver)

        links = run({"url": url, "max_pages": max_pages}) or []
        self.logger.info("Collected %s OLX links.", len(links))
        return links

    def scrape_listing_details(
        self,
        links: list[str],
        *,
        listing_id_by_url: dict[str, str] | None = None,
        allow_revive_sold: bool = False,
    ) -> list[dict]:
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
            "whatsapp_number",
            "contact_seller",
            "seller_name",
            "status",
        )

        status_update_sql = (
            "status = COALESCE(EXCLUDED.status, cars_scrap_olx.status)"
            if allow_revive_sold
            else """status = CASE
            WHEN cars_scrap_olx.status = 'sold' THEN cars_scrap_olx.status
            ELSE COALESCE(EXCLUDED.status, cars_scrap_olx.status)
          END"""
        )
        upsert_sql = f"""
        INSERT INTO cars_scrap_olx ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          condition = COALESCE(EXCLUDED.condition, cars_scrap_olx.condition),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_olx.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_olx.model),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_olx.variant),
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_olx.information_ads),
          information_ads_date = COALESCE(EXCLUDED.information_ads_date, cars_scrap_olx.information_ads_date),
          location = COALESCE(EXCLUDED.location, cars_scrap_olx.location),
          price = COALESCE(EXCLUDED.price, cars_scrap_olx.price),
          year = COALESCE(EXCLUDED.year, cars_scrap_olx.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_olx.mileage),
          transmission = COALESCE(EXCLUDED.transmission, cars_scrap_olx.transmission),
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_olx.seat_capacity),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_olx.engine_cc),
          fuel_type = COALESCE(EXCLUDED.fuel_type, cars_scrap_olx.fuel_type),
          series = COALESCE(EXCLUDED.series, cars_scrap_olx.series),
          type = COALESCE(EXCLUDED.type, cars_scrap_olx.type),
          images = COALESCE(EXCLUDED.images, cars_scrap_olx.images),
          whatsapp_number = COALESCE(EXCLUDED.whatsapp_number, cars_scrap_olx.whatsapp_number),
          contact_seller = COALESCE(EXCLUDED.contact_seller, cars_scrap_olx.contact_seller),
          seller_name = COALESCE(EXCLUDED.seller_name, cars_scrap_olx.seller_name),
          {status_update_sql},
          version = COALESCE(cars_scrap_olx.version, 0) + 1,
          last_scraped_at = NOW()
        """

        details_upsert_sql = """
        INSERT INTO cars_scrap_olx_details (listing_id, listing_url, raw_detail)
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          raw_detail = COALESCE(EXCLUDED.raw_detail, cars_scrap_olx_details.raw_detail)
        """

        conn = get_connection()
        cursor = conn.cursor()
        rows: list[tuple] = []
        details_rows: list[tuple] = []

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

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="olx")
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
                self.logger.info("OLX detail %s/%s: %s", idx, len(data["links"]), url)

                listing_id = listing_id_by_url.get(url) if listing_id_by_url else None
                try:
                    result = self._scrape_single_detail_botasaurus(driver, url=url, cfg=cfg, listing_id=listing_id)
                except Exception as exc:
                    self.logger.warning("Error while scraping OLX %s: %s", url, exc)
                    result = self._build_error_result(url, listing_id=listing_id, error_message=str(exc))

                results.append(result)
                row_listing_id = result.get("listing_id")
                if row_listing_id and not result.get("error"):
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
                            result.get("status") or "active",
                        )
                    )
                    if result.get("raw_detail"):
                        details_rows.append(
                            (
                                str(row_listing_id),
                                result.get("listing_url"),
                                Json(result.get("raw_detail")),
                            )
                        )
                    if len(rows) >= batch_size or len(details_rows) >= batch_size:
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
