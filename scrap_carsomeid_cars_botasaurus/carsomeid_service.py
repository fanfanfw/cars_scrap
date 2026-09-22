from __future__ import annotations

import os
import re
import time
from hashlib import sha1
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from dotenv import load_dotenv
import requests
from psycopg2.extras import Json, execute_values

from cars_scrap_common.botasaurus_utils import load_botasaurus_config
from cars_scrap_common.env_utils import env_bool
from cars_scrap_common.logging_utils import setup_logger
from cars_scrap_common.proxy_utils import mask_proxy_url, probe_public_ip, reset_cached_proxy
from cars_scrap_common.shared_base_service import SharedBaseService
from scrap_carsomeid_cars_botasaurus.database import get_connection

load_dotenv(override=True)

DEFAULT_LISTING_URL = "https://www.carsome.id/beli-mobil-bekas?sortKey=2"
BASE_URL = "https://www.carsome.id"
CARSOMEID_DETAIL_API_URL = "https://www.carsome.id/website/CarList/CarListDetail/v2"

# CARSOME-TRACK-02: explicit status enum. Anything else is unknown -> error/DB untouched.
CARSOMEID_ACTIVE_STATUS_NAMES = frozenset({"listed", "prelisted"})
CARSOMEID_SOLD_STATUS_NAMES = frozenset({"unlisted"})
CARSOMEID_SOLD_STATUS_MARKERS = ("sold", "unavailable")


