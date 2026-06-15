from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

from cars_scrap_common.botasaurus_utils import load_botasaurus_config, wait_for_css
from cars_scrap_common.env_utils import env_bool
from cars_scrap_common.logging_utils import setup_logger
from cars_scrap_common.proxy_utils import load_proxy_for_run, mask_proxy_url, probe_public_ip, reset_cached_proxy
from cars_scrap_common.shared_base_service import SharedBaseService
from scrap_goonet_cars_botasaurus.database import get_connection

load_dotenv(override=True)

BASE_URL = "https://www.goo-net-exchange.com"
DETAIL_URL_RE = re.compile(r"/usedcars/[^/]+/[^/]+/(\d+)/?", re.IGNORECASE)


class GoonetCarsService(SharedBaseService):
    def __init__(self):
        self.logger = setup_logger("scrape_goonet_cars")
        self.stop_flag = False
        self._logged_proxy_ip = False
        self.listing_url = os.getenv("GOONET_CARS_LISTING_URL", "").strip()

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
        match = DETAIL_URL_RE.search(str(url or ""))
        if match:
            return match.group(1)
        raise ValueError(f"Unable to extract listing_id from url={url!r}")

    def _is_detail_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        host = (parsed.hostname or "").lower()
        return host.endswith("goo-net-exchange.com") and bool(DETAIL_URL_RE.search(parsed.path))

    def _normalize_url(self, href: str | None) -> str | None:
        value = str(href or "").strip()
        if not value or value.startswith("javascript:") or value.startswith("#"):
            return None
        return urljoin(BASE_URL, value.split("#", 1)[0])

    def _parse_money(self, value: object | None) -> int | None:
        if isinstance(value, (int, float)):
            amount = int(value)
            return amount if amount > 0 else None
        text = str(value or "").strip()
        if not text:
            return None
        digits = re.findall(r"\d+", text.replace(",", ""))
        if not digits:
            return None
        try:
            amount = int("".join(digits))
        except Exception:
            return None
        return amount if amount > 0 else None

    def _parse_date(self, value: object | None):
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"(\d{4}/\d{2}/\d{2})", text)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y/%m/%d").date()
        except Exception:
            return None

    def _parse_year(self, value: object | None) -> int | None:
        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value or ""))
        if not match:
            return None
        return int(match.group(1))

    def _clean_text(self, value: object | None) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text or None

    def _first_json_ld_value(self, items: list[dict], key: str):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            value = item.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, dict):
                return value.get("name") or value.get("@id") or value
            return value
        return None

    def _navigate(self, driver, url: str, cfg) -> None:
        try:
            driver.get_via(url, referer="https://www.google.com/")
        except Exception:
            pass
        driver.google_get(url, bypass_cloudflare=cfg.bypass_cloudflare)

    def _page_unavailable(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "page not found",
                "not found",
                "already been sold",
                "this car has already been sold",
                "no longer available",
            ),
        )

    def _extract_links_botasaurus(self, driver) -> list[str]:
        try:
            hrefs = driver.run_js(
                """
                return Array.from(document.querySelectorAll('a[href]'))
                  .map((a) => a.href || a.getAttribute('href') || '')
                  .filter(Boolean);
                """
            )
        except Exception:
            hrefs = []

        seen: set[str] = set()
        links: list[str] = []
        for href in hrefs or []:
            url = self._normalize_url(str(href))
            if not url or url in seen or not self._is_detail_url(url):
                continue
            seen.add(url)
            links.append(url)
        return links

    def _next_page_url_botasaurus(self, driver) -> str | None:
        try:
            href = driver.run_js(
                """
                const candidates = Array.from(document.querySelectorAll('a[href]'));
                const next = candidates.find((a) => {
                  const text = ((a.innerText || a.textContent || '') + ' ' + (a.getAttribute('rel') || '') + ' ' + (a.getAttribute('class') || '')).toLowerCase();
                  return text.includes('next') || text.includes('＞') || text.includes('>');
                });
                return next ? (next.href || next.getAttribute('href')) : null;
                """
            )
        except Exception:
            href = None
        url = self._normalize_url(href)
        return url if url and url.startswith(BASE_URL) else None

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1) -> list[str]:
        url = (start_url or self.listing_url or "").strip()
        if not url:
            raise ValueError("Missing start_url or GOONET_CARS_LISTING_URL.")
        max_pages = max(1, int(max_pages or 1))

        from botasaurus.browser import Driver, browser

        cfg = load_botasaurus_config(service_name="goonet")
        self._maybe_log_proxy_and_ip(cfg)

        @browser(**cfg.browser_kwargs)
        def run(driver: Driver, data):
            if cfg.human_mode:
                try:
                    driver.enable_human_mode()
                except Exception:
                    pass

            links: list[str] = []
            seen: set[str] = set()
            current_url = data["url"]
            for page in range(1, data["max_pages"] + 1):
                if self.stop_flag or not current_url:
                    break
                self.logger.info("Listing page %s/%s: %s", page, data["max_pages"], current_url)
                self._navigate(driver, current_url, cfg)
                wait_for_css(
                    driver,
                    "a[href]",
                    timeout_seconds=cfg.listing_wait_seconds,
                    poll_interval_seconds=cfg.poll_interval_seconds,
                )
                for link in self._extract_links_botasaurus(driver):
                    if link in seen:
                        continue
                    seen.add(link)
                    links.append(link)
                next_url = self._next_page_url_botasaurus(driver)
                if not next_url or next_url == current_url:
                    break
                current_url = next_url
            return links

        return run({"url": url, "max_pages": max_pages}) or []

    def _extract_detail_js(self, driver) -> dict:
        return driver.run_js(
            r"""
            const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim() || null;
            const abs = (value) => {
              if (!value) return null;
              try { return new URL(value, location.href).href; } catch (_) { return value; }
            };
            const text = (selector, root = document) => {
              const el = root.querySelector(selector);
              return el ? clean(el.innerText || el.textContent) : null;
            };
            const valueByLabel = (label) => {
              const terms = [String(label || '').toLowerCase()];
              for (const dt of Array.from(document.querySelectorAll('dt'))) {
                const key = clean(dt.innerText || dt.textContent);
                if (!key || !terms.includes(key.toLowerCase())) continue;
                const dd = dt.parentElement ? dt.parentElement.querySelector('dd') : null;
                const value = dd ? clean(dd.innerText || dd.textContent) : null;
                if (value !== null) return value;
              }
              return null;
            };
            const overview = {};
            for (const dl of Array.from(document.querySelectorAll('.list-info dl'))) {
              const dt = dl.querySelector('dt');
              const dd = dl.querySelector('dd');
              const key = dt ? clean(dt.innerText || dt.textContent) : null;
              const value = dd ? clean(dd.innerText || dd.textContent) : null;
              if (key && !(key in overview)) overview[key] = value;
            }
            const categoryData = {};
            for (const h3 of Array.from(document.querySelectorAll('h3.ttl1'))) {
              const name = clean(h3.innerText || h3.textContent);
              if (!name) continue;
              const block = h3.nextElementSibling;
              const active = [];
              const inactive = [];
              if (block) {
                for (const li of Array.from(block.querySelectorAll('li'))) {
                  const value = clean(li.innerText || li.textContent);
                  if (!value) continue;
                  if (li.classList.contains('active')) active.push(value);
                  else inactive.push(value);
                }
              }
              categoryData[name] = { active, inactive };
            }
            const rates = {};
            for (const block of Array.from(document.querySelectorAll('.block-rate'))) {
              const key = text('.sub01', block);
              const value = text('.rate-sub', block);
              if (key) rates[key.replace(':', '')] = value;
            }
            for (const dl of Array.from(document.querySelectorAll('.question-hover dl'))) {
              const key = text('dt', dl);
              const value = text('dd', dl);
              if (key && !(key in rates)) rates[key] = value;
            }
            const imageSet = new Set();
            for (const el of Array.from(document.querySelectorAll('.gallery-main img[src], .list-photo img[src], img[src*="picture1.goo-net.com"]'))) {
              const src = abs(el.getAttribute('src'));
              if (src && src.includes('picture1.goo-net.com') && !src.includes('/P/')) imageSet.add(src);
            }
            for (const el of Array.from(document.querySelectorAll('[style*="background-image"]'))) {
              const style = el.getAttribute('style') || '';
              const match = style.match(/url\(["']?([^"')]+)["']?\)/i);
              const src = match ? abs(match[1]) : null;
              if (src && src.includes('picture1.goo-net.com') && !src.includes('/P/')) imageSet.add(src);
            }
            const video = document.querySelector('video source[src], video[src]');
            const report = document.querySelector('a[data-fancybox^="report"][href], p.report a[href]');
            const hidden = {};
            for (const input of Array.from(document.querySelectorAll('input[type="hidden"][name], input[type="hidden"][id]'))) {
              const key = input.getAttribute('name') || input.getAttribute('id');
              if (key) hidden[key] = input.getAttribute('value') || '';
            }
            const currencyValues = (() => {
              for (const id of ['currency_price_fob_calculate', 'before_b_to_b_price_fob_calculate', 'origin_price_fob_calculate']) {
                const el = document.getElementById(id);
                if (!el) continue;
                try { return JSON.parse(el.value || '{}'); } catch (_) {}
              }
              return {};
            })();
            return {
              brand_label: text('.sidebar .ttl1') || text('.ttl1'),
              title: text('h1.sub'),
              status_badge: text('.cmn-cate01 .red'),
              year_text: text('.sidebar .box .date') || text('.box-middle .box .date'),
              location: text('.sidebar .box .location') || text('.box-middle .box .location'),
              published_at_text: text('.sidebar .box .date01') || text('.box-middle .box .date01'),
              stock_number: text('.number span'),
              fob_price_text: text('#fob_default') || text('#estimated-total-price') || text('#price_fob_calculate_detail_sp'),
              currency_values: currencyValues,
              overview,
              overview_by_label: {
                month_year: valueByLabel('Month/Year'),
                color: valueByLabel('Color'),
                mileage: valueByLabel('Mileage'),
                repaired: valueByLabel('Repaired'),
                steering: valueByLabel('Steering'),
                transmission: valueByLabel('Transmission'),
                fuel: valueByLabel('Fuel'),
                drive_system: valueByLabel('Drive System'),
                doors: valueByLabel('Doors'),
                displacement: valueByLabel('Displacement'),
                chassis_no: valueByLabel('Chassis No'),
                model_code: valueByLabel('Model code')
              },
              ratings: rates,
              report_url: report ? abs(report.getAttribute('href')) : null,
              certified: !!Array.from(document.querySelectorAll('body *')).find((el) => clean(el.innerText || el.textContent) === 'Certified'),
              images: Array.from(imageSet),
              image_count_text: text('.camera, .photo-count, .count'),
              video_url: video ? abs(video.getAttribute('src') || video.querySelector('source')?.getAttribute('src')) : null,
              categories: categoryData,
              hidden_codes: hidden
            };
            """
        ) or {}

    def _json_ld_objects_from_html(self, soup: BeautifulSoup) -> list[dict]:
        objects: list[dict] = []

        def add_obj(value) -> None:
            if isinstance(value, dict):
                objects.append(value)
                graph = value.get("@graph")
                if isinstance(graph, list):
                    for item in graph:
                        add_obj(item)
            elif isinstance(value, list):
                for item in value:
                    add_obj(item)

        for script in soup.select('script[type="application/ld+json"]'):
            raw = script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                add_obj(json.loads(raw))
            except Exception:
                continue
        return objects

    def _extract_detail_html(self, html: str, url: str) -> tuple[dict, list[dict]]:
        soup = BeautifulSoup(html or "", "html.parser")

        def clean(value: object | None) -> str | None:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            return text or None

        def text(selector: str, root=None) -> str | None:
            node = (root or soup).select_one(selector)
            return clean(node.get_text(" ", strip=True)) if node else None

        def attr(selector: str, name: str) -> str | None:
            node = soup.select_one(selector)
            value = node.get(name) if node else None
            return self._normalize_url(str(value)) if value and name in {"href", "src"} else clean(value)

        overview: dict[str, str | None] = {}
        overview_by_label: dict[str, str | None] = {}
        for dl in soup.select(".list-info dl"):
            key = text("dt", dl)
            value = text("dd", dl)
            if key and key not in overview:
                overview[key] = value
                normalized_key = key.lower().replace("/", "_").replace(" ", "_")
                overview_by_label[normalized_key] = value

        label_map = {
            "month_year": "Month/Year",
            "color": "Color",
            "mileage": "Mileage",
            "repaired": "Repaired",
            "steering": "Steering",
            "transmission": "Transmission",
            "fuel": "Fuel",
            "drive_system": "Drive System",
            "doors": "Doors",
            "displacement": "Displacement",
            "chassis_no": "Chassis No",
            "model_code": "Model code",
        }
        for key, label in label_map.items():
            if key in overview_by_label:
                continue
            for dl in soup.select("dl"):
                if clean(dl.select_one("dt").get_text(" ", strip=True) if dl.select_one("dt") else None) == label:
                    overview_by_label[key] = text("dd", dl)
                    break

        categories: dict[str, dict[str, list[str]]] = {}
        for heading in soup.select("h3.ttl1"):
            name = clean(heading.get_text(" ", strip=True))
            if not name:
                continue
            block = heading.find_next_sibling()
            active: list[str] = []
            inactive: list[str] = []
            if block:
                for item in block.select("li"):
                    value = clean(item.get_text(" ", strip=True))
                    if not value:
                        continue
                    if "active" in (item.get("class") or []):
                        active.append(value)
                    else:
                        inactive.append(value)
            categories[name] = {"active": active, "inactive": inactive}

        ratings: dict[str, str | None] = {}
        for block in soup.select(".block-rate"):
            key = text(".sub01", block)
            value = text(".rate-sub", block)
            if key:
                ratings[key.replace(":", "")] = value
        for dl in soup.select(".question-hover dl"):
            key = text("dt", dl)
            value = text("dd", dl)
            if key and key not in ratings:
                ratings[key] = value

        images: list[str] = []
        seen_images: set[str] = set()
        for node in soup.select('.gallery-main img[src], .list-photo img[src], img[src*="picture1.goo-net.com"]'):
            src = self._normalize_url(str(node.get("src") or ""))
            if src and "picture1.goo-net.com" in src and "/P/" not in src and src not in seen_images:
                seen_images.add(src)
                images.append(src)
        for node in soup.select('[style*="background-image"]'):
            style = str(node.get("style") or "")
            match = re.search(r"url\([\"']?([^\"')]+)[\"']?\)", style, re.IGNORECASE)
            src = self._normalize_url(match.group(1)) if match else None
            if src and "picture1.goo-net.com" in src and "/P/" not in src and src not in seen_images:
                seen_images.add(src)
                images.append(src)

        hidden_codes = {}
        for node in soup.select('input[type="hidden"]'):
            key = node.get("name") or node.get("id")
            if key:
                hidden_codes[str(key)] = node.get("value") or ""

        currency_values = {}
        for key in ("currency_price_fob_calculate", "before_b_to_b_price_fob_calculate", "origin_price_fob_calculate"):
            raw = hidden_codes.get(key)
            if not raw:
                continue
            try:
                currency_values = json.loads(raw)
                break
            except Exception:
                continue

        video_node = soup.select_one("video source[src], video[src]")
        report_url = attr('a[data-fancybox^="report"][href], p.report a[href]', "href")
        data = {
            "brand_label": text(".sidebar .ttl1") or text(".ttl1"),
            "title": text("h1.sub") or text("h3.sub"),
            "status_badge": text(".cmn-cate01 .red"),
            "year_text": text(".sidebar .box .date") or text(".box-middle .box .date"),
            "location": text(".sidebar .box .location") or text(".box-middle .box .location"),
            "published_at_text": text(".sidebar .box .date01") or text(".box-middle .box .date01"),
            "stock_number": text(".number span"),
            "fob_price_text": text("#fob_default") or text("#estimated-total-price") or text("#price_fob_calculate_detail_sp"),
            "currency_values": currency_values,
            "overview": overview,
            "overview_by_label": overview_by_label,
            "ratings": ratings,
            "report_url": report_url,
            "certified": "Certified" in soup.get_text(" ", strip=True),
            "images": images,
            "image_count_text": text(".camera") or text(".photo-count") or text(".count"),
            "video_url": self._normalize_url(str(video_node.get("src") or "")) if video_node else None,
            "categories": categories,
            "hidden_codes": hidden_codes,
        }
        return data, self._json_ld_objects_from_html(soup)

    def _http_proxies(self) -> dict[str, str] | None:
        proxy_url = load_proxy_for_run(service_name="goonet")
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    def _goonet_http_headers(self, *, ajax: bool = False) -> dict[str, str]:
        headers = {
            "accept": "*/*" if ajax else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9,id;q=0.8",
            "referer": os.getenv("GOONET_REFERER") or "https://www.goo-net-exchange.com/php/search/summary.php",
            "user-agent": os.getenv("GOONET_USER_AGENT") or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        }
        if ajax:
            headers.update(
                {
                    "cache-control": "no-cache",
                    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "origin": BASE_URL,
                    "x-requested-with": "XMLHttpRequest",
                }
            )
        csrf = (os.getenv("GOONET_X_CSRF_TOKEN") or "").strip()
        if csrf:
            headers["x-csrf-token"] = csrf
        cookie = (os.getenv("GOONET_COOKIE") or "").strip()
        if cookie:
            headers["cookie"] = cookie
        return headers

    def _request_goonet(self, method: str, url: str, **kwargs) -> requests.Response:
        timeout = self._env_float("GOONET_HTTP_TIMEOUT", 30.0, minimum=1.0)
        proxies = self._http_proxies()
        response = requests.request(method, url, timeout=timeout, proxies=proxies, **kwargs)
        response.raise_for_status()
        return response

    def _parse_master_name_count(self, value: object | None) -> tuple[str | None, int | None]:
        text = self._clean_text(value)
        if not text:
            return None, None
        match = re.search(r"^(.*?)\s*\(([\d,]+)\)\s*$", text)
        if not match:
            return text, None
        name = self._clean_text(match.group(1))
        try:
            count = int(match.group(2).replace(",", ""))
        except Exception:
            count = None
        return name, count

    def _decode_goonet_json(self, response: requests.Response):
        try:
            return response.json()
        except Exception:
            text = response.text.strip()
            if not text:
                return []
            return json.loads(text)

    def _fetch_detail_http(self, url: str) -> str:
        response = self._request_goonet("GET", url, headers=self._goonet_http_headers(ajax=False))
        return response.text

    def _build_detail_result(self, *, url: str, listing_id_final: str, data: dict, json_ld_items: list[dict], tracking_mode: bool) -> dict:
        title = self._clean_text(data.get("title") or self._first_json_ld_value(json_ld_items, "name"))
        brand, model, variant = self._split_title(
            url,
            title,
            data.get("brand_label") or self._first_json_ld_value(json_ld_items, "brand"),
        )
        overview_by_label = data.get("overview_by_label") or {}
        currency_values = data.get("currency_values") or {}
        price_jpy = self._parse_money(currency_values.get("JPY")) or self._parse_money(
            self._first_json_ld_value(json_ld_items, "price")
        )
        price_usd = self._parse_money(currency_values.get("USD"))
        if not price_jpy and str(data.get("fob_price_text") or "").strip().startswith("¥"):
            price_jpy = self._parse_money(data.get("fob_price_text"))
        if not price_usd and str(data.get("fob_price_text") or "").strip().startswith("$"):
            price_usd = self._parse_money(data.get("fob_price_text"))
        images = list(dict.fromkeys(str(x).strip() for x in (data.get("images") or []) if str(x).strip()))
        video_url = self._clean_text(data.get("video_url"))
        location = self._clean_text(data.get("location"))
        if location:
            location = location.replace("Location", "").strip()
        seat_capacity = None
        categories = data.get("categories") or {}
        standard = categories.get("Standard Features") or {}
        for item in standard.get("active") or []:
            match = re.search(r"Seating Capacity\s*(\d+)", str(item), re.IGNORECASE)
            if match:
                seat_capacity = match.group(1)
                break
        mileage_text = overview_by_label.get("mileage")
        displacement_text = overview_by_label.get("displacement")
        published_at = self._parse_date(data.get("published_at_text"))
        year = self._parse_year(data.get("year_text") or overview_by_label.get("month_year"))
        media = {
            "images": images,
            "image_count": len(images),
            "image_count_text": data.get("image_count_text"),
            "main_image": images[0] if images else None,
            "video_url": video_url,
        }
        overview = {**(data.get("overview") or {}), "normalized": overview_by_label}
        certification = {
            "certified": bool(data.get("certified")),
            "vehicle_state_evaluation_report_url": data.get("report_url"),
        }
        specifications = {
            "car_condition": categories.get("Car Condition"),
            "standard_features": categories.get("Standard Features"),
        }
        equipment = {k: v for k, v in categories.items() if k not in {"Car Condition", "Standard Features"}}
        status = "active" if any([brand, model, title, price_jpy, price_usd, images]) else "sold"
        if status != "active" and not tracking_mode:
            raise RuntimeError("No extractable data on Goonet detail page.")
        return {
            "listing_id": listing_id_final,
            "stock_number": self._clean_text(data.get("stock_number")),
            "listing_url": url,
            "condition": "Used",
            "brand": brand,
            "model": model,
            "variant": variant,
            "information_ads": self._clean_text(data.get("published_at_text")),
            "information_ads_date": published_at,
            "location": location,
            "year": year,
            "mileage": self._parse_int_from_text(mileage_text),
            "price_jpy": price_jpy,
            "price_usd": price_usd,
            "transmission": self._clean_text(overview_by_label.get("transmission")),
            "seat_capacity": seat_capacity,
            "engine_cc": self._normalize_engine_cc(displacement_text),
            "fuel_type": self._clean_text(overview_by_label.get("fuel")),
            "series": None,
            "type": self._clean_text(overview_by_label.get("drive_system")),
            "images": images or None,
            "video_url": video_url,
            "whatsapp_number": None,
            "contact_seller": None,
            "seller_name": None,
            "media": media,
            "overview": overview,
            "ratings": data.get("ratings") or None,
            "certification": certification,
            "specifications": specifications,
            "equipment": equipment,
            "hidden_codes": data.get("hidden_codes") or None,
            "raw_json_ld": json_ld_items or None,
            "status": status,
            "error": None,
        }

    def _split_title(self, url: str, title: str | None, brand: str | None) -> tuple[str | None, str | None, str | None]:
        parsed_model = None
        try:
            parts = [p for p in urlparse(url).path.split("/") if p]
            if len(parts) >= 3 and parts[0].lower() == "usedcars":
                parsed_model = parts[2].replace("_", " ").strip() or None
        except Exception:
            parsed_model = None
        clean_title = self._clean_text(title)
        clean_brand = self._clean_text(brand)
        model = parsed_model
        variant = None
        if clean_title:
            text = clean_title
            if clean_brand and text.upper().startswith(clean_brand.upper() + " "):
                text = text[len(clean_brand) :].strip()
            if model and text.upper().startswith(model.upper() + " "):
                variant = text[len(model) :].strip() or None
            elif model and text.upper() == model.upper():
                variant = None
            else:
                tokens = text.split(" ", 1)
                if not model and tokens:
                    model = tokens[0]
                if len(tokens) > 1:
                    variant = tokens[1]
        return clean_brand, model, variant

    def _scrape_single_detail(self, driver, *, url: str, cfg, listing_id: str | None, tracking_mode: bool) -> dict:
        self._navigate(driver, url, cfg)
        current_url = str(getattr(driver, "current_url", "") or "")
        expected_id = (listing_id or self._extract_listing_id(url)).strip()

        if self._is_http_error_page_botasaurus(driver) or self._is_soft_block_page_botasaurus(driver):
            raise RuntimeError(f"Retryable error page while scraping goonet detail: current_url={current_url!r}")

        if self._page_unavailable(driver):
            if tracking_mode and expected_id:
                return self._build_sold_result(url, expected_id)
            raise RuntimeError(f"Goonet detail page unavailable: current_url={current_url!r}")

        wait_for_css(
            driver,
            "h1.sub, .list-info, script[type='application/ld+json']",
            timeout_seconds=cfg.detail_wait_seconds,
            poll_interval_seconds=cfg.poll_interval_seconds,
        )

        data = self._extract_detail_js(driver)
        json_ld_items = self._json_ld_objects_botasaurus(driver)
        listing_id_final = expected_id or self._extract_listing_id(current_url or url)
        return self._build_detail_result(
            url=url,
            listing_id_final=listing_id_final,
            data=data,
            json_ld_items=json_ld_items,
            tracking_mode=tracking_mode,
        )

    def fetch_master_makers(self, brand_cd: str | None = None) -> list[dict]:
        code = (brand_cd or os.getenv("GOONET_MASTER_SEED_BRAND_CD") or os.getenv("GOONET_MAKER_SEARCH_BRAND_CD") or "5543").strip()
        url = f"{BASE_URL}/php/search/ajax_maker_search.php"
        response = self._request_goonet(
            "POST",
            url,
            headers=self._goonet_http_headers(ajax=True),
            data={"brand_cd": code},
        )
        payload = self._decode_goonet_json(response)
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected Goonet makers response: {str(payload)[:300]}")
        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            maker_cd = self._clean_text(item.get("maker_cd") or item.get("value") or item.get("id"))
            maker_nm = self._clean_text(item.get("maker_nm") or item.get("label") or item.get("name"))
            if not maker_cd or not maker_nm:
                continue
            maker_name, count = self._parse_master_name_count(maker_nm)
            rows.append(
                {
                    "maker_cd": maker_cd,
                    "maker_nm": maker_nm,
                    "maker_name": maker_name,
                    "listing_count": count,
                    "raw": item,
                }
            )
        return rows

    def fetch_master_models(self, maker_cd: str) -> list[dict]:
        url = f"{BASE_URL}/php/search/ajax_car_search.php"
        response = self._request_goonet(
            "POST",
            url,
            headers=self._goonet_http_headers(ajax=True),
            data={"brand_cd": str(maker_cd).strip()},
        )
        payload = self._decode_goonet_json(response)
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected Goonet models response for maker_cd={maker_cd}: {str(payload)[:300]}")
        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            car_cd = self._clean_text(item.get("car_cd") or item.get("value") or item.get("id"))
            car_nm = self._clean_text(item.get("car_nm") or item.get("label") or item.get("name"))
            if not car_cd or not car_nm:
                continue
            model_name, count = self._parse_master_name_count(car_nm)
            if count is None:
                _, count = self._parse_master_name_count(f"{model_name or car_nm}{item.get('count_str') or ''}")
            rows.append(
                {
                    "maker_cd": str(maker_cd).strip(),
                    "car_cd": car_cd,
                    "car_nm": car_nm,
                    "model_name": model_name,
                    "listing_count": count,
                    "raw": item,
                }
            )
        return rows

    def fetch_master_grades(self, maker_cd: str, car_cd: str) -> list[dict]:
        url = f"{BASE_URL}/php/search/ajax_grade_search.php"
        response = self._request_goonet(
            "GET",
            url,
            headers=self._goonet_http_headers(ajax=True),
            params={"car_cd": str(car_cd).strip()},
        )
        payload = self._decode_goonet_json(response)
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected Goonet grades response for car_cd={car_cd}: {str(payload)[:300]}")
        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            grade_cd = self._clean_text(item.get("grade_cd") or item.get("value") or item.get("id"))
            grade_nm = self._clean_text(item.get("grade_nm") or item.get("label") or item.get("name"))
            if not grade_cd or not grade_nm:
                continue
            grade_name, count = self._parse_master_name_count(grade_nm)
            rows.append(
                {
                    "maker_cd": str(maker_cd).strip(),
                    "car_cd": str(car_cd).strip(),
                    "grade_cd": grade_cd,
                    "grade_nm": grade_nm,
                    "grade_name": grade_name,
                    "listing_count": count,
                    "raw": item,
                }
            )
        return rows

    def sync_master_data(
        self,
        *,
        brand_cd: str | None = None,
        max_makers: int = 0,
        max_models: int = 0,
        resume: bool = True,
    ) -> dict:
        makers = self.fetch_master_makers(brand_cd=brand_cd)
        if max_makers and max_makers > 0:
            makers = makers[:max_makers]
        conn = get_connection()
        cursor = conn.cursor()
        maker_sql = """
        INSERT INTO cars_scrap_goonet_makers (maker_cd, maker_nm, maker_name, listing_count, raw)
        VALUES %s
        ON CONFLICT (maker_cd) DO UPDATE SET
          maker_nm = EXCLUDED.maker_nm,
          maker_name = EXCLUDED.maker_name,
          listing_count = EXCLUDED.listing_count,
          raw = EXCLUDED.raw,
          updated_at = NOW()
        """
        model_sql = """
        INSERT INTO cars_scrap_goonet_models (maker_cd, car_cd, car_nm, model_name, listing_count, raw)
        VALUES %s
        ON CONFLICT (car_cd) DO UPDATE SET
          maker_cd = EXCLUDED.maker_cd,
          car_nm = EXCLUDED.car_nm,
          model_name = EXCLUDED.model_name,
          listing_count = EXCLUDED.listing_count,
          raw = EXCLUDED.raw,
          updated_at = NOW()
        """
        grade_sql = """
        INSERT INTO cars_scrap_goonet_grades (maker_cd, car_cd, grade_cd, grade_nm, grade_name, listing_count, raw)
        VALUES %s
        ON CONFLICT (car_cd, grade_cd) DO UPDATE SET
          maker_cd = EXCLUDED.maker_cd,
          grade_nm = EXCLUDED.grade_nm,
          grade_name = EXCLUDED.grade_name,
          listing_count = EXCLUDED.listing_count,
          raw = EXCLUDED.raw,
          updated_at = NOW()
        """
        total_models = 0
        total_grades = 0
        skipped_grade_models = 0
        errors: list[dict] = []

        def has_existing_grades(car_cd: str) -> bool:
            if not resume:
                return False
            cursor.execute("SELECT 1 FROM cars_scrap_goonet_grades WHERE car_cd = %s LIMIT 1", (str(car_cd),))
            return cursor.fetchone() is not None

        try:
            self.logger.info("Goonet master sync: %s makers found%s", len(makers), " (resume enabled)" if resume else "")
            if makers:
                execute_values(
                    cursor,
                    maker_sql,
                    [(r["maker_cd"], r["maker_nm"], r["maker_name"], r["listing_count"], Json(r["raw"])) for r in makers],
                    page_size=100,
                )
                conn.commit()
            for maker_index, maker in enumerate(makers, start=1):
                maker_cd = maker["maker_cd"]
                maker_name = maker.get("maker_name") or maker.get("maker_nm") or maker_cd
                self.logger.info("Goonet master sync maker %s/%s: %s (%s)", maker_index, len(makers), maker_name, maker_cd)
                try:
                    models = self.fetch_master_models(maker_cd)
                    if max_models and max_models > 0:
                        models = models[:max_models]
                    self.logger.info("Goonet master sync maker %s/%s: %s models", maker_index, len(makers), len(models))
                    if models:
                        execute_values(
                            cursor,
                            model_sql,
                            [
                                (r["maker_cd"], r["car_cd"], r["car_nm"], r["model_name"], r["listing_count"], Json(r["raw"]))
                                for r in models
                            ],
                            page_size=100,
                        )
                        conn.commit()
                    total_models += len(models)
                    for model_index, model in enumerate(models, start=1):
                        car_cd = model["car_cd"]
                        model_name = model.get("model_name") or model.get("car_nm") or car_cd
                        try:
                            if has_existing_grades(car_cd):
                                skipped_grade_models += 1
                                self.logger.debug(
                                    "Goonet master sync maker %s/%s model %s/%s: %s (%s) skipped, grades already exist",
                                    maker_index,
                                    len(makers),
                                    model_index,
                                    len(models),
                                    model_name,
                                    car_cd,
                                )
                                continue
                            self.logger.info(
                                "Goonet master sync maker %s/%s model %s/%s: fetching grades for %s (%s)",
                                maker_index,
                                len(makers),
                                model_index,
                                len(models),
                                model_name,
                                car_cd,
                            )
                            grades = self.fetch_master_grades(maker_cd, car_cd)
                            if grades:
                                execute_values(
                                    cursor,
                                    grade_sql,
                                    [
                                        (
                                            r["maker_cd"],
                                            r["car_cd"],
                                            r["grade_cd"],
                                            r["grade_nm"],
                                            r["grade_name"],
                                            r["listing_count"],
                                            Json(r["raw"]),
                                        )
                                        for r in grades
                                    ],
                                    page_size=100,
                                )
                                conn.commit()
                            total_grades += len(grades)
                            self.logger.info(
                                "Goonet master sync maker %s/%s model %s/%s: %s grades saved",
                                maker_index,
                                len(makers),
                                model_index,
                                len(models),
                                len(grades),
                            )
                        except Exception as exc:
                            conn.rollback()
                            errors.append({"maker_cd": maker_cd, "car_cd": car_cd, "error": str(exc)})
                except Exception as exc:
                    conn.rollback()
                    errors.append({"maker_cd": maker_cd, "error": str(exc)})
            return {
                "makers_count": len(makers),
                "models_count": total_models,
                "grades_count": total_grades,
                "skipped_grade_models_count": skipped_grade_models,
                "resume": resume,
                "errors_count": len(errors),
                "errors": errors,
            }
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def sync_missing_master_grades(self, *, limit: int = 0) -> dict:
        conn = get_connection()
        cursor = conn.cursor()
        grade_sql = """
        INSERT INTO cars_scrap_goonet_grades (maker_cd, car_cd, grade_cd, grade_nm, grade_name, listing_count, raw)
        VALUES %s
        ON CONFLICT (car_cd, grade_cd) DO UPDATE SET
          maker_cd = EXCLUDED.maker_cd,
          grade_nm = EXCLUDED.grade_nm,
          grade_name = EXCLUDED.grade_name,
          listing_count = EXCLUDED.listing_count,
          raw = EXCLUDED.raw,
          updated_at = NOW()
        """
        try:
            sql = """
            SELECT m.maker_cd, m.car_cd, m.model_name, m.car_nm
            FROM cars_scrap_goonet_models m
            WHERE NOT EXISTS (
              SELECT 1 FROM cars_scrap_goonet_grades g WHERE g.car_cd = m.car_cd
            )
            ORDER BY m.maker_cd, m.car_cd
            """
            params: list[object] = []
            if limit and limit > 0:
                sql += " LIMIT %s"
                params.append(limit)
            cursor.execute(sql, params)
            models = cursor.fetchall()
            self.logger.info("Goonet missing grades sync: %s models to process", len(models))
            total_grades = 0
            errors: list[dict] = []
            for index, (maker_cd, car_cd, model_name, car_nm) in enumerate(models, start=1):
                label = model_name or car_nm or car_cd
                try:
                    self.logger.info("Goonet missing grades sync model %s/%s: %s (%s)", index, len(models), label, car_cd)
                    grades = self.fetch_master_grades(str(maker_cd), str(car_cd))
                    if grades:
                        execute_values(
                            cursor,
                            grade_sql,
                            [
                                (
                                    r["maker_cd"],
                                    r["car_cd"],
                                    r["grade_cd"],
                                    r["grade_nm"],
                                    r["grade_name"],
                                    r["listing_count"],
                                    Json(r["raw"]),
                                )
                                for r in grades
                            ],
                            page_size=100,
                        )
                        conn.commit()
                    total_grades += len(grades)
                except Exception as exc:
                    conn.rollback()
                    errors.append({"maker_cd": str(maker_cd), "car_cd": str(car_cd), "error": str(exc)})
            return {
                "models_count": len(models),
                "grades_count": total_grades,
                "errors_count": len(errors),
                "errors": errors,
            }
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _scrape_single_detail_http(self, *, url: str, listing_id: str | None, tracking_mode: bool) -> dict:
        listing_id_final = (listing_id or self._extract_listing_id(url)).strip()
        html = self._fetch_detail_http(url)
        lowered = html.lower()
        if any(token in lowered for token in ("page not found", "already been sold", "this car has already been sold")):
            if tracking_mode:
                return self._build_sold_result(url, listing_id_final)
            raise RuntimeError("Goonet detail page unavailable.")
        data, json_ld_items = self._extract_detail_html(html, url)
        return self._build_detail_result(
            url=url,
            listing_id_final=listing_id_final,
            data=data,
            json_ld_items=json_ld_items,
            tracking_mode=tracking_mode,
        )

    def _build_sold_result(self, url: str, listing_id: str) -> dict:
        keys = (
            "stock_number",
            "condition",
            "brand",
            "model",
            "variant",
            "information_ads",
            "information_ads_date",
            "location",
            "year",
            "mileage",
            "price_jpy",
            "price_usd",
            "transmission",
            "seat_capacity",
            "engine_cc",
            "fuel_type",
            "series",
            "type",
            "images",
            "video_url",
            "whatsapp_number",
            "contact_seller",
            "seller_name",
            "media",
            "overview",
            "ratings",
            "certification",
            "specifications",
            "equipment",
            "hidden_codes",
            "raw_json_ld",
        )
        return {"listing_id": listing_id, "listing_url": url, **{key: None for key in keys}, "status": "sold", "error": None}

    def _build_error_result(self, url: str, listing_id: str | None = None, error_message: str | None = None) -> dict:
        result = self._build_sold_result(url, listing_id or "")
        result["status"] = "active"
        result["error"] = "error"
        result["error_message"] = error_message
        return result

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

        columns = (
            "listing_id",
            "stock_number",
            "listing_url",
            "condition",
            "brand",
            "model",
            "variant",
            "information_ads",
            "information_ads_date",
            "location",
            "year",
            "mileage",
            "price_jpy",
            "price_usd",
            "transmission",
            "seat_capacity",
            "engine_cc",
            "fuel_type",
            "series",
            "type",
            "images",
            "video_url",
            "whatsapp_number",
            "contact_seller",
            "seller_name",
            "status",
        )
        status_update_sql = (
            "status = COALESCE(EXCLUDED.status, cars_scrap_goonet.status)"
            if allow_revive_sold
            else """status = CASE
            WHEN cars_scrap_goonet.status = 'sold' THEN cars_scrap_goonet.status
            ELSE COALESCE(EXCLUDED.status, cars_scrap_goonet.status)
          END"""
        )
        upsert_sql = f"""
        INSERT INTO cars_scrap_goonet ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          stock_number = COALESCE(EXCLUDED.stock_number, cars_scrap_goonet.stock_number),
          listing_url = EXCLUDED.listing_url,
          condition = COALESCE(EXCLUDED.condition, cars_scrap_goonet.condition),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_goonet.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_goonet.model),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_goonet.variant),
          information_ads = COALESCE(EXCLUDED.information_ads, cars_scrap_goonet.information_ads),
          information_ads_date = COALESCE(EXCLUDED.information_ads_date, cars_scrap_goonet.information_ads_date),
          location = COALESCE(EXCLUDED.location, cars_scrap_goonet.location),
          year = COALESCE(EXCLUDED.year, cars_scrap_goonet.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_goonet.mileage),
          price_jpy = COALESCE(EXCLUDED.price_jpy, cars_scrap_goonet.price_jpy),
          price_usd = COALESCE(EXCLUDED.price_usd, cars_scrap_goonet.price_usd),
          transmission = COALESCE(EXCLUDED.transmission, cars_scrap_goonet.transmission),
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_goonet.seat_capacity),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_goonet.engine_cc),
          fuel_type = COALESCE(EXCLUDED.fuel_type, cars_scrap_goonet.fuel_type),
          series = COALESCE(EXCLUDED.series, cars_scrap_goonet.series),
          type = COALESCE(EXCLUDED.type, cars_scrap_goonet.type),
          images = COALESCE(EXCLUDED.images, cars_scrap_goonet.images),
          video_url = COALESCE(EXCLUDED.video_url, cars_scrap_goonet.video_url),
          whatsapp_number = COALESCE(EXCLUDED.whatsapp_number, cars_scrap_goonet.whatsapp_number),
          contact_seller = COALESCE(EXCLUDED.contact_seller, cars_scrap_goonet.contact_seller),
          seller_name = COALESCE(EXCLUDED.seller_name, cars_scrap_goonet.seller_name),
          {status_update_sql},
          version = COALESCE(cars_scrap_goonet.version, 0) + 1,
          last_scraped_at = NOW()
        """
        details_columns = (
            "listing_id",
            "listing_url",
            "media",
            "overview",
            "ratings",
            "certification",
            "specifications",
            "equipment",
            "hidden_codes",
            "raw_json_ld",
        )
        details_upsert_sql = f"""
        INSERT INTO cars_scrap_goonet_details ({", ".join(details_columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          media = COALESCE(EXCLUDED.media, cars_scrap_goonet_details.media),
          overview = COALESCE(EXCLUDED.overview, cars_scrap_goonet_details.overview),
          ratings = COALESCE(EXCLUDED.ratings, cars_scrap_goonet_details.ratings),
          certification = COALESCE(EXCLUDED.certification, cars_scrap_goonet_details.certification),
          specifications = COALESCE(EXCLUDED.specifications, cars_scrap_goonet_details.specifications),
          equipment = COALESCE(EXCLUDED.equipment, cars_scrap_goonet_details.equipment),
          hidden_codes = COALESCE(EXCLUDED.hidden_codes, cars_scrap_goonet_details.hidden_codes),
          raw_json_ld = COALESCE(EXCLUDED.raw_json_ld, cars_scrap_goonet_details.raw_json_ld)
        """
        sold_upsert_sql = """
        UPDATE cars_scrap_goonet
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
                deduped = {str(row[0]): row for row in rows if row[0]}
                rows[:] = list(deduped.values())
            if details_rows:
                deduped_details = {str(row[0]): row for row in details_rows if row[0]}
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

        def handle_result(result: dict) -> None:
            row_listing_id = result.get("listing_id")
            if row_listing_id and not result.get("error"):
                status = str(result.get("status") or "active")
                if status != "active":
                    if listing_id_by_url:
                        sold_rows.append((result.get("listing_url"), str(row_listing_id)))
                        if len(sold_rows) >= batch_size:
                            flush_sold_rows()
                    return
                if require_brand_model and (not result.get("brand") or not result.get("model")):
                    result["skipped_upsert"] = True
                    result["skip_reason"] = "missing_brand_or_model"
                    return
                rows.append(tuple(result.get(column) for column in columns))
                details_rows.append(
                    (
                        str(row_listing_id),
                        result.get("listing_url"),
                        Json(result.get("media")) if result.get("media") else None,
                        Json(result.get("overview")) if result.get("overview") else None,
                        Json(result.get("ratings")) if result.get("ratings") else None,
                        Json(result.get("certification")) if result.get("certification") else None,
                        Json(result.get("specifications")) if result.get("specifications") else None,
                        Json(result.get("equipment")) if result.get("equipment") else None,
                        Json(result.get("hidden_codes")) if result.get("hidden_codes") else None,
                        Json(result.get("raw_json_ld")) if result.get("raw_json_ld") else None,
                    )
                )
                if len(rows) >= batch_size or len(details_rows) >= batch_size:
                    flush_rows()

        def run_http() -> list[dict]:
            results: list[dict] = []
            for idx, url in enumerate(links, start=1):
                if self.stop_flag:
                    break
                url = str(url or "").strip()
                if not url:
                    continue
                self.logger.info("Detail %s/%s: %s", idx, len(links), url)
                listing_id = listing_id_by_url.get(url) if listing_id_by_url else None
                try:
                    result = self._scrape_single_detail_http(
                        url=url,
                        listing_id=listing_id,
                        tracking_mode=bool(listing_id_by_url),
                    )
                except Exception as exc:
                    self.logger.warning("Error while scraping %s: %s", url, exc)
                    result = self._build_error_result(url, listing_id=listing_id, error_message=str(exc))
                results.append(result)
                handle_result(result)
            flush_rows()
            flush_sold_rows()
            return results

        def run_browser() -> list[dict]:
            from botasaurus.browser import Driver, browser

            cfg = load_botasaurus_config(service_name="goonet")
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
                    url = str(url or "").strip()
                    if not url:
                        continue
                    self.logger.info("Detail %s/%s: %s", idx, len(data["links"]), url)
                    listing_id = listing_id_by_url.get(url) if listing_id_by_url else None
                    try:
                        result = self._scrape_single_detail(
                            driver,
                            url=url,
                            cfg=cfg,
                            listing_id=listing_id,
                            tracking_mode=bool(listing_id_by_url),
                        )
                    except Exception as exc:
                        self.logger.warning("Error while scraping %s: %s", url, exc)
                        result = self._build_error_result(url, listing_id=listing_id, error_message=str(exc))
                    results.append(result)
                    handle_result(result)
                flush_rows()
                flush_sold_rows()
                return results

            return run({"links": links}) or []

        try:
            if env_bool("GOONET_DETAIL_USE_BROWSER", False):
                return run_browser()
            return run_http()
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
        max_attempts = self._env_int("GOONET_RUN_MAX_ATTEMPTS", 2, minimum=1)
        retry_wait_seconds = self._env_float("GOONET_RUN_RETRY_WAIT_SECONDS", 5.0, minimum=0.0)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self.scrape_listing_details(
                    original_urls,
                    listing_id_by_url=listing_id_by_url,
                    allow_revive_sold=allow_revive_sold,
                    require_brand_model=require_brand_model,
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                self.logger.warning("Goonet run attempt %s/%s failed: %s", attempt, max_attempts, exc)
                time.sleep(retry_wait_seconds)
                reset_cached_proxy()
        if last_exc:
            raise last_exc
        return []

    def to_jsonable(self, value):
        return json.loads(json.dumps(value, default=str))
