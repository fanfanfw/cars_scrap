from __future__ import annotations

import os
import re
import time
from hashlib import sha1
from urllib.parse import urlparse

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from cars_scrap_common.botasaurus_utils import load_botasaurus_config, raise_if_cloudflare_challenge
from cars_scrap_common.logging_utils import setup_logger
from scrap_carlist_cars_botasaurus.database import get_connection

load_dotenv(override=True)


class CarlistCarsService:
    def __init__(self):
        self.logger = setup_logger("scrape_carlist_cars")
        self.listing_url = os.getenv("CARLIST_CARS_LISTING_URL", "").strip()

    def close(self) -> None:
        return

    def _extract_listing_id(self, url: str) -> str:
        match = re.search(r"(?:-|/)(\d{5,})(?:[/?#.]|$)", url)
        if match:
            return match.group(1)
        return sha1(url.encode("utf-8")).hexdigest()[:16]

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

    def scrape_listing_details(self, links: list[str]) -> list[dict]:
        batch_size = int(os.getenv("DB_BATCH_SIZE", "100"))
        if batch_size < 1:
            batch_size = 1

        rows: list[tuple] = []
        results: list[dict] = []
        for url in links:
            url = (url or "").strip()
            if not url:
                continue
            listing_id = self._extract_listing_id(url)
            rows.append((listing_id, url, "active"))
            results.append({"listing_id": listing_id, "listing_url": url, "status": "active"})

        if not rows:
            return []

        columns = ("listing_id", "listing_url", "status")
        upsert_sql = f"""
        INSERT INTO car_scrap_carlist ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (listing_id) DO UPDATE SET
          listing_url = EXCLUDED.listing_url,
          status = COALESCE(EXCLUDED.status, car_scrap_carlist.status),
          version = car_scrap_carlist.version + 1,
          last_scraped_at = NOW()
        """

        conn = None
        try:
            conn = get_connection()
            with conn.cursor() as cursor:
                for i in range(0, len(rows), batch_size):
                    chunk = rows[i : i + batch_size]
                    execute_values(cursor, upsert_sql, chunk, page_size=len(chunk))
            conn.commit()
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

        return results