def normalize_carsomeid_status_token(value: object) -> str:
    """Normalize a CarsomeID status field to a comparable token.

    Lowercases, strips and removes any non-alphanumeric characters so variants
    like ``PreListed``, ``Pre-Listed`` and ``pre listed`` compare equal.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def classify_carsomeid_status(*, sale_status_name: object, car_status_label: object) -> str:
    """Map CarsomeID detail status fields to ``active`` or ``sold``.

    ``Listed``/``PreListed`` are active and ``Unlisted`` is sold. An explicit
    ``sold``/``unavailable`` status name/label is also sold. An empty or unknown
    enum raises ``RuntimeError`` so the caller records an error result and never
    silently marks an unknown listing active (DB untouched).
    """
    name = normalize_carsomeid_status_token(sale_status_name)
    label = normalize_carsomeid_status_token(car_status_label)

    # Explicit sold status name (e.g. "Sold"/"Unavailable") is always sold.
    if any(marker in name for marker in CARSOMEID_SOLD_STATUS_MARKERS):
        return "sold"
    # Unknown non-empty enum must never be silently classified as active.
    if name and name not in CARSOMEID_ACTIVE_STATUS_NAMES and name not in CARSOMEID_SOLD_STATUS_NAMES:
        raise RuntimeError(f"CarsomeID detail returned unknown saleStatusName: {sale_status_name!r}")
    if name in CARSOMEID_SOLD_STATUS_NAMES:
        return "sold"
    # Explicit sold/unavailable label is sold when the status name is absent/known.
    if any(marker in label for marker in CARSOMEID_SOLD_STATUS_MARKERS):
        return "sold"
    if name in CARSOMEID_ACTIVE_STATUS_NAMES:
        return "active"
    raise RuntimeError(
        "CarsomeID detail returned empty/unknown status "
        f"(saleStatusName={sale_status_name!r}, carStatusLabel={car_status_label!r})"
    )


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
        proxy_enabled = os.getenv("CARSOMEID_PROXY_ENABLED")
        if proxy_enabled is None:
            return load_botasaurus_config(service_name="carsomeid")
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
            return slug.lower()
        return sha1(url.encode("utf-8")).hexdigest()[:16]

    def _listing_id_to_car_no(self, listing_id: str) -> str:
        return str(listing_id or "").strip().upper()

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

    def _page_has_no_listing_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const text = ((document.body && document.body.innerText) || '').toLowerCase();
                    const hasCards = !!document.querySelector(
                      '.list-card__item a.mod-b-card__header[href], a.mod-b-card__header[href]'
                    );
                    return !hasCards && (
                      text.includes('tidak ada lagi mobil') ||
                      text.includes('tidak menemukan apa yang anda cari') ||
                      text.includes('tidak ada mobil')
                    );
                    """
                )
            )
        except Exception:
            return False

    def _url_for_page(self, url: str, page_no: int) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["pageNo"] = [str(page_no)]
        if "sortKey" not in query:
            query["sortKey"] = ["2"]
        encoded = urlencode(query, doseq=True)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, encoded, parsed.fragment))

    def _parse_int(self, value: object) -> int | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("-") and text[1:].isdigit():
            return int(text)
        digits = re.findall(r"\d+", text)
        if not digits:
            return None
        try:
            return int("".join(digits))
        except Exception:
            return None

    def _first_text(self, *values: object) -> str | None:
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    def _collect_images(self, car: dict) -> list[str] | None:
        images: list[str] = []

        def add(value: object) -> None:
            if isinstance(value, str):
                url = value.strip()
                if url.startswith(("http://", "https://")):
                    images.append(url)
                return
            if isinstance(value, dict):
                for key in ("url", "image", "imageUrl", "src"):
                    add(value.get(key))
                return
            if isinstance(value, list):
                for item in value:
                    add(item)

        add(car.get("image"))
        for key in ("innerImage", "outerImage", "imagesInner", "imagesOuter", "carInnerImage", "carOutImage"):
            add(car.get(key))

        deduped = list(dict.fromkeys(images))
        return deduped or None

    def _status_from_detail(self, payload: dict) -> str:
        car = ((payload or {}).get("data") or {}).get("car") or {}
        return classify_carsomeid_status(
            sale_status_name=car.get("saleStatusName"),
            car_status_label=car.get("carStatusLabel"),
        )

    def _extract_detail_fields(self, *, url: str, payload: dict) -> dict:
        listing_id = self._extract_listing_id(url)
        car = ((payload or {}).get("data") or {}).get("car") or {}
        all_price = car.get("allPrice") or {}
        images = self._collect_images(car)
        image = self._first_text(car.get("image"), *(images or []))
        price = self._parse_int(all_price.get("price") or car.get("installmentPrice") or car.get("expSellingPrice"))

        return {
            "listing_id": listing_id,
            "listing_url": url,
            "car_no": self._first_text(car.get("carNo"), self._listing_id_to_car_no(listing_id)),
            "reg_no": self._first_text(car.get("licensePlate"), car.get("regNo")),
            "car_id": self._parse_int(car.get("carId") or car.get("id")),
            "image": image,
            "brand": self._first_text(car.get("brandName")),
            "model": self._first_text(car.get("modelName")),
            "model_group": "NO MODEL GROUP",
            "variant": self._first_text(car.get("carVariant")),
            "year": self._parse_int(car.get("carYear")),
            "mileage": self._parse_int(car.get("carMileage")),
            "price": price,
            "cash_price": self._parse_int(
                all_price.get("originalPrice") or all_price.get("expSellingPrice") or car.get("expSellingPrice")
            ),
            "installment_price": self._parse_int(
                all_price.get("installmentPrice") or car.get("installmentPrice") or all_price.get("price")
            ),
            "monthly_payment": self._parse_int(all_price.get("monthPrice") or car.get("monthPayment")),
            "location": self._first_text(car.get("location"), car.get("locationName"), car.get("storeName")),
            "transmission": self._first_text(car.get("transmissionLabel")),
            "engine_cc": self._first_text(car.get("carEngine")),
            "fuel_type": self._first_text(car.get("fuelLabel")),
            "seat_capacity": self._first_text(car.get("seatSize")),
            "car_type": self._first_text(car.get("carTypeName"), car.get("carTypeKey")),
            "color": self._first_text(car.get("colorLabel"), car.get("colorName")),
            "images": images,
            "status": self._status_from_detail(payload),
            "source": "carsomeid",
            "raw_detail": payload,
            "error": None,
        }

    def _build_error_result(self, url: str, *, error_message: str) -> dict:
        listing_id = self._extract_listing_id(url)
        return {
            "listing_id": listing_id,
            "listing_url": url,
            "car_no": self._listing_id_to_car_no(listing_id),
            "status": "active",
            "source": "carsomeid",
            "raw_detail": None,
            "error": "error",
            "error_message": error_message,
        }

    def _is_retryable_detail_error(self, exc: Exception) -> bool:
        """CARSOME-TRACK-03: only Timeout/ConnectionError, HTTP 429 and HTTP 5xx retry."""
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, requests.HTTPError):
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code is not None and (status_code == 429 or 500 <= status_code < 600):
                return True
        return False

    def fetch_detail_api(self, url: str) -> dict:
        listing_id = self._extract_listing_id(url)
        car_no = self._listing_id_to_car_no(listing_id)
        timeout = self._carsomeid_env_float("DETAIL_API_TIMEOUT_SECONDS", 30.0, minimum=1.0)
        max_attempts = self._carsomeid_env_int("DETAIL_MAX_ATTEMPTS", 2, minimum=1)
        retry_wait_seconds = self._carsomeid_env_float("DETAIL_RETRY_WAIT_SECONDS", 3.0, minimum=0.0)
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "country": "ID",
            "origin": BASE_URL,
            "referer": url,
            "user-agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
            "x-language": "id",
            "x-platform": "web",
        }
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    CARSOMEID_DETAIL_API_URL,
                    headers=headers,
                    json={"recommendCount": 6, "carNo": car_no, "token": ""},
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("CarsomeID detail API returned non-object JSON")
                if payload.get("code") != "0000" or not (((payload.get("data") or {}).get("car") or {})):
                    raise RuntimeError(
                        f"CarsomeID detail API returned invalid payload for {car_no}: {payload.get('msg')}"
                    )
                return payload
            except Exception as exc:
                if attempt >= max_attempts or not self._is_retryable_detail_error(exc):
                    raise
                self.logger.warning(
                    "CarsomeID detail retry %s/%s for %s after error: %s",
                    attempt,
                    max_attempts,
                    car_no,
                    exc,
                )
                if retry_wait_seconds:
                    time.sleep(retry_wait_seconds)
        raise RuntimeError(f"CarsomeID detail API retry exhausted for {car_no}")

    def _load_listing_urls_by_pages(self, driver, *, url: str, cfg, max_pages: int, all_pages: bool) -> list[str]:
        collected: list[str] = []
        page_pause = self._carsomeid_env_float("LISTING_PAGE_PAUSE_SECONDS", 1.0, minimum=0.0)
        page_limit = max_pages
        if all_pages:
            page_limit = self._carsomeid_env_int("ALL_PAGES_MAX", 200, minimum=1)

        for page_no in range(1, page_limit + 1):
            page_url = self._url_for_page(url, page_no)
            self.logger.info("Navigating CarsomeID listing page %s/%s: %s", page_no, page_limit, page_url)
            self._navigate_botasaurus(driver, page_url)
            time.sleep(max(0.0, cfg.listing_wait_seconds))
            self._dismiss_popups_botasaurus(driver)

            urls = self._extract_listing_urls_botasaurus(driver)
            if not urls:
                if self._page_has_no_listing_botasaurus(driver):
                    self.logger.info("CarsomeID listing page %s has no listings; stopping pagination.", page_no)
                    break
                self.logger.warning("CarsomeID listing page %s returned no listing links; stopping pagination.", page_no)
                break

            before = len(collected)
            collected = list(dict.fromkeys([*collected, *urls]))
            self.logger.info("CarsomeID listing page %s collected %s links (%s new).", page_no, len(urls), len(collected) - before)

            if page_no < page_limit and page_pause:
                time.sleep(page_pause)

        if all_pages and len(collected) > 0 and page_no >= page_limit:
            self.logger.warning(
                "CarsomeID all-pages reached safety cap of %s page(s). Increase CARSOMEID_ALL_PAGES_MAX if needed.",
                page_limit,
            )

        return collected

    def scrape_all_from_main(self, start_url: str | None = None, max_pages: int = 1, *, all_pages: bool = False) -> list[str]:
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
            return self._load_listing_urls_by_pages(
                driver,
                url=data["url"],
                cfg=cfg,
                max_pages=int(data["max_pages"]),
                all_pages=bool(data.get("all_pages")),
            )

        links = run({"url": url, "max_pages": max_pages, "all_pages": all_pages}) or []
        self.logger.info("Collected %s links.", len(links))
        return links

    def scrape_listing_details(self, links: list[str]) -> list[dict]:
        batch_size = self._env_int("DB_BATCH_SIZE", 100, minimum=1)
        columns = (
            "listing_id",
            "listing_url",
            "car_no",
            "reg_no",
            "car_id",
            "image",
            "brand",
            "model",
            "model_group",
            "variant",
            "year",
            "mileage",
            "price",
            "cash_price",
            "installment_price",
            "monthly_payment",
            "location",
            "transmission",
            "engine_cc",
            "fuel_type",
            "seat_capacity",
            "car_type",
            "color",
            "images",
            "status",
            "source",
        )
        upsert_sql = f"""
        INSERT INTO cars_scrap_carsomeid ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          car_no = COALESCE(EXCLUDED.car_no, cars_scrap_carsomeid.car_no),
          reg_no = COALESCE(EXCLUDED.reg_no, cars_scrap_carsomeid.reg_no),
          car_id = COALESCE(EXCLUDED.car_id, cars_scrap_carsomeid.car_id),
          image = COALESCE(EXCLUDED.image, cars_scrap_carsomeid.image),
          brand = COALESCE(EXCLUDED.brand, cars_scrap_carsomeid.brand),
          model = COALESCE(EXCLUDED.model, cars_scrap_carsomeid.model),
          model_group = COALESCE(EXCLUDED.model_group, cars_scrap_carsomeid.model_group),
          variant = COALESCE(EXCLUDED.variant, cars_scrap_carsomeid.variant),
          year = COALESCE(EXCLUDED.year, cars_scrap_carsomeid.year),
          mileage = COALESCE(EXCLUDED.mileage, cars_scrap_carsomeid.mileage),
          price = COALESCE(EXCLUDED.price, cars_scrap_carsomeid.price),
          cash_price = COALESCE(EXCLUDED.cash_price, cars_scrap_carsomeid.cash_price),
          installment_price = COALESCE(EXCLUDED.installment_price, cars_scrap_carsomeid.installment_price),
          monthly_payment = COALESCE(EXCLUDED.monthly_payment, cars_scrap_carsomeid.monthly_payment),
          location = COALESCE(EXCLUDED.location, cars_scrap_carsomeid.location),
          transmission = COALESCE(EXCLUDED.transmission, cars_scrap_carsomeid.transmission),
          engine_cc = COALESCE(EXCLUDED.engine_cc, cars_scrap_carsomeid.engine_cc),
          fuel_type = COALESCE(EXCLUDED.fuel_type, cars_scrap_carsomeid.fuel_type),
          seat_capacity = COALESCE(EXCLUDED.seat_capacity, cars_scrap_carsomeid.seat_capacity),
          car_type = COALESCE(EXCLUDED.car_type, cars_scrap_carsomeid.car_type),
          color = COALESCE(EXCLUDED.color, cars_scrap_carsomeid.color),
          images = COALESCE(EXCLUDED.images, cars_scrap_carsomeid.images),
          status = COALESCE(EXCLUDED.status, cars_scrap_carsomeid.status),
          source = COALESCE(EXCLUDED.source, cars_scrap_carsomeid.source),
          version = COALESCE(cars_scrap_carsomeid.version, 0) + 1,
          last_scraped_at = NOW()
        """

        details_upsert_sql = """
        INSERT INTO cars_scrap_carsomeid_details (listing_id, listing_url, raw_detail)
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          raw_detail = COALESCE(EXCLUDED.raw_detail, cars_scrap_carsomeid_details.raw_detail),
          last_scraped_at = NOW()
        """

        conn = get_connection()
        cursor = conn.cursor()
        rows: list[tuple] = []
        details_rows: list[tuple] = []
        results: list[dict] = []

        def flush_rows() -> None:
            if not rows and not details_rows:
                return
            try:
                if rows:
                    deduped_rows = {str(row[0]): row for row in rows if row and row[0]}
                    execute_values(cursor, upsert_sql, list(deduped_rows.values()), page_size=100)
                if details_rows:
                    deduped_details = {str(row[0]): row for row in details_rows if row and row[0]}
                    execute_values(cursor, details_upsert_sql, list(deduped_details.values()), page_size=100)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            rows.clear()
            details_rows.clear()

        try:
            for idx, url in enumerate([str(u or "").strip() for u in links if str(u or "").strip()], start=1):
                if self.stop_flag:
                    break
                self.logger.info("CarsomeID detail %s/%s: %s", idx, len(links), url)
                try:
                    payload = self.fetch_detail_api(url)
                    result = self._extract_detail_fields(url=url, payload=payload)
                except Exception as exc:
                    self.logger.warning("Error while scraping CarsomeID %s: %s", url, exc)
                    result = self._build_error_result(url, error_message=str(exc))

                results.append(result)
                if result.get("error"):
                    continue

                rows.append(tuple(result.get(column) for column in columns))
                details_rows.append(
                    (
                        result.get("listing_id"),
                        result.get("listing_url"),
                        Json(result.get("raw_detail")) if result.get("raw_detail") else None,
                    )
                )

                if len(rows) >= batch_size or len(details_rows) >= batch_size:
                    flush_rows()

            flush_rows()
            return results
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
