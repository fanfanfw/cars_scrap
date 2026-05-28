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
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip, reset_cached_proxy
from cars_scrap_common.shared_base_service import SharedBaseService
from scrap_olx_cars_botasaurus.database import get_connection

load_dotenv(override=True)

DEFAULT_LISTING_URL = "https://www.olx.co.id/indonesia_g1000001/mobil-bekas_c198"
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

    def _is_browser_error_page_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "this site can't be reached",
                "this site cant be reached",
                "err_tunnel_connection_failed",
                "tunnel_connection_failed",
                "err_proxy_connection_failed",
                "proxy_connection_failed",
                "err_timed_out",
                "took too long to respond",
                "http error 400",
                "400 bad request",
                "request header or cookie too large",
            ),
        )

    def _load_listing_cards_botasaurus(self, driver, *, url: str, cfg) -> list[dict]:
        self.logger.info("Navigating (botasaurus): %s", url)
        self._navigate_botasaurus(driver, url, cfg)
        time.sleep(max(0.0, cfg.listing_wait_seconds))
        if self._is_browser_error_page_botasaurus(driver):
            raise RuntimeError(f"Retryable OLX listing browser error page: current_url={getattr(driver, 'current_url', None)!r}")
        cards = self._extract_listing_cards_botasaurus(driver)
        if not cards:
            raise RuntimeError(f"Retryable OLX listing empty result: current_url={getattr(driver, 'current_url', None)!r}")
        return cards

    def _retry_listing_with_new_session(self, *, url: str, max_pages: int, base_profile: str) -> list[dict] | None:
        max_retries = self._env_int("OLX_LISTING_PROXY_RETRIES", 3, minimum=0)
        if max_retries < 1:
            return None

        from botasaurus.browser import Driver, browser

        previous_profile = os.getenv("BOTASAURUS_PROFILE")
        try:
            for retry_index in range(1, max_retries + 1):
                retry_profile = f"{base_profile}_listing_r{retry_index}_{str(time.time_ns())[-8:]}"
                try:
                    os.environ["BOTASAURUS_PROFILE"] = retry_profile
                    reset_cached_proxy()
                    cfg = load_botasaurus_config(service_name="olx")
                finally:
                    if previous_profile is None:
                        os.environ.pop("BOTASAURUS_PROFILE", None)
                    else:
                        os.environ["BOTASAURUS_PROFILE"] = previous_profile
                    reset_cached_proxy()

                self.logger.warning(
                    "Retrying OLX listing with new session/proxy. retry=%s/%s profile=%s proxy=%s url=%s",
                    retry_index,
                    max_retries,
                    cfg.browser_kwargs.get("profile"),
                    mask_proxy_url(str(cfg.proxy_url)) if getattr(cfg, "proxy_url", None) else "direct",
                    url,
                )

                @browser(**cfg.browser_kwargs)
                def run_retry(driver: Driver, data):
                    if cfg.human_mode:
                        try:
                            driver.enable_human_mode()
                        except Exception:
                            pass
                    return self._load_listing_cards_botasaurus(driver, url=data["url"], cfg=cfg)

                try:
                    cards = run_retry({"url": url, "max_pages": max_pages})
                    if cards:
                        return cards
                except Exception as exc:
                    self.logger.warning("OLX listing retry %s failed: %s", retry_index, exc)
        finally:
            if previous_profile is None:
                os.environ.pop("BOTASAURUS_PROFILE", None)
            else:
                os.environ["BOTASAURUS_PROFILE"] = previous_profile
            reset_cached_proxy()

        return None

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

    def _parse_information_ads_date(self, information_ads: str | None) -> date | None:
        text = re.sub(r"\s+", " ", str(information_ads or "")).strip().lower()
        if not text:
            return None

        today = date.today()
        if text in {"hari ini", "today"}:
            return today
        if "kemarin" in text:
            try:
                from datetime import timedelta

                return today - timedelta(days=1)
            except Exception:
                return None

        relative_match = re.search(r"(\d+)\s+hari\s+yang\s+lalu", text)
        if relative_match:
            try:
                from datetime import timedelta

                return today - timedelta(days=int(relative_match.group(1)))
            except Exception:
                return None

        month_map = {
            "jan": 1,
            "januari": 1,
            "feb": 2,
            "februari": 2,
            "mar": 3,
            "maret": 3,
            "apr": 4,
            "april": 4,
            "mei": 5,
            "jun": 6,
            "juni": 6,
            "jul": 7,
            "juli": 7,
            "agu": 8,
            "agustus": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "okt": 10,
            "oktober": 10,
            "nov": 11,
            "november": 11,
            "des": 12,
            "desember": 12,
        }
        match = re.search(r"\b(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?\b", text)
        if not match:
            return None
        day = int(match.group(1))
        month = month_map.get(match.group(2))
        if not month:
            return None
        year = int(match.group(3)) if match.group(3) else today.year
        try:
            return date(year, month, day)
        except Exception:
            return None

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

        match = re.search(r"\s+([A-Za-z][A-Za-z-]*)\s*-\s*(AT|MT|A/T|M/T|Automatic|Manual)\s*$", value, re.IGNORECASE)
        if not match:
            return None, None, None

        variant = value[: match.start()].strip(" -") or None
        fuel_type = match.group(1).strip()
        transmission = match.group(2).strip().upper().replace("/", "")
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
        if re.search(r"<\s*\d", value):
            picked = numbers[0]
        else:
            picked = numbers[-1]
        normalized = picked.replace(".", "").replace(",", "")
        return normalized or None

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

                const labels = Array.from(document.querySelectorAll('#main_content ._CCSn, #main_content div, #main_content span'));
                for (const labelEl of labels) {
                  const label = norm(labelEl.innerText || labelEl.textContent).toLowerCase();
                  if (!wanted.some((wantedLabel) => label === wantedLabel)) continue;

                  const siblings = [];
                  if (labelEl.nextElementSibling) siblings.push(labelEl.nextElementSibling);
                  const parent = labelEl.parentElement;
                  if (parent) {
                    siblings.push(...Array.from(parent.querySelectorAll('._3VRXh')));
                    siblings.push(...Array.from(parent.children).filter((el) => el !== labelEl));
                  }

                  for (const candidate of siblings) {
                    const value = norm(candidate.innerText || candidate.textContent);
                    const valueLower = value.toLowerCase();
                    if (!value || valueLower === label || wanted.includes(valueLower)) continue;
                    if (value.length <= 140) return value;
                  }
                }
                return null;
                """,
                {"labels": list(labels)},
            )
        except Exception:
            return None

    def _wait_for_label_value_botasaurus(self, driver, labels: tuple[str, ...], *, timeout_seconds: float = 8.0) -> str | None:
        deadline = time.time() + max(0.1, timeout_seconds)
        while time.time() < deadline:
            value = self._extract_label_value_botasaurus(driver, labels)
            if value:
                return value
            time.sleep(0.25)
        return None

    def _extract_location_botasaurus(self, driver) -> str | None:
        value = self._wait_for_label_value_botasaurus(
            driver,
            ("lokasi", "location"),
            timeout_seconds=self._env_float("OLX_DETAIL_LOCATION_WAIT_SECONDS", 8.0, minimum=0.1),
        )
        if value:
            return value

        try:
            fallback = driver.run_js(
                r"""
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const bad = /^(lokasi|location|lihat peta|share|bagikan|chat|hubungi|jual mobil)$/i;
                const selectors = [
                  '[data-aut-id="itemLocation"]',
                  '[data-aut-id="item-location"]',
                  '[data-aut-id*="location" i]',
                  '[class*="location" i]',
                  '[aria-label*="location" i]',
                  '[aria-label*="lokasi" i]'
                ];

                for (const selector of selectors) {
                  for (const el of Array.from(document.querySelectorAll(selector))) {
                    const text = norm(el.innerText || el.textContent || el.getAttribute('aria-label'));
                    if (text && !bad.test(text) && text.length <= 160) return text;
                  }
                }

                const haystack = norm(document.querySelector('#main_content')?.innerText || document.body?.innerText || '');
                const labeled = haystack.match(/(?:Lokasi|Location)\s*[:\n]\s*([^\n]+?)(?=\s{2,}|\n|$)/i);
                if (labeled) {
                  const text = norm(labeled[1]);
                  if (text && !bad.test(text) && text.length <= 160) return text;
                }

                return null;
                """
            )
        except Exception:
            fallback = None
        if fallback:
            return str(fallback).strip() or None
        return None

    def _extract_engine_cc_botasaurus(self, driver) -> str | None:
        value = self._wait_for_label_value_botasaurus(
            driver,
            ("kapasitas mesin", "engine capacity", "engine"),
            timeout_seconds=self._env_float("OLX_DETAIL_ENGINE_CC_WAIT_SECONDS", 8.0, minimum=0.1),
        )
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
                const galleries = Array.from(document.querySelectorAll('[data-aut-id="imageGallery"]'));
                const root = galleries
                  .map((el) => ({ el, rect: el.getBoundingClientRect() }))
                  .filter((item) => item.rect.width > 300 && item.rect.height > 200)
                  .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.el || galleries[0];
                if (!root) return [];
                for (const img of Array.from(root.querySelectorAll('img'))) {
                  add(img.currentSrc || img.src || img.getAttribute('src'));
                  add(img.getAttribute('srcset'));
                  add(img.getAttribute('data-src'));
                  const source = img.closest('picture') && img.closest('picture').querySelector('source[srcset]');
                  add(source && source.getAttribute('srcset'));
                }
                for (const el of Array.from(root.querySelectorAll('[style]'))) {
                  const style = String(el.getAttribute('style') || '');
                  const matches = style.match(/url\(["']?([^"')]+)["']?\)/ig) || [];
                  for (const match of matches) {
                    const cleaned = match.replace(/^url\(["']?/i, '').replace(/["']?\)$/i, '');
                    add(cleaned);
                  }
                }
                const html = root.innerHTML || '';
                const apolloMatches = html.match(/https?:\/\/apollo\.olx\.co\.id(?::443)?\/v1\/files\/[^\s"'<>]+\/image[^\s"'<>]*/g) || [];
                for (const match of apolloMatches) add(match.replace(':443/', '/'));
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
        self._open_gallery_botasaurus(driver)
        time.sleep(self._env_float("OLX_IMAGE_GALLERY_OPEN_WAIT_SECONDS", 0.5, minimum=0.0))
        return self._extract_images_once_botasaurus(driver) or None

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

    def _extract_listing_cards_botasaurus(self, driver) -> list[dict]:
        try:
            items = driver.run_js(
                r"""
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const out = [];
                for (const li of Array.from(document.querySelectorAll('li[data-aut-id="itemBox"]'))) {
                  const a = li.querySelector('a[href*="/item/"][href*="iid-"]');
                  if (!a) continue;
                  const details = li.querySelector('[data-aut-id="itemDetails"]');
                  let informationAds = null;
                  if (details) {
                    const span = details.querySelector('span');
                    informationAds = norm(span && (span.innerText || span.textContent));
                  }
                  if (!informationAds) {
                    const text = norm(li.innerText || li.textContent);
                    const match = text.match(/(Hari ini|Kemarin|\d+\s+hari\s+yang\s+lalu|\d{1,2}\s+(?:Jan|Januari|Feb|Februari|Mar|Maret|Apr|April|Mei|Jun|Juni|Jul|Juli|Agu|Agustus|Sep|Sept|September|Okt|Oktober|Nov|November|Des|Desember)(?:\s+\d{4})?)/i);
                    informationAds = match ? match[1] : null;
                  }
                  out.push({
                    listing_url: a.href || a.getAttribute('href'),
                    information_ads: informationAds || null,
                  });
                }
                return out;
                """
            )
        except Exception:
            items = []
        cards: list[dict] = []
        seen: set[str] = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            url = urljoin("https://www.olx.co.id", str(item.get("listing_url") or "").strip())
            if self._is_listing_detail_url(url):
                if url in seen:
                    continue
                seen.add(url)
                information_ads = str(item.get("information_ads") or "").strip() or None
                cards.append(
                    {
                        "listing_url": url,
                        "information_ads": information_ads,
                        "information_ads_date": self._parse_information_ads_date(information_ads),
                    }
                )
        return cards

    def _extract_listing_urls_botasaurus(self, driver) -> list[str]:
        return [str(item["listing_url"]) for item in self._extract_listing_cards_botasaurus(driver) if item.get("listing_url")]

    def _scrape_single_detail_botasaurus(
        self,
        driver,
        *,
        url: str,
        cfg,
        listing_id: str | None = None,
        listing_meta: dict | None = None,
        tracking_mode: bool = False,
    ) -> dict:
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
            if not tracking_mode:
                raise RuntimeError("OLX listing unavailable/not-found during fresh scrape; not marking sold")
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
        if not location_value:
            self.logger.debug("OLX location missing but detail data was extracted: %s", url)

        return {
            "listing_id": listing_id_final,
            "listing_url": url,
            "condition": None,
            "brand": brand_value,
            "model": model_value,
            "variant": variant_value,
            "information_ads": (listing_meta or {}).get("information_ads"),
            "information_ads_date": (listing_meta or {}).get("information_ads_date"),
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

    def _scrape_single_detail_with_retries(
        self,
        driver,
        *,
        url: str,
        cfg,
        listing_id: str | None = None,
        listing_meta: dict | None = None,
        tracking_mode: bool = False,
    ) -> dict:
        retries = self._env_int("OLX_DETAIL_RETRIES", 2, minimum=0)
        retry_wait_seconds = self._env_float("OLX_DETAIL_RETRY_WAIT_SECONDS", 2.0, minimum=0.0)
        last_exc: Exception | None = None

        for attempt in range(1, retries + 2):
            try:
                return self._scrape_single_detail_botasaurus(
                    driver,
                    url=url,
                    cfg=cfg,
                    listing_id=listing_id,
                    listing_meta=listing_meta,
                    tracking_mode=tracking_mode,
                )
            except Exception as exc:
                last_exc = exc
                if attempt > retries:
                    break
                self.logger.warning(
                    "Retrying OLX detail after error. retry=%s/%s url=%s error=%s",
                    attempt,
                    retries,
                    url,
                    exc,
                )
                if retry_wait_seconds:
                    time.sleep(retry_wait_seconds)

        if last_exc:
            raise last_exc
        raise RuntimeError("OLX detail retry exhausted")

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
            return self._load_listing_cards_botasaurus(driver, url=data["url"], cfg=cfg)

        try:
            cards = run({"url": url, "max_pages": max_pages}) or []
        except Exception as exc:
            self.logger.warning("OLX listing scrape failed, trying new session/proxy: %s", exc)
            base_profile = str(cfg.browser_kwargs.get("profile") or "olx")
            cards = self._retry_listing_with_new_session(url=url, max_pages=max_pages, base_profile=base_profile) or []
        self._listing_meta_by_url = {str(item.get("listing_url")): item for item in cards if item.get("listing_url")}
        links = list(self._listing_meta_by_url.keys())
        self.logger.info("Collected %s OLX links.", len(links))
        return links

    def scrape_listing_details(
        self,
        links: list[str],
        *,
        listing_id_by_url: dict[str, str] | None = None,
        listing_meta_by_url: dict[str, dict] | None = None,
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
          variant = EXCLUDED.variant,
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_olx.information_ads),
          information_ads_date = COALESCE(EXCLUDED.information_ads_date, cars_scrap_olx.information_ads_date),
          location = COALESCE(EXCLUDED.location, cars_scrap_olx.location),
          price = COALESCE(EXCLUDED.price, cars_scrap_olx.price),
          year = COALESCE(EXCLUDED.year, cars_scrap_olx.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_olx.mileage),
          transmission = EXCLUDED.transmission,
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_olx.seat_capacity),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_olx.engine_cc),
          fuel_type = EXCLUDED.fuel_type,
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

                tracking_mode = bool(listing_id_by_url)
                listing_id = listing_id_by_url.get(url) if listing_id_by_url else None
                listing_meta = None
                if listing_meta_by_url:
                    listing_meta = listing_meta_by_url.get(url)
                if listing_meta is None:
                    listing_meta = getattr(self, "_listing_meta_by_url", {}).get(url)
                try:
                    result = self._scrape_single_detail_with_retries(
                        driver,
                        url=url,
                        cfg=cfg,
                        listing_id=listing_id,
                        listing_meta=listing_meta,
                        tracking_mode=tracking_mode,
                    )
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
