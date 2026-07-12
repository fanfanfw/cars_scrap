from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from cars_scrap_common.logging_utils import setup_logger


BASE_CATALOG_URL = "https://www.goo-net-exchange.com/catalog/"


class CatalogInterrupted(KeyboardInterrupt):
    pass


class GoonetCatalogService:
    def __init__(self) -> None:
        self.logger = setup_logger("scrape_goonet_catalog")

    def fetch_html(self, url: str) -> str:
        timeout = float(os.getenv("GOONET_CATALOG_TIMEOUT_SECONDS") or 15)
        attempts = max(int(os.getenv("GOONET_CATALOG_MAX_ATTEMPTS") or 2), 1)
        retry_wait = max(float(os.getenv("GOONET_CATALOG_RETRY_WAIT_SECONDS") or 3), 0.0)
        self.logger.info("Fetching catalog page: %s", url)
        last_error: Exception | None = None
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
        for attempt in range(1, attempts + 1):
            self.logger.info("Fetch attempt %s/%s timeout=%.1fs: %s", attempt, attempts, timeout, url)
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                if 200 <= response.status_code < 300:
                    return response.text
                last_error = RuntimeError(f"GET {url} failed with HTTP {response.status_code}")
                self.logger.warning("Fetch attempt %s/%s failed: %s", attempt, attempts, last_error)
                if response.status_code < 500 and response.status_code != 429:
                    break
            except requests.RequestException as exc:
                last_error = exc
                self.logger.warning("Fetch attempt %s/%s failed: %s", attempt, attempts, exc)
            if attempt < attempts and retry_wait > 0:
                time.sleep(retry_wait)
        raise RuntimeError(f"Unable to fetch {url}: {last_error}")

    def clean_text(self, value: object | None) -> str | None:
        if value is None:
            return None
        if hasattr(value, "get_text"):
            value = value.get_text(" ")
        text = re.sub(r"\s+", " ", str(value)).strip()
        return text or None

    def absolute_url(self, href: str, base_url: str) -> str:
        return urljoin(base_url, str(href or "").strip())

    def extract_make_slug(self, url: str) -> str | None:
        parts = self._catalog_path_parts(url)
        if len(parts) == 1 and "__" not in parts[0]:
            return parts[0]
        return None

    def extract_model_slug(self, url: str) -> str | None:
        parts = self._catalog_path_parts(url)
        if len(parts) == 1 and "__" in parts[0]:
            return parts[0]
        return None

    def extract_catalog_spec_id(self, url: str) -> str | None:
        parts = self._catalog_path_parts(url)
        if len(parts) == 2 and "__" in parts[0] and re.fullmatch(r"\d+", parts[1]):
            return parts[1]
        return None

    def stable_json_hash(self, value: object) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _catalog_path_parts(self, url: str) -> list[str]:
        path = urlparse(str(url or "")).path.strip("/")
        parts = [part for part in path.split("/") if part]
        if parts and parts[0].lower() == "catalog":
            return parts[1:]
        return parts

    def parse_makes(self, html: str, base_url: str = BASE_CATALOG_URL) -> list[dict]:
        soup = BeautifulSoup(html or "", "html.parser")
        section = soup.select_one(".section-makes .content-makes") or soup
        makes: list[dict] = []
        seen: set[str] = set()
        for box in section.select(".makes-box"):
            country = self.clean_text(box.select_one(".region"))
            for link in box.select('a[href^="/catalog/"]'):
                make_url = self.absolute_url(link.get("href", ""), base_url)
                make_slug = self.extract_make_slug(make_url)
                make_name = self.clean_text(link)
                if not make_slug or not make_name or make_slug in seen:
                    continue
                seen.add(make_slug)
                makes.append(
                    {
                        "make_slug": make_slug,
                        "make_name": make_name,
                        "country": country,
                        "make_url": make_url,
                        "raw": {"href": link.get("href"), "text": make_name, "country": country},
                    }
                )
        return makes

    def parse_models(self, html: str, make: dict) -> list[dict]:
        soup = BeautifulSoup(html or "", "html.parser")
        base_url = make.get("make_url") or BASE_CATALOG_URL
        make_slug = make.get("make_slug")
        models: list[dict] = []
        seen: set[str] = set()
        for link in soup.select('.main-content a.name-car[href^="/catalog/"]'):
            model_url = self.absolute_url(link.get("href", ""), base_url)
            model_slug = self.extract_model_slug(model_url)
            model_name = self.clean_text(link)
            if not model_slug or not model_name or model_slug in seen:
                continue
            if make_slug and not model_slug.startswith(f"{make_slug}__"):
                continue
            parent = link.find_parent("li")
            production_period = self.clean_text(parent.select_one(".desc-car")) if parent else None
            production_start, production_end = self._split_production_period(production_period)
            seen.add(model_slug)
            models.append(
                {
                    "model_slug": model_slug,
                    "make_slug": make_slug or model_slug.split("__", 1)[0],
                    "model_name": model_name,
                    "production_period": production_period,
                    "production_start": production_start,
                    "production_end": production_end,
                    "model_url": model_url,
                    "raw": {"href": link.get("href"), "text": model_name, "production_period": production_period},
                }
            )
        return models

    def _split_production_period(self, production_period: str | None) -> tuple[str | None, str | None]:
        if not production_period or " - " not in production_period:
            return production_period, None
        start, end = production_period.split(" - ", 1)
        return self.clean_text(start), self.clean_text(end)

    def parse_model_specs(self, html: str, model: dict) -> list[dict]:
        soup = BeautifulSoup(html or "", "html.parser")
        model_url = model.get("model_url") or BASE_CATALOG_URL
        model_slug = model.get("model_slug") or self.extract_model_slug(model_url)
        make_slug = model.get("make_slug") or (model_slug.split("__", 1)[0] if model_slug and "__" in model_slug else None)
        make_name = model.get("make_name") or make_slug
        model_name = model.get("model_name") or self._model_name_from_page(soup, make_name, model_slug)
        specs: list[dict] = []
        seen: set[str] = set()
        for block in soup.select(".list-car"):
            sale_title = self.clean_text(block.select_one(".list-intro .title"))
            release_month = self._release_month(sale_title)
            for row in block.select("table tbody tr"):
                link = row.select_one('a[href^="/catalog/"]')
                if not link:
                    continue
                spec_url = self.absolute_url(link.get("href", ""), model_url)
                catalog_spec_id = self.extract_catalog_spec_id(spec_url)
                if not catalog_spec_id or catalog_spec_id in seen:
                    continue
                cells = row.select("td")
                transmission, drive = self._split_transmission_drive(self.clean_text(cells[5]) if len(cells) > 5 else None)
                seen.add(catalog_spec_id)
                specs.append(
                    {
                        "catalog_spec_id": catalog_spec_id,
                        "make_slug": make_slug,
                        "model_slug": model_slug,
                        "make_name": make_name,
                        "model_name": model_name,
                        "variant_name": self.clean_text(link),
                        "release_month": release_month,
                        "sale_title": sale_title,
                        "spec_url": spec_url,
                        "model_code": self.clean_text(cells[2]) if len(cells) > 2 else None,
                        "engine": self.clean_text(cells[3]) if len(cells) > 3 else None,
                        "doors": self._parse_int(self.clean_text(cells[4]) if len(cells) > 4 else None),
                        "transmission": transmission,
                        "drive": drive,
                        "dimension": self.clean_text(cells[6]) if len(cells) > 6 else None,
                        "weight": self.clean_text(cells[7]) if len(cells) > 7 else None,
                        "msrp": self.clean_text(cells[8]) if len(cells) > 8 else None,
                        "raw_list_row": {
                            "href": link.get("href"),
                            "release_title": sale_title,
                            "cells": [self.clean_text(cell) for cell in cells],
                        },
                    }
                )
        return specs

    def _model_name_from_page(self, soup: BeautifulSoup, make_name: str | None, model_slug: str | None) -> str | None:
        title = self.clean_text(soup.select_one("h1"))
        if title and make_name and title.upper().startswith(str(make_name).upper() + " "):
            return title[len(str(make_name)) :].strip()
        if title:
            return title
        if model_slug and "__" in model_slug:
            return model_slug.split("__", 1)[1].replace("_", " ")
        return None

    def _release_month(self, sale_title: str | None) -> str | None:
        match = re.search(r"Sale in\s+(\d{1,2}/\d{4})", sale_title or "")
        return match.group(1) if match else None

    def _split_transmission_drive(self, value: str | None) -> tuple[str | None, str | None]:
        parts = (value or "").split()
        if len(parts) < 2:
            return (parts[0], None) if parts else (None, None)
        return " ".join(parts[:-1]), parts[-1]

    def _parse_int(self, value: str | None) -> int | None:
        match = re.search(r"\d+", value or "")
        return int(match.group(0)) if match else None

    def parse_spec_detail(self, html: str, spec: dict) -> dict:
        soup = BeautifulSoup(html or "", "html.parser")
        spec_url = spec.get("spec_url") or spec.get("url") or BASE_CATALOG_URL
        catalog_spec_id = spec.get("catalog_spec_id") or self.extract_catalog_spec_id(spec_url)
        path_parts = self._catalog_path_parts(spec_url)
        model_slug = spec.get("model_slug") or self.extract_model_slug(spec_url) or (path_parts[0] if path_parts and "__" in path_parts[0] else None)
        make_slug = spec.get("make_slug") or (model_slug.split("__", 1)[0] if model_slug and "__" in model_slug else None)
        make_name = spec.get("make_name") or self.clean_text(soup.select_one(".detail-right .type-car")) or make_slug
        name_parts = self._detail_name_parts(soup, make_name)
        model_name = spec.get("model_name") or name_parts[0] or self._model_name_from_slug(model_slug)
        variant_name = spec.get("variant_name") or name_parts[1]
        general_specification = self._parse_general_specification(soup)
        specification_tables = self._parse_specification_tables(soup)
        return {
            "identity": {
                "catalog_spec_id": catalog_spec_id,
                "make_slug": make_slug,
                "model_slug": model_slug,
                "make_name": make_name,
                "model_name": model_name,
                "variant_name": variant_name,
                "url": self.absolute_url(spec_url, BASE_CATALOG_URL),
            },
            "general_specification": general_specification,
            "specifications": {
                "engine_fuel": specification_tables.get("engine_fuel", {}),
                "suspension": specification_tables.get("suspension", {}),
                "drivetrain": specification_tables.get("drivetrain", {}),
            },
            "media": self._parse_media(soup, spec_url),
            "source": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "page_title": self.clean_text(soup.title) if soup.title else None,
            },
        }

    def _detail_name_parts(self, soup: BeautifulSoup, make_name: str | None) -> tuple[str | None, str | None]:
        title = self.clean_text(soup.select_one(".detail-right .name-car") or soup.select_one("h1"))
        if not title:
            return None, None
        if make_name and title.upper().startswith(str(make_name).upper() + " "):
            title = title[len(str(make_name)) :].strip()
        if " " not in title:
            return title, None
        model_name, variant_name = title.rsplit(" ", 1)
        return self.clean_text(model_name), self.clean_text(variant_name)

    def _model_name_from_slug(self, model_slug: str | None) -> str | None:
        if model_slug and "__" in model_slug:
            return model_slug.split("__", 1)[1].replace("_", " ")
        return None

    def _parse_general_specification(self, soup: BeautifulSoup) -> dict:
        rows: dict[str, str | None] = {}
        for row in soup.select(".general-box p"):
            key = self.clean_text(row.select_one("strong"))
            value = self.clean_text(row.select_one("span"))
            if key:
                rows[key] = value
        return rows

    def _parse_specification_tables(self, soup: BeautifulSoup) -> dict:
        tables: dict[str, dict] = {}
        for box in soup.select(".specifications-box"):
            title = self.clean_text(box.select_one(".title-box"))
            key = self._specification_key(title)
            if not key:
                continue
            rows: dict[str, object] = {}
            for row in box.select("table tr"):
                label = self.clean_text(row.find("th"))
                cell = row.find("td")
                if not label or cell is None:
                    continue
                nested = self._parse_nested_specs(cell)
                rows[label] = nested if nested else self.clean_text(cell)
            tables[key] = rows
        return tables

    def _specification_key(self, title: str | None) -> str | None:
        normalized = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
        return {"engine_fuel": "engine_fuel", "suspension": "suspension", "drivetrain": "drivetrain"}.get(normalized)

    def _parse_nested_specs(self, cell) -> dict:
        rows: dict[str, str | None] = {}
        for item in cell.select("p"):
            key = self.clean_text(item.select_one("strong"))
            value = self.clean_text(item.select_one("span"))
            if key:
                rows[key] = value
        return rows

    def _parse_media(self, soup: BeautifulSoup, base_url: str) -> dict:
        images: list[str] = []
        for image in soup.select(".gallery-main img, .gallery-thumbs img, img[src*='catalogphoto'], img[data-src*='catalogphoto'], a[href*='catalogphoto']"):
            src = self.clean_text(image.get("src") or image.get("data-src") or image.get("href"))
            if not src or src.startswith("data:"):
                continue
            url = self.absolute_url(src, base_url)
            if url not in images:
                images.append(url)
        return {"main_image": images[0] if images else None, "thumbnails": images[1:], "images": images}

    def upsert_makes(self, rows: list[dict], run_id: int | None = None) -> int:
        if not rows:
            return 0
        from psycopg2.extras import Json, execute_batch
        from scrap_goonet_cars_botasaurus.database import get_connection

        _ = run_id
        params = [
            {
                "make_slug": row.get("make_slug"),
                "make_name": row.get("make_name"),
                "country": row.get("country"),
                "make_url": row.get("make_url"),
                "raw": Json(row.get("raw")),
            }
            for row in rows
        ]
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    execute_batch(
                        cursor,
                        """
                        INSERT INTO cars_scrap_goonet_catalog_makes (make_slug, make_name, country, make_url, raw)
                        VALUES (%(make_slug)s, %(make_name)s, %(country)s, %(make_url)s, %(raw)s)
                        ON CONFLICT (make_slug) DO UPDATE SET
                          make_name = EXCLUDED.make_name,
                          country = EXCLUDED.country,
                          make_url = EXCLUDED.make_url,
                          raw = EXCLUDED.raw,
                          last_seen_at = NOW(),
                          status = 'active'
                        """,
                        params,
                    )
        finally:
            conn.close()
        return len(rows)

    def upsert_models(self, rows: list[dict], run_id: int | None = None) -> int:
        if not rows:
            return 0
        from psycopg2.extras import Json, execute_batch
        from scrap_goonet_cars_botasaurus.database import get_connection

        _ = run_id
        params = [
            {
                "model_slug": row.get("model_slug"),
                "make_slug": row.get("make_slug"),
                "model_name": row.get("model_name"),
                "production_period": row.get("production_period"),
                "production_start": row.get("production_start"),
                "production_end": row.get("production_end"),
                "model_url": row.get("model_url"),
                "raw": Json(row.get("raw")),
            }
            for row in rows
        ]
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    execute_batch(
                        cursor,
                        """
                        INSERT INTO cars_scrap_goonet_catalog_models (
                          model_slug, make_slug, model_name, production_period, production_start, production_end, model_url, raw
                        ) VALUES (
                          %(model_slug)s, %(make_slug)s, %(model_name)s, %(production_period)s, %(production_start)s,
                          %(production_end)s, %(model_url)s, %(raw)s
                        )
                        ON CONFLICT (model_slug) DO UPDATE SET
                          make_slug = EXCLUDED.make_slug,
                          model_name = EXCLUDED.model_name,
                          production_period = EXCLUDED.production_period,
                          production_start = EXCLUDED.production_start,
                          production_end = EXCLUDED.production_end,
                          model_url = EXCLUDED.model_url,
                          raw = EXCLUDED.raw,
                          last_seen_at = NOW(),
                          status = 'active'
                        """,
                        params,
                    )
        finally:
            conn.close()
        return len(rows)

    def upsert_spec_summaries(self, rows: list[dict], run_id: int | None = None) -> int:
        if not rows:
            return 0
        from psycopg2.extras import Json, execute_batch
        from scrap_goonet_cars_botasaurus.database import get_connection

        _ = run_id
        params = [
            {
                "catalog_spec_id": row.get("catalog_spec_id"),
                "make_slug": row.get("make_slug"),
                "model_slug": row.get("model_slug"),
                "make_name": row.get("make_name"),
                "model_name": row.get("model_name"),
                "variant_name": row.get("variant_name"),
                "release_month": row.get("release_month"),
                "sale_title": row.get("sale_title"),
                "spec_url": row.get("spec_url"),
                "model_code": row.get("model_code"),
                "engine": row.get("engine"),
                "doors": row.get("doors"),
                "transmission": row.get("transmission"),
                "drive": row.get("drive"),
                "dimension": row.get("dimension"),
                "weight": row.get("weight"),
                "msrp": row.get("msrp"),
                "raw_list_row": Json(row.get("raw_list_row")),
            }
            for row in rows
        ]
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    execute_batch(
                        cursor,
                        """
                        INSERT INTO cars_scrap_goonet_catalog_specs (
                          catalog_spec_id, make_slug, model_slug, make_name, model_name, variant_name, release_month, sale_title,
                          spec_url, model_code, engine, doors, transmission, drive, dimension, weight, msrp, raw_list_row
                        ) VALUES (
                          %(catalog_spec_id)s, %(make_slug)s, %(model_slug)s, %(make_name)s, %(model_name)s, %(variant_name)s,
                          %(release_month)s, %(sale_title)s, %(spec_url)s, %(model_code)s, %(engine)s, %(doors)s,
                          %(transmission)s, %(drive)s, %(dimension)s, %(weight)s, %(msrp)s, %(raw_list_row)s
                        )
                        ON CONFLICT (catalog_spec_id) DO UPDATE SET
                          make_slug = EXCLUDED.make_slug,
                          model_slug = EXCLUDED.model_slug,
                          make_name = EXCLUDED.make_name,
                          model_name = EXCLUDED.model_name,
                          variant_name = EXCLUDED.variant_name,
                          release_month = EXCLUDED.release_month,
                          sale_title = EXCLUDED.sale_title,
                          spec_url = EXCLUDED.spec_url,
                          model_code = EXCLUDED.model_code,
                          engine = EXCLUDED.engine,
                          doors = EXCLUDED.doors,
                          transmission = EXCLUDED.transmission,
                          drive = EXCLUDED.drive,
                          dimension = EXCLUDED.dimension,
                          weight = EXCLUDED.weight,
                          msrp = EXCLUDED.msrp,
                          raw_list_row = EXCLUDED.raw_list_row,
                          last_seen_at = NOW(),
                          status = 'active'
                        """,
                        params,
                    )
        finally:
            conn.close()
        return len(rows)

    def stable_spec_detail_hash(self, data: dict) -> str:
        normalized = dict(data)
        source = normalized.get("source")
        if isinstance(source, dict):
            source = dict(source)
            source.pop("scraped_at", None)
            normalized["source"] = source
        return self.stable_json_hash(normalized)

    def upsert_spec_detail(self, row: dict, run_id: int | None = None) -> bool:
        from psycopg2.extras import Json
        from scrap_goonet_cars_botasaurus.database import get_connection

        _ = run_id
        data = row.get("data") if isinstance(row.get("data"), dict) else row
        identity = data.get("identity", {})
        content_hash = self.stable_spec_detail_hash(data)
        params = {
            "catalog_spec_id": row.get("catalog_spec_id") or identity.get("catalog_spec_id"),
            "make_slug": row.get("make_slug") or identity.get("make_slug"),
            "model_slug": row.get("model_slug") or identity.get("model_slug"),
            "make_name": row.get("make_name") or identity.get("make_name"),
            "model_name": row.get("model_name") or identity.get("model_name"),
            "variant_name": row.get("variant_name") or identity.get("variant_name"),
            "spec_url": row.get("spec_url") or identity.get("url"),
            "data": Json(data),
            "content_hash": content_hash,
        }
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT content_hash FROM cars_scrap_goonet_catalog_specs WHERE catalog_spec_id = %s",
                        (params["catalog_spec_id"],),
                    )
                    old = cursor.fetchone()
                    cursor.execute(
                        """
                        INSERT INTO cars_scrap_goonet_catalog_specs (
                          catalog_spec_id, make_slug, model_slug, make_name, model_name, variant_name, spec_url,
                          data, content_hash, last_changed_at
                        ) VALUES (
                          %(catalog_spec_id)s, %(make_slug)s, %(model_slug)s, %(make_name)s, %(model_name)s, %(variant_name)s,
                          %(spec_url)s, %(data)s, %(content_hash)s, NOW()
                        )
                        ON CONFLICT (catalog_spec_id) DO UPDATE SET
                          data = CASE
                            WHEN cars_scrap_goonet_catalog_specs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                            THEN EXCLUDED.data ELSE cars_scrap_goonet_catalog_specs.data END,
                          content_hash = CASE
                            WHEN cars_scrap_goonet_catalog_specs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                            THEN EXCLUDED.content_hash ELSE cars_scrap_goonet_catalog_specs.content_hash END,
                          last_changed_at = CASE
                            WHEN cars_scrap_goonet_catalog_specs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                            THEN NOW() ELSE cars_scrap_goonet_catalog_specs.last_changed_at END,
                          make_slug = COALESCE(EXCLUDED.make_slug, cars_scrap_goonet_catalog_specs.make_slug),
                          model_slug = COALESCE(EXCLUDED.model_slug, cars_scrap_goonet_catalog_specs.model_slug),
                          make_name = COALESCE(EXCLUDED.make_name, cars_scrap_goonet_catalog_specs.make_name),
                          model_name = COALESCE(EXCLUDED.model_name, cars_scrap_goonet_catalog_specs.model_name),
                          variant_name = COALESCE(EXCLUDED.variant_name, cars_scrap_goonet_catalog_specs.variant_name),
                          spec_url = EXCLUDED.spec_url,
                          last_seen_at = NOW(),
                          status = 'active'
                        """,
                        params,
                    )
        finally:
            conn.close()
        return old is None or old[0] != content_hash

    def get_pending_spec_details(self, limit: int = 0) -> list[dict]:
        from scrap_goonet_cars_botasaurus.database import get_connection

        query = """
            SELECT catalog_spec_id, make_slug, model_slug, make_name, model_name, variant_name, spec_url, last_seen_at
            FROM cars_scrap_goonet_catalog_specs
            WHERE status = 'active'
              AND spec_url IS NOT NULL
              AND spec_url <> ''
              AND (content_hash IS NULL OR data = '{}'::jsonb)
            ORDER BY last_seen_at NULLS FIRST, catalog_spec_id
        """
        params: tuple[int, ...] = ()
        if limit and limit > 0:
            query += " LIMIT %s"
            params = (limit,)
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, params)
                    columns = [column[0] for column in cursor.description]
                    return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def sync_pending_spec_details(self, max_specs: int = 0, delay_seconds: float | None = None) -> dict:
        pending = self.get_pending_spec_details(max_specs)
        summary = {"status": "running", "pending": len(pending), "spec_details": 0, "details_changed": 0, "errors": []}
        delay = self._catalog_delay(delay_seconds)
        for spec_index, spec in enumerate(pending, start=1):
            self.logger.info("Processing pending spec detail %s/%s: %s", spec_index, len(pending), spec.get("spec_url"))
            try:
                detail = self.parse_spec_detail(self.fetch_html(spec["spec_url"]), spec)
                if self.upsert_spec_detail({"data": detail}, None):
                    summary["details_changed"] += 1
                summary["spec_details"] += 1
            except Exception as exc:
                self.logger.warning("Failed to sync pending spec detail %s: %s", spec.get("spec_url"), exc)
                self._record_sync_error(summary, "spec_details", spec.get("spec_url"), exc)
            self._sleep_between_requests(delay)
        summary["status"] = "success" if not summary["errors"] else "partial"
        self.logger.info("Finished pending spec detail sync summary=%s", json.dumps(summary, ensure_ascii=False))
        return summary

    def mark_missing_makes(self, run_started_at) -> int:
        return self._mark_missing("cars_scrap_goonet_catalog_makes", run_started_at)

    def mark_missing_models(self, run_started_at) -> int:
        return self._mark_missing("cars_scrap_goonet_catalog_models", run_started_at)

    def mark_missing_specs(self, run_started_at) -> int:
        return self._mark_missing("cars_scrap_goonet_catalog_specs", run_started_at)

    def _mark_missing(self, table_name: str, run_started_at) -> int:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"UPDATE {table_name} SET status = 'missing' WHERE status = 'active' AND last_seen_at < %s",
                        (run_started_at,),
                    )
                    return cursor.rowcount
        finally:
            conn.close()

    def _create_catalog_run(self, resumed_from_run_id: int | None = None) -> tuple[int, object]:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO cars_scrap_goonet_catalog_runs (resumed_from_run_id, heartbeat_at) VALUES (%s, NOW()) RETURNING id, started_at",
                        (resumed_from_run_id,),
                    )
                    return cursor.fetchone()
        finally:
            conn.close()

    def _resumable_run(self, run_id: int | None = None) -> dict:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                where = "id = %s AND " if run_id is not None else ""
                params = (run_id,) if run_id is not None else ()
                cursor.execute(
                    f"""SELECT id, current_make_slug, current_model_slug, status
                        FROM cars_scrap_goonet_catalog_runs
                        WHERE {where}status IN ('running', 'interrupted', 'partial', 'error')
                        ORDER BY started_at DESC, id DESC LIMIT 1""",
                    params,
                )
                row = cursor.fetchone()
                if not row:
                    raise RuntimeError(f"No resumable Goonet catalog run{' with id ' + str(run_id) if run_id is not None else ''} found")
                return dict(zip(("id", "current_make_slug", "current_model_slug", "status"), row))
        finally:
            conn.close()

    def _update_run(self, run_id: int, phase: str, make_slug: str | None = None, model_slug: str | None = None, checkpoint: bool = False) -> None:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    if checkpoint:
                        cursor.execute(
                            "UPDATE cars_scrap_goonet_catalog_runs SET phase=%s, current_make_slug=%s, current_model_slug=%s, heartbeat_at=NOW() WHERE id=%s",
                            (phase, make_slug, model_slug, run_id),
                        )
                        self.logger.info("Checkpoint updated run_id=%s make=%s model=%s", run_id, make_slug, model_slug)
                    else:
                        cursor.execute("UPDATE cars_scrap_goonet_catalog_runs SET phase=%s, heartbeat_at=NOW() WHERE id=%s", (phase, run_id))
        finally:
            conn.close()

    def _finish_catalog_run(self, run_id: int, status: str, summary: dict, error: str | None = None) -> None:
        from scrap_goonet_cars_botasaurus.database import get_connection

        errors = summary.get("errors") or []
        error_text = error or (json.dumps(errors, ensure_ascii=False) if errors else None)
        if error_text and len(error_text) > 4000:
            error_text = error_text[:4000]
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """UPDATE cars_scrap_goonet_catalog_runs
                           SET finished_at=NOW(), status=%s, phase='finished', heartbeat_at=NOW(),
                               makes_count=%s, models_count=%s, specs_count=%s, error=%s WHERE id=%s""",
                        (status, summary.get("makes", 0), summary.get("models", 0), summary.get("spec_summaries", 0), error_text, run_id),
                    )
        finally:
            conn.close()

    def _completed_spec_ids(self, model_slug: str) -> set[str]:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT catalog_spec_id FROM cars_scrap_goonet_catalog_specs WHERE model_slug=%s AND data <> '{}'::jsonb AND content_hash IS NOT NULL",
                    (model_slug,),
                )
                return {row[0] for row in cursor.fetchall()}
        finally:
            conn.close()

    def _pending_count(self) -> int:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM cars_scrap_goonet_catalog_specs WHERE status='active' AND (content_hash IS NULL OR data='{}'::jsonb)")
                return cursor.fetchone()[0]
        finally:
            conn.close()

    def _catalog_delay(self, delay_seconds: float | None) -> float:
        if delay_seconds is not None:
            return max(float(delay_seconds), 0.0)
        return max(float(os.getenv("GOONET_CATALOG_DELAY_SECONDS") or 0), 0.0)

    def _sleep_between_requests(self, delay_seconds: float) -> None:
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    def _record_sync_error(self, summary: dict, stage: str, url: str | None, error: Exception) -> None:
        summary["errors"].append({"stage": stage, "url": url, "error": str(error)})

    def _backfill_pending(self, summary: dict, run_id: int, max_specs: int, delay: float, attempted: set[str]) -> None:
        pending = self.get_pending_spec_details()
        self.logger.info("Pending detail backfill run_id=%s count=%s attempted=%s budget=%s", run_id, len(pending), len(attempted), max_specs or "unlimited")
        for spec in pending:
            spec_id = spec["catalog_spec_id"]
            if spec_id in attempted:
                continue
            if max_specs > 0 and summary["detail_attempts"] >= max_specs:
                break
            attempted.add(spec_id)
            summary["detail_attempts"] += 1
            self._update_run(run_id, "pending_details")
            try:
                detail = self.parse_spec_detail(self.fetch_html(spec["spec_url"]), spec)
                if self.upsert_spec_detail({"data": detail}, run_id):
                    summary["details_changed"] += 1
                summary["spec_details"] += 1
            except Exception as exc:
                self.logger.warning("Failed pending detail %s: %s", spec.get("spec_url"), exc)
                self._record_sync_error(summary, "pending_details", spec.get("spec_url"), exc)
            self._sleep_between_requests(delay)

    def resume(self, run_id: int | None = None, **limits) -> dict:
        source = self._resumable_run(run_id)
        return self._sync(resume_from=source, **limits)

    def sync_all(self, *, max_makes: int = 0, max_models: int = 0, max_specs: int = 0, delay_seconds: float | None = None) -> dict:
        return self._sync(max_makes=max_makes, max_models=max_models, max_specs=max_specs, delay_seconds=delay_seconds)

    def _sync(
        self,
        *,
        max_makes: int = 0,
        max_models: int = 0,
        max_specs: int = 0,
        delay_seconds: float | None = None,
        resume_from: dict | None = None,
    ) -> dict:
        from scrap_goonet_cars_botasaurus.database import get_connection

        summary = {"run_id": None, "resumed_from_run_id": resume_from and resume_from["id"], "status": "running", "makes": 0,
                   "models": 0, "spec_summaries": 0, "spec_details": 0, "detail_attempts": 0, "details_changed": 0,
                   "details_skipped": 0, "pending_details": 0, "missing_makes": 0, "missing_models": 0,
                   "missing_specs": 0, "errors": []}
        run_id = None
        lock_conn = None
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        sigterm_received = False
        sigterm_installed = False

        def interrupt_on_sigterm(_signum, _frame):
            nonlocal sigterm_received
            if sigterm_received:
                return
            sigterm_received = True
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            raise CatalogInterrupted("Received SIGTERM")

        try:
            signal.signal(signal.SIGTERM, interrupt_on_sigterm)
            sigterm_installed = True
            lock_conn = get_connection()
            with lock_conn.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", (7274, 19001))
                if not cursor.fetchone()[0]:
                    raise RuntimeError("Another Goonet catalog sync/resume job is already running")
            run_id, run_started_at = self._create_catalog_run(summary["resumed_from_run_id"])
            summary["run_id"] = run_id
            delay = self._catalog_delay(delay_seconds)
            checkpoint_make = resume_from and resume_from.get("current_make_slug")
            checkpoint_model = resume_from and resume_from.get("current_model_slug")
            if checkpoint_make or checkpoint_model:
                self._update_run(run_id, "resuming", checkpoint_make, checkpoint_model, checkpoint=True)
            self.logger.info("Starting catalog run_id=%s resume_source=%s checkpoint=%s/%s limits=%s/%s/%s",
                             run_id, summary["resumed_from_run_id"], checkpoint_make, checkpoint_model, max_makes, max_models, max_specs)
            attempted: set[str] = set()
            self._backfill_pending(summary, run_id, max_specs, delay, attempted)
            if max_specs > 0 and summary["detail_attempts"] >= max_specs:
                summary["pending_details"] = self._pending_count()
                summary["status"] = "partial"
                self._finish_catalog_run(run_id, summary["status"], summary)
                self.logger.info("Finished catalog run_id=%s after capped pending backfill pending=%s", run_id, summary["pending_details"])
                return summary
            self._update_run(run_id, "discovery")
            makes = self.parse_makes(self.fetch_html(BASE_CATALOG_URL), BASE_CATALOG_URL)
            if checkpoint_make:
                checkpoint_index = next((i for i, make in enumerate(makes) if make["make_slug"] == checkpoint_make), None)
                if checkpoint_index is None:
                    raise RuntimeError(f"Saved checkpoint make {checkpoint_make} is no longer present in catalog")
                makes = makes[checkpoint_index:]
            if max_makes > 0:
                makes = makes[:max_makes]
            summary["makes"] = self.upsert_makes(makes, run_id)
            self._sleep_between_requests(delay)

            for make in makes:
                if max_models > 0 and summary["models"] >= max_models:
                    break
                try:
                    self._update_run(run_id, "models")
                    models = self.parse_models(self.fetch_html(make["make_url"]), make)
                except Exception as exc:
                    self._record_sync_error(summary, "models", make.get("make_url"), exc)
                    self.logger.warning("Failed models page %s: %s", make.get("make_url"), exc)
                    continue
                for model in models:
                    model["make_name"] = make.get("make_name")
                if make["make_slug"] == checkpoint_make and checkpoint_model:
                    model_index = next((i for i, model in enumerate(models) if model["model_slug"] == checkpoint_model), None)
                    if model_index is None:
                        raise RuntimeError(f"Saved checkpoint model {checkpoint_model} is no longer present in catalog")
                    models = models[model_index + 1:]
                if max_models > 0:
                    models = models[:max_models - summary["models"]]
                if models:
                    summary["models"] += self.upsert_models(models, run_id)
                self._sleep_between_requests(delay)

                stop_discovery = False
                for model in models:
                    try:
                        self._update_run(run_id, "spec_summaries")
                        specs = self.parse_model_specs(self.fetch_html(model["model_url"]), model)
                    except Exception as exc:
                        self._record_sync_error(summary, "spec_summaries", model.get("model_url"), exc)
                        self.logger.warning("Failed model page %s: %s", model.get("model_url"), exc)
                        continue
                    # A checkpoint means every link on this model page was discovered and durably upserted.
                    summary["spec_summaries"] += self.upsert_spec_summaries(specs, run_id)
                    completed = self._completed_spec_ids(model["model_slug"])
                    model_complete = True
                    for spec in specs:
                        spec_id = spec["catalog_spec_id"]
                        if spec_id in completed:
                            summary["details_skipped"] += 1
                            continue
                        if spec_id in attempted:
                            continue
                        if max_specs > 0 and summary["detail_attempts"] >= max_specs:
                            model_complete = False
                            break
                        attempted.add(spec_id)
                        summary["detail_attempts"] += 1
                        self._update_run(run_id, "spec_details")
                        try:
                            detail = self.parse_spec_detail(self.fetch_html(spec["spec_url"]), spec)
                            if self.upsert_spec_detail({"data": detail}, run_id):
                                summary["details_changed"] += 1
                            summary["spec_details"] += 1
                        except Exception as exc:
                            self._record_sync_error(summary, "spec_details", spec.get("spec_url"), exc)
                            self.logger.warning("Failed spec detail %s: %s", spec.get("spec_url"), exc)
                        self._sleep_between_requests(delay)
                    if model_complete:
                        self.logger.info("Model fully enumerated; detail failures remain durably pending, checkpoint may advance model=%s", model["model_slug"])
                        self._update_run(run_id, "model_complete", make["make_slug"], model["model_slug"], checkpoint=True)
                    else:
                        self.logger.info("Detail budget exhausted mid-model; retaining prior checkpoint model=%s", model["model_slug"])
                        stop_discovery = True
                        break
                if stop_discovery:
                    break

            if not resume_from and max_makes <= 0 and max_models <= 0 and max_specs <= 0:
                if not any(error["stage"] == "models" for error in summary["errors"]):
                    summary["missing_makes"] = self.mark_missing_makes(run_started_at)
                    summary["missing_models"] = self.mark_missing_models(run_started_at)
                if not any(error["stage"] == "spec_summaries" for error in summary["errors"]):
                    summary["missing_specs"] = self.mark_missing_specs(run_started_at)
            if max_specs <= 0 or summary["detail_attempts"] < max_specs:
                self._backfill_pending(summary, run_id, max_specs, delay, attempted)
            summary["pending_details"] = self._pending_count()
            limited = max_makes > 0 or max_models > 0 or max_specs > 0
            summary["status"] = "partial" if limited or summary["errors"] or summary["pending_details"] else "success"
            self._finish_catalog_run(run_id, summary["status"], summary)
            self.logger.info("Finished catalog run_id=%s status=%s skipped=%s pending=%s", run_id, summary["status"], summary["details_skipped"], summary["pending_details"])
        except KeyboardInterrupt:
            if run_id is not None:
                summary["status"] = "interrupted"
                self._finish_catalog_run(run_id, "interrupted", summary, "Interrupted by user")
            raise
        except Exception as exc:
            if run_id is not None:
                summary["status"] = "error"
                self._record_sync_error(summary, "fatal", None, exc)
                self._finish_catalog_run(run_id, "error", summary, str(exc))
            else:
                raise
        finally:
            try:
                if lock_conn is not None:
                    try:
                        with lock_conn.cursor() as cursor:
                            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", (7274, 19001))
                    finally:
                        lock_conn.close()
            finally:
                if sigterm_installed:
                    signal.signal(signal.SIGTERM, previous_sigterm)
        return summary
