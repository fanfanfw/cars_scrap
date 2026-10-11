from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from cars_scrap_common.logging_utils import setup_logger


BASE_CATALOG_URL = "https://www.goo-net-exchange.com/catalog/"

# GNC-IMG-01: Goonet catalog galleries put the first photo inline as a base64
# data URI in `.gallery-main` and only expose its URL via the matching
# `.gallery-thumbs` entry (`/carphoto/thumb/<name>.jpg`). The non-thumb URL
# (`/carphoto/<name>.jpg`) must be derived from a trusted source and
# network-validated before it is persisted; base64, `/thumb/` URLs, arbitrary
# hosts/schemes, and failed guesses are never stored.
MEDIA_PROBE_BYTES = 65536
IMAGE_CONTENT_TYPE_PREFIX = "image/"
MEDIA_HOST = "catalogphoto.goo-net.com"
MEDIA_PATH_PREFIX = "/carphoto/"
MEDIA_THUMB_PREFIX = "/carphoto/thumb/"
TRUSTED_MEDIA_HOSTS = ("catalogphoto.goo-net.com", "www.goo-net-exchange.com", "goo-net-exchange.com")
ALLOWED_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif")
# GNC-IMG-01 probe outcomes: a definitive verdict (valid/invalid) may be acted
# on, while a transient one (429/5xx/timeout/connection error) must NOT be
# persisted as empty media or it would erase prior media and mark the spec done.
MEDIA_VALID = "valid"
MEDIA_INVALID = "invalid"
MEDIA_TRANSIENT = "transient"

# GNC-IMG-03: bounded, operator-driven media sample refresh for a handful of
# EXISTING catalog specs. There is deliberately no auto-selection, no sync-all
# and no scheduling here: the caller must name 1..MAX explicit catalog_spec_id
# values whose spec_url rows already exist in the DB. Source URLs always come
# from the stored rows (never an arbitrary CLI URL), requests are serially
# throttled, and any DB write additionally requires an explicit apply flag plus
# a verified rollback snapshot of the exact pre-write rows.
SAMPLE_REFRESH_MAX_IDS = 3
SAMPLE_REFRESH_MIN_INTERVAL_SECONDS = 1.0
SAMPLE_REFRESH_MEDIA_FIELDS = ("main_image", "thumbnails", "images")
# A stored spec_url is only ever fetched when it is the exact canonical detail
# URL for that catalog_spec_id on the trusted host: https, no userinfo, no
# explicit port, no query, no fragment, exactly
# /catalog/<MODEL_SLUG>/<id>/. This blocks SSRF / off-host or exotic URLs that
# may have been injected into the DB by another writer.
SPEC_REFRESH_ALLOWED_HOST = "www.goo-net-exchange.com"
SPEC_REFRESH_PATH_RE = re.compile(r"^/catalog/(?P<model>[A-Za-z0-9_]+__[A-Za-z0-9_]+)/(?P<spec_id>\d+)/$")
# Snapshot columns: every column the guard/merge/restore can rely on. The
# restore writes back the full media-content subset (data/content_hash/timestamps/
# status/spec_url) and verifies it round-trips; the immutable artifact itself
# preserves ALL columns as the forensic record.
SAMPLE_REFRESH_SNAPSHOT_COLUMNS = (
    "id", "catalog_spec_id", "make_slug", "model_slug", "make_name", "model_name",
    "variant_name", "release_month", "sale_title", "spec_url", "model_code", "engine",
    "doors", "transmission", "drive", "dimension", "weight", "msrp", "data",
    "raw_list_row", "content_hash", "first_seen_at", "last_seen_at", "last_changed_at", "status",
)


class CatalogInterrupted(KeyboardInterrupt):
    pass


class SampleRefreshConflict(RuntimeError):
    """A guarded sample-refresh write found a concurrent change.

    Raised inside the single apply transaction so the WHOLE batch is rolled back
    (no partial apply). This keeps the snapshot's ``applied_states`` accurate: a
    batch either applies every changed row or none of them.
    """

    def __init__(self, catalog_spec_id: str) -> None:
        super().__init__("concurrent change detected; sample refresh batch aborted")
        self.catalog_spec_id = catalog_spec_id


class SampleRefreshRollbackError(RuntimeError):
    """A guarded rollback could not be applied to every candidate row.

    Raised so the whole restore transaction is rolled back and NO row is restored
    (fail closed), rather than committing a partial restore.
    """


class MediaUnresolved(RuntimeError):
    """Gallery image resolution was indeterminate (429/5xx/timeout) or a derived
    candidate could not be verified against the network.

    Raised instead of yielding empty media so a per-spec run abstains: the
    stored spec (and its previously populated media) is left untouched and the
    run reports partial rather than success. A genuinely image-less page still
    yields empty media normally. The message is a fixed string (never untrusted
    page content); ``retryable`` marks a transient/indeterminate verdict.
    """

    def __init__(self, retryable: bool = False) -> None:
        super().__init__("image validation pending retry" if retryable else "derived image could not be verified")
        self.retryable = retryable


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
        try:
            return self._collect_media(soup, base_url)
        except MediaUnresolved:
            # Indeterminate validation: propagate so the caller abstains from
            # writing (preserving any previously stored media and leaving a
            # first-time spec pending) instead of overwriting with empty media.
            raise
        except Exception as exc:
            # Unexpected parsing failure is fail-closed: abort this spec's media
            # (do NOT return empty media, which would hash/upsert and erase any
            # previously stored media). Log the exception type only; the message
            # may embed huge/untrusted page content. A page with no gallery stays
            # a definitive empty result inside _collect_media.
            self.logger.warning("Media parse failed (%s); failing closed", type(exc).__name__)
            raise MediaUnresolved(retryable=True) from exc

    def _collect_media(self, soup: BeautifulSoup, base_url: str) -> dict:
        slides = soup.select(".gallery-main img")
        thumbs = soup.select(".gallery-thumbs img")
        sources = slides
        if not sources:
            sources = soup.select(
                "img[src*='carphoto'], img[data-src*='carphoto'], a[href*='carphoto'], "
                "img[src*='catalogphoto'], img[data-src*='catalogphoto'], a[href*='catalogphoto']"
            )
        images: list[str] = []
        thumbnails: list[str] = []
        unresolved_retryable = False
        unresolved_terminal = False
        for index, node in enumerate(sources):
            thumb_node = thumbs[index] if sources is slides and index < len(thumbs) else None
            thumb_url = self._absolute_media_url(thumb_node, base_url)
            if thumb_url and self._is_thumb_url(thumb_url) and thumb_url not in thumbnails:
                thumbnails.append(thumb_url)
            src_url = self._absolute_media_url(node, base_url)
            if src_url and not self._is_thumb_url(src_url):
                # The slide itself carries a direct src.
                if self._is_trusted_direct_url(src_url):
                    if src_url not in images:
                        images.append(src_url)
                else:
                    # Existing slide with an untrusted/nonconforming direct src
                    # cannot yield a usable image: fail closed rather than drop
                    # it and store empty media over previously stored images.
                    unresolved_terminal = True
                continue
            # The slide's real URL is hidden as base64 (or as its own /thumb/
            # URL). Derive the non-thumb URL from a trusted thumb source. Only a
            # definitive VALID probe is stored; a derivable candidate that could
            # not be validated (404, 429, timeout, bad/unknown bytes) means the
            # gallery could not be established, so abstain rather than store
            # empty media (which would erase prior media and mark it complete).
            candidate = self._thumb_to_nonthumb(thumb_url or src_url)
            if candidate is None:
                # Existing slide (base64/absent src) with a missing or malformed
                # thumbnail cannot be resolved: fail closed, never empty.
                unresolved_terminal = True
                continue
            if candidate in images:
                # Duplicate of an already-stored image: harmless, never fail.
                continue
            verdict = self.probe_image_url(candidate)
            if verdict == MEDIA_VALID:
                images.append(candidate)
            elif verdict == MEDIA_TRANSIENT:
                unresolved_retryable = True
            else:
                unresolved_terminal = True
        if unresolved_retryable or unresolved_terminal:
            raise MediaUnresolved(retryable=unresolved_retryable)
        return {"main_image": images[0] if images else None, "thumbnails": thumbnails, "images": images}

    def _absolute_media_url(self, node, base_url: str) -> str | None:
        src = self._media_src(node)
        return self.absolute_url(src, base_url) if src else None

    def _media_src(self, node) -> str | None:
        if node is None:
            return None
        src = self.clean_text(node.get("src") or node.get("data-src") or node.get("href"))
        if not src or src.lower().startswith("data:"):
            return None
        return src

    def _safe_media_parts(self, url: str | None, host: str | None = None):
        """Return parsed URL parts only for a safe https image URL, else None.

        Rejects non-https schemes, unexpected hosts, userinfo, explicit ports,
        and fragments. ``host`` restricts to a single exact host when given.
        """
        try:
            parts = urlparse(url or "")
            allowed = {host} if host else set(TRUSTED_MEDIA_HOSTS)
            if parts.scheme != "https" or parts.hostname not in allowed:
                return None
            if parts.username or parts.password or parts.port or parts.fragment:
                return None
        except ValueError:
            return None
        return parts

    def _allowed_image_suffix(self, path: str) -> bool:
        return path.lower().endswith(ALLOWED_IMAGE_SUFFIXES)

    def _is_thumb_url(self, url: str | None) -> bool:
        parts = self._safe_media_parts(url)
        return bool(parts) and parts.path.startswith(MEDIA_THUMB_PREFIX)

    def _is_trusted_direct_url(self, url: str | None) -> bool:
        parts = self._safe_media_parts(url)
        if parts is None or parts.path.startswith(MEDIA_THUMB_PREFIX):
            return False
        return parts.path.startswith(MEDIA_PATH_PREFIX) and self._allowed_image_suffix(parts.path)

    def _thumb_to_nonthumb(self, url: str | None) -> str | None:
        """Derive `/carphoto/<name>.jpg` from a trusted `/carphoto/thumb/` URL."""
        parts = self._safe_media_parts(url, host=MEDIA_HOST)
        if parts is None or not parts.path.startswith(MEDIA_THUMB_PREFIX):
            return None
        name = parts.path[len(MEDIA_THUMB_PREFIX):]
        if not name or "/" in name or not self._allowed_image_suffix(name):
            return None
        return urlunparse(parts._replace(path=MEDIA_PATH_PREFIX + name))

    def validate_image_url(self, url: str | None) -> bool:
        """Boolean view: True only on a definitive valid image (see probe)."""
        return self.probe_image_url(url) == MEDIA_VALID

    def probe_image_url(self, url: str | None) -> str:
        """Classify a candidate non-thumb URL as valid / invalid / transient.

        Bounded, non-redirected probe (https + exact host + `/carphoto/` +
        image suffix). ``MEDIA_VALID`` requires HTTP 2xx, ``image/*`` content
        type, and recognizable JPEG/PNG/GIF dimensions > 0. Definitive
        negative answers (bad URL/shape, redirect, 4xx, wrong type, unknown
        format) are ``MEDIA_INVALID``; 429/5xx and network errors (timeout,
        connection) are ``MEDIA_TRANSIENT`` so callers can abstain instead of
        persisting empty media."""
        parts = self._safe_media_parts(url, host=MEDIA_HOST)
        if parts is None or not parts.path.startswith(MEDIA_PATH_PREFIX):
            return MEDIA_INVALID
        if parts.path.startswith(MEDIA_THUMB_PREFIX) or not self._allowed_image_suffix(parts.path):
            return MEDIA_INVALID
        timeout = float(os.getenv("GOONET_CATALOG_TIMEOUT_SECONDS") or 15)
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
        try:
            with requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False) as response:
                status = response.status_code
                if status == 429 or status >= 500:
                    return MEDIA_TRANSIENT
                if not 200 <= status < 300:
                    return MEDIA_INVALID
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not content_type.startswith(IMAGE_CONTENT_TYPE_PREFIX):
                    return MEDIA_INVALID
                data = b""
                for chunk in response.iter_content(chunk_size=8192):
                    data += chunk
                    if len(data) >= MEDIA_PROBE_BYTES:
                        break
                if not data:
                    return MEDIA_INVALID
                dimensions = self._image_dimensions(data)
                if bool(dimensions) and dimensions[0] > 0 and dimensions[1] > 0:
                    return MEDIA_VALID
                return MEDIA_INVALID
        except requests.RequestException as exc:
            self.logger.warning("Image validation transient for %s (%s)", url, type(exc).__name__)
            return MEDIA_TRANSIENT

    def _image_dimensions(self, data: bytes) -> tuple[int, int] | None:
        try:
            if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
                return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            if data[:3] == b"GIF" and len(data) >= 10:
                return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
            if data[:2] == b"\xff\xd8":
                return self._jpeg_dimensions(data)
        except (IndexError, ValueError):
            return None
        return None

    def _jpeg_dimensions(self, data: bytes) -> tuple[int, int] | None:
        index = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if marker == 0xD9:
                return None
            segment_length = int.from_bytes(data[index + 2:index + 4], "big")
            if segment_length < 2:
                return None
            if marker in sof_markers:
                return int.from_bytes(data[index + 7:index + 9], "big"), int.from_bytes(data[index + 5:index + 7], "big")
            index += 2 + segment_length
        return None

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
        summary = {"status": "running", "pending": len(pending), "spec_details": 0, "details_changed": 0, "media_unresolved": 0, "errors": []}
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
                self._record_spec_detail_error(summary, "spec_details", spec.get("spec_url"), exc)
            self._sleep_between_requests(delay)
        summary["status"] = "success" if not summary["errors"] else "partial"
        self.logger.info("Finished pending spec detail sync summary=%s", json.dumps(summary, ensure_ascii=False))
        return summary

    # ------------------------------------------------------------------
    # GNC-IMG-03: bounded sample media refresh for existing catalog specs.
    # ------------------------------------------------------------------
    def _normalize_refresh_ids(self, catalog_spec_ids) -> list[str]:
        """Return a de-duplicated, order-preserving list of 1..MAX explicit ids.

        Rejects empty input, more than ``SAMPLE_REFRESH_MAX_IDS`` entries and any
        non-numeric id. This is the single choke point that guarantees the tool
        can never widen into a bulk/auto-selected scrape.
        """
        raw = list(catalog_spec_ids or [])
        # Bound the RAW argument length BEFORE de-duplication so a caller cannot
        # smuggle a bulk list past the cap by repeating ids (e.g. 4 copies of one
        # id must be rejected, not collapsed to a single accepted id).
        if len(raw) > SAMPLE_REFRESH_MAX_IDS:
            raise ValueError(
                f"at most {SAMPLE_REFRESH_MAX_IDS} catalog_spec_id values may be supplied "
                f"(got {len(raw)})"
            )
        normalized: list[str] = []
        for value in raw:
            text = str(value).strip()
            if not text:
                raise ValueError("catalog_spec_id must be a non-empty integer string")
            if not re.fullmatch(r"\d+", text):
                raise ValueError(f"catalog_spec_id must be numeric: {text!r}")
            if text not in normalized:
                normalized.append(text)
        if not normalized:
            raise ValueError("at least one explicit catalog_spec_id is required")
        return normalized

    def _load_spec_rows(self, catalog_spec_ids: list[str]) -> dict[str, dict]:
        """Fetch the exact stored rows (all snapshot columns) for the requested ids.

        ``spec_url`` is the ONLY source of the page to fetch; an operator cannot
        inject an arbitrary URL, and the URL is separately validated against the
        exact canonical detail-URL shape before any request is made.
        """
        from psycopg2.extras import RealDictCursor

        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                columns = ", ".join(SAMPLE_REFRESH_SNAPSHOT_COLUMNS)
                cursor.execute(
                    f"SELECT {columns} FROM cars_scrap_goonet_catalog_specs WHERE catalog_spec_id = ANY(%s)",
                    (list(catalog_spec_ids),),
                )
                return {str(row["catalog_spec_id"]): dict(row) for row in cursor.fetchall()}
        finally:
            conn.close()

    def _validated_spec_url(self, spec_url: str | None, catalog_spec_id: str) -> str | None:
        """Return the stored URL only if it is the exact canonical detail URL.

        Enforced shape: ``https://www.goo-net-exchange.com/catalog/<MODEL__SLUG>/<id>/``
        with no userinfo, no explicit port, no query, no fragment and exactly one
        model path segment. Anything else (off-host, http, port, query, fragment,
        path traversal, id mismatch) is rejected so a poisoned DB row cannot turn
        this tool into an SSRF primitive.
        """
        try:
            parts = urlparse(str(spec_url or ""))
        except ValueError:
            return None
        if parts.scheme != "https" or parts.hostname != SPEC_REFRESH_ALLOWED_HOST:
            return None
        if parts.username or parts.password or parts.port or parts.query or parts.fragment:
            return None
        match = SPEC_REFRESH_PATH_RE.match(parts.path)
        if not match or match.group("spec_id") != str(catalog_spec_id):
            return None
        return f"https://{SPEC_REFRESH_ALLOWED_HOST}{parts.path}"

    def _snapshot_dir(self) -> Path:
        return Path.home() / ".local" / "state" / "cars_scrap_goonet_catalog_sample_refresh"

    def _ensure_secure_dir(self, directory: Path) -> None:
        """Create ``directory`` as a real dir (never through a symlink), mode 0700."""
        if directory.exists() and directory.is_symlink():
            raise RuntimeError(f"refusing to use symlinked snapshot directory: {directory}")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise RuntimeError(f"refusing to use symlinked snapshot directory: {directory}")
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:  # noqa: BLE001 - chmod failure must fail closed
            raise RuntimeError(f"could not set snapshot directory mode 0700: {exc}") from exc

    @staticmethod
    def _json_default(value):
        if isinstance(value, (datetime,)):
            return value.isoformat()
        return str(value)

    def _json_normalize(self, value):
        """Round-trip a value through JSON so on-disk and in-memory forms match."""
        return json.loads(json.dumps(value, default=self._json_default, ensure_ascii=False))

    @staticmethod
    def _canonical_timestamp(value) -> str | None:
        """Canonical UTC ISO-8601 for a datetime or ISO string, else None.

        Used for the rollback post-state guard so a DB-returned ``timestamptz``
        (datetime) and the artifact's stored ISO string compare on equal terms
        regardless of timezone rendering or sub-second formatting.
        """
        if value is None:
            return None
        try:
            if isinstance(value, str):
                moment = datetime.fromisoformat(value)
            elif isinstance(value, datetime):
                moment = value
            else:
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            return None

    def _write_rollback_backup(
        self,
        rows: list[dict],
        *,
        applied_states: dict[str, dict] | None = None,
        created_at: str | None = None,
    ) -> str:
        """Persist an immutable, integrity-checked rollback artifact of pre-write rows.

        The file lives OUTSIDE the repository in a real (never symlinked)
        directory with mode 0700, is created with mode 0600, written atomically
        (``O_EXCL`` temp in the same dir + ``os.replace``) and then re-read and
        validated (artifact tag, unique ids, per-row post-write state) before the
        path is returned. Any failure raises so the caller fails closed BEFORE
        writing. The artifact is the immortal forensic record: it is never
        modified or deleted by this tool.
        """
        if not rows:
            raise RuntimeError("refusing to write an empty rollback snapshot")
        applied_states = applied_states or {}
        normalized_rows = self._json_normalize(rows)
        expected_post = self._json_normalize(applied_states)
        payload = {
            "artifact": "gnc-img-03-sample-refresh-snapshot",
            "version": 1,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "columns": list(SAMPLE_REFRESH_SNAPSHOT_COLUMNS),
            "rows": normalized_rows,
            "applied_states": expected_post,
        }
        ids = [str(row.get("catalog_spec_id")) for row in normalized_rows]
        if len(set(ids)) != len(ids):
            raise RuntimeError("snapshot rows contain duplicate catalog_spec_id values")
        snapshot_dir = self._snapshot_dir()
        self._ensure_secure_dir(snapshot_dir)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final_path = snapshot_dir / f"sample_refresh_{stamp}_{secrets.token_hex(6)}.json"
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".sample_refresh_", suffix=".tmp", dir=str(snapshot_dir))
        try:
            os.fchmod(tmp_fd, 0o600)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False, default=self._json_default)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, str(final_path))
            os.chmod(final_path, 0o600)
        except Exception:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass
            raise
        # Fail closed unless the artifact on disk is readable, complete and matches
        # the exact rows/expected post-state we are about to write.
        try:
            with open(final_path, "r", encoding="utf-8") as handle:
                written = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - any failure means no safe rollback
            raise RuntimeError(f"rollback snapshot could not be verified: {type(exc).__name__}") from exc
        if (
            written.get("artifact") != payload["artifact"]
            or written.get("columns") != list(SAMPLE_REFRESH_SNAPSHOT_COLUMNS)
            or written.get("rows") != normalized_rows
            or written.get("applied_states") != expected_post
        ):
            raise RuntimeError("rollback snapshot verification mismatch")
        return str(final_path)

    def _load_backup(self, backup_path: str) -> dict:
        """Load and integrity-check a snapshot artifact; returns the full payload."""
        path = Path(str(backup_path))
        if path.is_symlink():
            raise ValueError("refusing to load a symlinked snapshot artifact")
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("artifact") != "gnc-img-03-sample-refresh-snapshot":
            raise ValueError("not a GNC-IMG-03 sample refresh snapshot artifact")
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError("snapshot artifact contains no rows")
        ids = [str(row.get("catalog_spec_id")) for row in rows]
        if any(not re.fullmatch(r"\d+", i) for i in ids):
            raise ValueError("snapshot artifact contains a non-numeric catalog_spec_id")
        if len(set(ids)) != len(ids):
            raise ValueError("snapshot artifact contains duplicate catalog_spec_id values")
        applied_states = payload.get("applied_states")
        if not isinstance(applied_states, dict):
            raise ValueError("snapshot artifact is missing per-row applied state")
        # Every applied row MUST carry the concrete post-write content_hash and
        # timestamp that the rollback guard compares against. Without them the
        # guard could compare against None and clobber a row whose live values are
        # NULL, so a malformed/tampered artifact is rejected outright.
        for spec_id, state in applied_states.items():
            if not re.fullmatch(r"\d+", str(spec_id)):
                raise ValueError("snapshot artifact has a non-numeric applied row id")
            if not isinstance(state, dict) or not isinstance(state.get("content_hash"), str) or not state["content_hash"]:
                raise ValueError("snapshot artifact applied row is missing its post-write content_hash")
            if self._canonical_timestamp(state.get("last_changed_at")) is None:
                raise ValueError("snapshot artifact applied row is missing a valid post-write last_changed_at")
        return payload

    def _merge_media_only(self, stored_data: dict, fresh_data: dict) -> dict:
        """Return ``stored_data`` with ONLY the media block replaced.

        Non-media fields (general_specification, specifications, identity and any
        other key already stored) are preserved byte-for-byte; a media subset that
        is missing from ``fresh_data`` is rejected so a partial parse cannot erase
        stored media.
        """
        if not isinstance(stored_data, dict):
            stored_data = {}
        media = fresh_data.get("media") if isinstance(fresh_data, dict) else None
        if not isinstance(media, dict):
            raise MediaUnresolved(retryable=False)
        for field in SAMPLE_REFRESH_MEDIA_FIELDS:
            if field not in media:
                raise MediaUnresolved(retryable=False)
        merged = dict(stored_data)
        merged["media"] = {field: media.get(field) for field in SAMPLE_REFRESH_MEDIA_FIELDS}
        return merged

    def _current_identity_guard(self, stored: dict) -> dict:
        """The compare-and-set identity asserted at write time (optimistic guard)."""
        return {
            "content_hash": stored.get("content_hash"),
            "last_changed_at": stored.get("last_changed_at"),
            "spec_url": stored.get("spec_url"),
        }

    def refresh_sample_specs(
        self,
        catalog_spec_ids,
        *,
        apply: bool = False,
        delay_seconds: float | None = None,
        min_interval_seconds: float = SAMPLE_REFRESH_MIN_INTERVAL_SECONDS,
        sleep_func=None,
    ) -> dict:
        """Bounded media-only refresh for existing specs; read-only unless apply.

        * ``catalog_spec_ids`` must name 1..3 explicit existing ids (raw argument
          length is capped before de-duplication).
        * The page URL is read from the stored row's ``spec_url`` only, and only
          after it validates as the exact canonical https detail URL for that id.
        * With ``apply=False`` (default) nothing is written: the plan is returned.
        * With ``apply=True`` a verified immutable snapshot of the exact pre-write
          rows is required first; any snapshot failure aborts BEFORE any write.
        * A spec whose media is unchanged does ZERO writes (no last_seen_at bump).
        * The changed rows are written in ONE guarded transaction (content_hash +
          last_changed_at + spec_url). A concurrent change on any row aborts the
          whole batch: NO row is written, so the snapshot's candidate set always
          equals the applied set (never a partial apply).
        * MediaUnresolved / 429 / 404 / any per-spec error leaves the row
          untouched and is reported; no bulk marks, run rows or missing flips.
        """
        ids = self._normalize_refresh_ids(catalog_spec_ids)
        min_interval = max(float(min_interval_seconds), 0.0)
        delay_from_cli = 0.0 if delay_seconds is None else float(delay_seconds)
        throttle = max(delay_from_cli, min_interval)
        sleeper = sleep_func or time.sleep

        stored_rows = self._load_spec_rows(ids)
        summary: dict = {
            "mode": "apply" if apply else "dry_run",
            "applied": bool(apply),
            "requested": ids,
            "found": [spec_id for spec_id in ids if spec_id in stored_rows],
            "missing": [spec_id for spec_id in ids if spec_id not in stored_rows],
            "refreshed": 0,
            "changed": 0,
            "unchanged": 0,
            "errors": [],
            "results": [],
            "snapshot_path": None,
        }

        plans: list[dict] = []
        for index, spec_id in enumerate(ids):
            stored = stored_rows.get(spec_id)
            if stored is None:
                summary["results"].append({"catalog_spec_id": spec_id, "status": "missing"})
                continue
            spec_url = self._validated_spec_url(stored.get("spec_url"), spec_id)
            if spec_url is None:
                summary["results"].append({"catalog_spec_id": spec_id, "status": "rejected_url"})
                self._record_sync_error(
                    summary, "refresh", None,
                    ValueError("stored spec_url is not the canonical detail URL for this id"),
                )
                continue
            # Throttle between distinct network fetches (not before the first).
            if index > 0:
                sleeper(throttle)
            try:
                fresh = self.parse_spec_detail(self.fetch_html(spec_url), dict(stored))
                merged = self._merge_media_only(stored.get("data") or {}, fresh)
            except MediaUnresolved as exc:
                summary["results"].append({"catalog_spec_id": spec_id, "status": "media_unresolved"})
                self._record_spec_detail_error(summary, "refresh", spec_url, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - per-spec isolation
                summary["results"].append({"catalog_spec_id": spec_id, "status": "error"})
                # Untrusted page content / request detail must never reach the
                # error record: log the exception TYPE only.
                self._record_sync_error(
                    summary, "refresh", spec_url,
                    RuntimeError(f"refresh failed ({type(exc).__name__})"),
                )
                continue
            new_hash = self.stable_spec_detail_hash(merged)
            changed = new_hash != stored.get("content_hash")
            plan = {
                "catalog_spec_id": spec_id,
                "spec_url": spec_url,
                "stored": stored,
                "merged": merged,
                "new_hash": new_hash,
                "changed": changed,
            }
            plans.append(plan)
            summary["results"].append(
                {
                    "catalog_spec_id": spec_id,
                    "status": "planned",
                    "changed": changed,
                    # Dry-run may echo the resolved media; apply never logs URLs.
                    "media": merged.get("media") if not apply else None,
                }
            )

        if not apply:
            summary["planned"] = len(plans)
            summary["planned_changed"] = sum(1 for plan in plans if plan["changed"])
            summary["status"] = "ok" if not summary["errors"] else "partial"
            return summary

        # ---- apply path: snapshot exact pre-write rows, then one atomic write ----
        written_plans = [plan for plan in plans if plan["changed"]]
        write_timestamp = datetime.now(timezone.utc)
        if written_plans:
            # ``applied_states`` names EXACTLY the rows this batch will write. The
            # batch runs as a single transaction that aborts on any guard failure,
            # so on success the applied set equals this candidate set and on
            # failure NOTHING is applied (never a partial/ambiguous apply). That is
            # what keeps the snapshot's recorded ids accurate without rewriting it.
            applied_states = {
                plan["catalog_spec_id"]: {
                    "content_hash": plan["new_hash"],
                    "last_changed_at": write_timestamp.isoformat(),
                }
                for plan in written_plans
            }
            summary["snapshot_path"] = self._write_rollback_backup(
                [dict(plan["stored"]) for plan in written_plans],
                applied_states=applied_states,
            )
        results_by_id = {item.get("catalog_spec_id"): item for item in summary["results"]}
        for plan in plans:
            if plan["changed"]:
                continue
            # Media identical to stored: make ZERO writes, not even last_seen_at.
            summary["unchanged"] += 1
            item = results_by_id.get(plan["catalog_spec_id"])
            if item is not None:
                item["status"] = "unchanged"
                item["applied"] = "unchanged"
        if written_plans:
            try:
                written = self._apply_sample_refresh_batch(written_plans, write_timestamp)
            except SampleRefreshConflict as exc:
                # The single transaction aborted: NO row in the batch was written.
                item = results_by_id.get(exc.catalog_spec_id)
                if item is not None:
                    item["status"] = "conflict"
                # Nothing was applied, so there is nothing to roll back; the
                # pre-write artifact is still retained on disk as a forensic record.
                summary["snapshot_path"] = None
                self._record_sync_error(
                    summary, "refresh_conflict", None,
                    RuntimeError("concurrent change detected; batch aborted, no rows written"),
                )
            except Exception as exc:  # noqa: BLE001 - a half-applied batch must be impossible
                summary["snapshot_path"] = None
                self._record_sync_error(
                    summary, "refresh", None,
                    RuntimeError(f"refresh batch failed ({type(exc).__name__}); no rows written"),
                )
            else:
                for spec_id in written:
                    item = results_by_id.get(spec_id)
                    if item is not None:
                        item["applied"] = "changed"
                summary["changed"] = len(written)
                summary["refreshed"] = len(written)
        applied = summary["changed"] + summary["unchanged"] + len(summary["errors"])
        summary["status"] = "partial" if summary["errors"] else ("success" if applied else "ok")
        return summary

    def _apply_sample_refresh_batch(self, plans: list[dict], write_timestamp: datetime) -> list[str]:
        """Apply every guarded media write in ONE all-or-nothing transaction.

        Returns the ids actually written (equal to ``plans`` on success). If any
        row is missing or fails the optimistic guard (content_hash +
        last_changed_at + spec_url) a ``SampleRefreshConflict`` is raised, which
        rolls the ENTIRE batch back: no row is partially applied, so the
        snapshot's recorded candidate set stays exactly equal to the applied set.
        ``write_timestamp`` is written verbatim (not ``NOW()``) so the rollback
        guard can compare it deterministically.
        """
        from psycopg2.extras import Json

        from scrap_goonet_cars_botasaurus.database import get_connection

        written: list[str] = []
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    for plan in plans:
                        spec_id = plan["catalog_spec_id"]
                        expected = self._current_identity_guard(plan["stored"])
                        cursor.execute(
                            """SELECT content_hash, last_changed_at, spec_url
                               FROM cars_scrap_goonet_catalog_specs
                               WHERE catalog_spec_id = %s FOR UPDATE""",
                            (spec_id,),
                        )
                        current = cursor.fetchone()
                        if current is None:
                            raise SampleRefreshConflict(spec_id)
                        current_hash, current_changed, current_url = current[0], current[1], current[2]
                        if (
                            current_hash != expected["content_hash"]
                            or current_url != expected["spec_url"]
                            or current_changed != expected["last_changed_at"]
                        ):
                            # A concurrent writer changed this row since the read;
                            # abort the whole batch rather than write a subset.
                            raise SampleRefreshConflict(spec_id)
                        # Guard passed and plan is "changed", so stored hash !=
                        # new_hash; this row is always written here.
                        cursor.execute(
                            """UPDATE cars_scrap_goonet_catalog_specs
                               SET data = %(data)s,
                                   content_hash = %(content_hash)s,
                                   last_changed_at = %(last_changed_at)s,
                                   last_seen_at = NOW(),
                                   status = 'active'
                               WHERE catalog_spec_id = %(catalog_spec_id)s""",
                            {
                                "data": Json(plan["merged"]),
                                "content_hash": plan["new_hash"],
                                "last_changed_at": write_timestamp,
                                "catalog_spec_id": spec_id,
                            },
                        )
                        written.append(spec_id)
        finally:
            conn.close()
        return written

    def rollback_sample_refresh(self, snapshot_path: str, *, apply: bool = False) -> dict:
        """Guarded, all-or-nothing restore of the pre-write rows from a snapshot.

        Read-only unless ``apply=True``. Every candidate row recorded in the
        snapshot is checked under ``FOR UPDATE`` against its post-write
        ``content_hash`` + ``last_changed_at``. If ANY candidate is missing or no
        longer matches (a concurrent edit), the WHOLE restore is aborted: the
        transaction is rolled back and ``SampleRefreshRollbackError`` is raised,
        so no row is partially restored. On success every candidate is restored
        in one transaction. Because the apply path is itself all-or-nothing, the
        snapshot's candidate set is exactly the set of rows that were applied.
        """
        payload = self._load_backup(snapshot_path)
        rows_by_id = {str(row["catalog_spec_id"]): row for row in payload["rows"]}
        applied_states = payload["applied_states"]
        if not isinstance(applied_states, dict) or not applied_states:
            raise ValueError("snapshot artifact has no applied rows to restore")

        plan = {
            "mode": "rollback_apply" if apply else "rollback_dry_run",
            "applied": bool(apply),
            "snapshot_path": str(snapshot_path),
            "candidates": sorted(applied_states),
            "restored": 0,
        }
        if not apply:
            plan["status"] = "ok"
            return plan

        from psycopg2.extras import Json

        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            with conn:  # one transaction: any failure rolls the WHOLE batch back
                with conn.cursor() as cursor:
                    for spec_id in sorted(applied_states):
                        row = rows_by_id.get(spec_id)
                        expected_post = applied_states.get(spec_id) or {}
                        if row is None:
                            # A candidate not present in the snapshot's own rows is
                            # corrupt/ambiguous -> never restore a partial batch.
                            raise SampleRefreshRollbackError(
                                f"rollback aborted: snapshot row missing for {spec_id}"
                            )
                        cursor.execute(
                            "SELECT content_hash, last_changed_at FROM cars_scrap_goonet_catalog_specs WHERE catalog_spec_id = %s FOR UPDATE",
                            (spec_id,),
                        )
                        current = cursor.fetchone()
                        if current is None:
                            raise SampleRefreshRollbackError(
                                f"rollback aborted: target row missing for {spec_id}"
                            )
                        current_hash = current[0]
                        current_changed = self._canonical_timestamp(current[1])
                        if (
                            current_hash != expected_post.get("content_hash")
                            or current_changed != self._canonical_timestamp(expected_post.get("last_changed_at"))
                        ):
                            # Someone else changed the row after our refresh (or the
                            # artifact does not describe our write): fail closed and
                            # never clobber; the whole restore is aborted.
                            raise SampleRefreshRollbackError(
                                f"rollback aborted: concurrent change on {spec_id}"
                            )
                        cursor.execute(
                            """UPDATE cars_scrap_goonet_catalog_specs
                               SET data = %(data)s,
                                   content_hash = %(content_hash)s,
                                   first_seen_at = %(first_seen_at)s,
                                   last_seen_at = %(last_seen_at)s,
                                   last_changed_at = %(last_changed_at)s,
                                   status = %(status)s,
                                   spec_url = %(spec_url)s
                               WHERE catalog_spec_id = %(catalog_spec_id)s""",
                            {
                                "data": Json(row.get("data") or {}),
                                "content_hash": row.get("content_hash"),
                                "first_seen_at": row.get("first_seen_at"),
                                "last_seen_at": row.get("last_seen_at"),
                                "last_changed_at": row.get("last_changed_at"),
                                "status": row.get("status"),
                                "spec_url": row.get("spec_url"),
                                "catalog_spec_id": spec_id,
                            },
                        )
                        plan["restored"] += cursor.rowcount
        finally:
            conn.close()
        plan["status"] = "success"
        return plan

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

        # GNC-IMG-12B: a stored spec whose data.media.images still carries a
        # `/thumb/` URL is NOT considered complete, even when data/content_hash
        # are otherwise complete (main_image already a non-thumb). sync_all then
        # re-visits it so the GNC-IMG-01 resolver can replace those thumb URLs
        # with validated non-thumb originals. The jsonb_typeof CASE guard keeps
        # jsonb_array_elements_text from raising on a malformed (non-array)
        # media.images: such rows have no `images` array, so they carry no thumb
        # and are treated exactly like the previous "no thumbs" complete rows.
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT catalog_spec_id FROM cars_scrap_goonet_catalog_specs
                       WHERE model_slug=%s
                         AND data <> '{}'::jsonb
                         AND content_hash IS NOT NULL
                         AND NOT EXISTS (
                           SELECT 1
                           FROM jsonb_array_elements_text(
                             CASE WHEN jsonb_typeof(data->'media'->'images') = 'array'
                                  THEN data->'media'->'images'
                                  ELSE '[]'::jsonb END
                           ) AS img(url)
                           WHERE strpos(img.url, '/thumb/') > 0
                         )""",
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

    def _record_spec_detail_error(self, summary: dict, stage: str, url: str | None, error: Exception) -> None:
        """Record a per-spec failure, distinguishing indeterminate media.

        ``MediaUnresolved`` means image resolution could not be established, so
        the spec was intentionally NOT written (its previously stored media is
        preserved and a first-time spec stays pending). It is reported under a
        distinct ``*_media`` stage with a sanitized message; the run stays
        ``partial`` rather than ``success`` so the monthly contract sees it.
        """
        if isinstance(error, MediaUnresolved):
            summary["media_unresolved"] = summary.get("media_unresolved", 0) + 1
            self._record_sync_error(summary, f"{stage}_media", url, error)
        else:
            self._record_sync_error(summary, stage, url, error)

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
                self._record_spec_detail_error(summary, "pending_details", spec.get("spec_url"), exc)
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
                   "missing_specs": 0, "media_unresolved": 0, "errors": []}
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
                            self._record_spec_detail_error(summary, "spec_details", spec.get("spec_url"), exc)
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
