"""GNC-FULL-03E-1: bounded, READ-ONLY inventory/classifier for empty-image specs.

Scope (deliberately minimal):

* Persist ONE private snapshot (dir 0700, file 0600, atomic temp+``os.replace``)
  of the candidate ``(catalog_spec_id, canonical spec_url)`` set, taken from a
  READ-ONLY DB transaction (``conn.set_session(readonly=True)``) on the FIRST
  run. Later runs LOAD AND VERIFY that snapshot instead of re-SELECTing, so the
  checkpoint's coverage always refers to a fixed, hash-verified id set and a
  concurrent raw change can never silently broaden or narrow it. The snapshot
  is integrity-checked (SHA-256 + canonical-URL shape) on every load and the
  runner refuses to scan a corrupt/poisoned snapshot. ``--snapshot-refresh``
  refuses to run against a non-empty checkpoint (it would silently mix two id
  sets); use a fresh ``--state-dir`` to restart.
* For each candidate make ONE bounded page fetch to its stored canonical detail
  URL plus, only for a gallery-looking page, at most one image probe per
  embedded slide (the probe is how the shipped resolver proves a non-thumb
  original). The page fetch's internal 429/5xx retry is forced to a SINGLE
  attempt for the whole scan (env restored afterwards) and ``probe_image_url``
  is already single-shot, so one budget unit == one real HTTP request. Every
  upstream request (page OR image) is counted against a single ``--max-requests``
  budget and separated by the same minimum ``--delay-seconds`` interval.
  Exhausting the budget STOPS the run and does NOT checkpoint the id it could not
  finish (unvisited, or visited-but-probe-starved), so a plain resume re-visits
  it; only fully resolved pages are ever appended to the checkpoint.
* Classify each page as exactly one of ``gallery_has_image`` /
  ``confirmed_no_gallery`` / ``unavailable_or_ambiguous``, preserving a private
  append-only fsync'd checkpoint so an interrupted scan resumes without
  re-fetching ids already classified.

Classification is conservative and MUST NOT misclassify:

* A ``gallery_has_image`` verdict requires (a) a proven gallery AND (b) a STRONG
  page self-identity verifying the expected spec id. A proven gallery is an
  EMBEDDED ``.gallery-main`` slide (base64/absent ``src``) whose own paired
  ``.gallery-thumbs`` entry derives the non-thumb candidate that ``_collect_media``
  returned among ``images``. A direct (non-embedded) slide URL, a page-level
  generic/fallback ``carphoto``/``catalogphoto`` node, or an embedded slide whose
  OWN thumb is missing never count.
* A ``confirmed_no_gallery`` verdict additionally requires a successfully parsed
  detail page whose identity is STRONGLY verified against the expected spec id.
  A page that self-declares a DIFFERENT id (``identity_mismatch``) or that cannot
  be verified (``identity_unverified``) stays ambiguous — a wrong-spec page is
  never reported as a gallery-less car.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scrap_goonet_catalog_botasaurus.catalog_service import (
    SPEC_REFRESH_ALLOWED_HOST,
    SPEC_REFRESH_PATH_RE,
    GoonetCatalogService,
    MediaUnresolved,
)

# Only the canonical catalog host may declare a page's identity.
_TRUSTED_LOCATOR_HOSTS = (SPEC_REFRESH_ALLOWED_HOST,)

# Candidate snapshot predicate. ``COALESCE`` keeps the predicate safe on rows
# missing the media block; a JSON ``null`` main_image and an empty ``images``
# array are the exact "empty image" symptom this inventory targets. EXECUTED IN
# A READ-ONLY TRANSACTION only.
CANDIDATE_SQL = """
    SELECT catalog_spec_id, spec_url
    FROM cars_scrap_goonet_catalog_specs
    WHERE status = 'active'
      AND COALESCE(data -> 'media' -> 'main_image', 'null'::jsonb) = 'null'::jsonb
      AND COALESCE(data -> 'media' -> 'images', '[]'::jsonb) = '[]'::jsonb
    ORDER BY catalog_spec_id
"""

CATEGORY_GALLERY = "gallery_has_image"
CATEGORY_NO_GALLERY = "confirmed_no_gallery"
CATEGORY_UNAVAILABLE = "unavailable_or_ambiguous"
CATEGORIES = (CATEGORY_GALLERY, CATEGORY_NO_GALLERY, CATEGORY_UNAVAILABLE)
_CATEGORY_SUMMARY_KEY = {
    CATEGORY_GALLERY: "gallery_has_image",
    CATEGORY_NO_GALLERY: "confirmed_no_gallery",
    CATEGORY_UNAVAILABLE: "unavailable_or_ambiguous",
}

# Same "photo-ish" fallback selector used by ``_collect_media``. Its presence
# WITHOUT an embedded, identity-verified gallery is never proof of a car gallery.
PHOTO_NODE_SELECTOR = (
    "img[src*='carphoto'], img[data-src*='carphoto'], a[href*='carphoto'], "
    "img[src*='catalogphoto'], img[data-src*='catalogphoto'], a[href*='catalogphoto']"
)

# Strong page self-identity markers (the page's OWN canonical URL). These are
# self-declarations, so a page for a DIFFERENT spec reveals a mismatching id and
# is never trusted as the expected spec.
SELF_LOCATOR_SELECTOR = (
    "link[rel='canonical'][href], link[rel=canonical][href], "
    "meta[property='og:url'][content], meta[name='twitter:url'][content]"
)

STATE_DIRNAME = "cars_scrap_goonet_catalog_image_inventory"
SNAPSHOT_FILENAME = "snapshot.json"
CHECKPOINT_FILENAME = "inventory.jsonl"
SNAPSHOT_ARTIFACT = "gnc-full-03e1-image-inventory-snapshot"
MIN_INTER_REQUEST_SECONDS = 0.2
# ``fetch_html`` retries 429/5xx up to this env's value (default 2). The inventory
# forces it to 1 for the whole scan so one ``budget.before("page")`` == one real
# HTTP request, then restores the caller's value. ``probe_image_url`` already
# issues a single ``requests.get`` (no retry loop), so it is one request too.
CATALOG_MAX_ATTEMPTS_ENV = "GOONET_CATALOG_MAX_ATTEMPTS"
FORCED_MAX_ATTEMPTS = "1"


class SnapshotIntegrityError(RuntimeError):
    """The persisted candidate snapshot is missing, corrupt or unverifiable.

    Raised so the runner fails closed rather than scanning an id set it cannot
    trust (an incomplete/edited snapshot must never masquerade as coverage).
    """


class RequestBudgetExceeded(RuntimeError):
    """A hard ``--max-requests`` cap was hit; the run stops without further I/O."""


def _normalize_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _rows_digest(rows: list) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _SourceBudget:
    """Counts every upstream request and enforces one shared delay/budget.

    The page fetch and each image probe pass through ``before`` so the delay
    spaces ALL real source requests (not just page fetches) and the hard
    ``max_requests`` cap fails closed before any over-budget request is sent.
    """

    def __init__(self, delay: float, max_requests: int = 0, sleeper=None) -> None:
        self.delay = max(float(delay or 0.0), 0.0)
        self.max_requests = max(int(max_requests or 0), 0)
        self.sleeper = sleeper or time.sleep
        self.total = 0
        self.page_fetches = 0
        self.image_probes = 0

    def before(self, kind: str) -> None:
        if self.max_requests and self.total >= self.max_requests:
            raise RequestBudgetExceeded(
                f"source request budget of {self.max_requests} reached"
            )
        if self.total > 0 and self.delay > 0:
            self.sleeper(self.delay)
        self.total += 1
        if kind == "page":
            self.page_fetches += 1
        else:
            self.image_probes += 1


class ImageInventoryClassifier:
    """Read-only verdict on a fetched detail page (no DB, no writes).

    ``classify_html`` returns ``(category, reason, images_count)`` and never
    raises for page-specific problems (a hard request-budget stop is re-raised so
    the runner can halt).
    """

    def __init__(self, service: GoonetCatalogService | None = None) -> None:
        self.service = service or GoonetCatalogService()

    def _model_slug_from_detail_url(self, spec_url: str) -> str | None:
        """Derive the ``MODEL__SLUG`` path segment of a detail URL (or None)."""
        parts = self.service._catalog_path_parts(spec_url)
        if len(parts) == 2 and "__" in parts[0]:
            return parts[0]
        return None

    def classify_html(self, html: str, spec_url: str) -> tuple[str, str, int]:
        service = self.service
        expected_id = service.extract_catalog_spec_id(spec_url)
        model_slug = self._model_slug_from_detail_url(spec_url)
        try:
            soup = BeautifulSoup(html or "", "html.parser")
        except Exception as exc:  # noqa: BLE001 - never let one page abort the scan
            return CATEGORY_UNAVAILABLE, f"html_unparsed_{type(exc).__name__}", 0

        strong_identity, identity_reason = self._verify_identity(soup, expected_id, model_slug)
        slides = soup.select(".gallery-main img")
        thumbs = soup.select(".gallery-thumbs img")
        photo_nodes = soup.select(PHOTO_NODE_SELECTOR)

        if slides:
            try:
                media = service._collect_media(soup, spec_url)
            except RequestBudgetExceeded:
                raise
            except MediaUnresolved as exc:
                return (
                    CATEGORY_UNAVAILABLE,
                    "gallery_media_transient" if exc.retryable else "gallery_media_invalid",
                    0,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed to ambiguous
                return CATEGORY_UNAVAILABLE, f"gallery_media_error_{type(exc).__name__}", 0
            images = media.get("images") or []
            proven, gallery_reason = self._embedded_gallery_proven(slides, thumbs, images, spec_url)
            if proven and strong_identity:
                return CATEGORY_GALLERY, gallery_reason, len(images)
            if proven and not strong_identity:
                # A proven gallery on a page we cannot tie to the expected spec id
                # (id-less generic page or a wrong-spec page): never claim it here.
                return CATEGORY_UNAVAILABLE, f"gallery_{identity_reason}", len(images)
            return CATEGORY_UNAVAILABLE, "gallery_unproven", len(images)

        if photo_nodes:
            # Fallback photo nodes without an embedded gallery: needs review.
            return CATEGORY_UNAVAILABLE, "photo_nodes_without_gallery", 0
        if strong_identity:
            # A successfully parsed, identity-verified detail page with zero
            # gallery/photo candidate.
            return CATEGORY_NO_GALLERY, "detail_identity_ok_no_gallery", 0
        # No gallery and no reliable spec-id identity: a wrong-spec or generic
        # error page must NOT be reported as a gallery-less car.
        return CATEGORY_UNAVAILABLE, identity_reason, 0

    def _embedded_gallery_proven(
        self, slides: list, thumbs: list, images: list, base_url: str
    ) -> tuple[bool, str]:
        """True only when an embedded slide's OWN thumb derived a validated image.

        ``_collect_media`` pairs ``slides[index]`` with ``thumbs[index]``; we
        re-check that exact pairing so a resolved image from a DIFFERENT (direct)
        slide can never be attributed to an embedded slide.
        """
        service = self.service
        image_set = set(images)
        for index, slide in enumerate(slides):
            if service._media_src(slide) is not None:
                continue  # a direct slide carries its URL; not an embedded base64 slide
            thumb_node = thumbs[index] if index < len(thumbs) else None
            thumb_url = service._absolute_media_url(thumb_node, base_url)
            if not thumb_url or not service._is_thumb_url(thumb_url):
                continue
            candidate = service._thumb_to_nonthumb(thumb_url)
            if candidate and candidate in image_set:
                return True, "embedded_slide_thumb_probe_valid"
        return False, "gallery_unproven"

    def _verify_identity(
        self, soup: BeautifulSoup, expected_id: str | None, model_slug: str | None
    ) -> tuple[bool, str]:
        """Strongly verify the page belongs to ``expected_id``; else fail closed.

        Strong evidence is the page's OWN declared identity — a canonical /
        ``og:url`` / ``twitter:url`` locator whose exact ``/catalog/<MODEL>/<id>/``
        path matches BOTH the expected spec id and model slug. This is strictly a
        self-declaration: page BODY links (breadcrumbs, related/other-spec links)
        are deliberately NOT trusted, so a wrong-spec or id-less page can never
        yield a gallery/no-gallery claim. A self-declaration of a different id is
        ``identity_mismatch``; no usable declaration is ``identity_unverified``.
        """
        if not expected_id:
            return False, "identity_unverified"
        declared = self._locator_pairs(self._self_locator_urls(soup))
        if declared:
            if any(spec_id == expected_id and slug == model_slug for spec_id, slug in declared):
                return True, "id_marker_match"
            # The page declares a canonical id that is NOT the expected one.
            return False, "identity_mismatch"
        # Weak corroboration only (never enough for a claim, but lets the report
        # tell "looks like the right model, id unproven" apart from "unknown page").
        container = soup.select_one(".detail-right")
        name = None
        if container is not None:
            name = self.service.clean_text(container.select_one(".name-car") or container.select_one("h1"))
        token = _normalize_text(self.service._model_name_from_slug(model_slug))
        if container is not None and name and token and token in _normalize_text(name):
            return False, "identity_name_only"
        return False, "identity_unverified"

    def _self_locator_urls(self, soup: BeautifulSoup) -> list:
        urls: list = []
        for node in soup.select(SELF_LOCATOR_SELECTOR):
            value = node.get("href") or node.get("content")
            if value:
                urls.append(value)
        return urls

    @staticmethod
    def _locator_pairs(urls: list) -> list:
        """Extract ``(spec_id, model_slug)`` from trusted self-declaration URLs.

        Only an absolute ``https`` URL on the trusted Goonet host is accepted, so
        an injected/off-host canonical link cannot spoof the expected identity.
        """
        pairs: list = []
        for url in urls:
            try:
                parts = urlparse(str(url or ""))
            except ValueError:
                continue
            if parts.scheme != "https" or parts.hostname not in _TRUSTED_LOCATOR_HOSTS:
                continue
            if parts.username or parts.password or parts.port or parts.query or parts.fragment:
                continue
            match = SPEC_REFRESH_PATH_RE.match(parts.path)
            if match:
                pairs.append((match.group("spec_id"), match.group("model")))
        return pairs


class ImageInventoryRunner:
    """Bounded read-only scan; on-disk artifacts are the snapshot + checkpoint."""

    def __init__(
        self,
        service: GoonetCatalogService | None = None,
        classifier: ImageInventoryClassifier | None = None,
        state_dir: str | os.PathLike | None = None,
        sleep_func=None,
        logger=None,
    ) -> None:
        self.service = service or GoonetCatalogService()
        self.classifier = classifier or ImageInventoryClassifier(self.service)
        self._state_dir = (
            Path(state_dir)
            if state_dir is not None
            else Path.home() / ".local" / "state" / STATE_DIRNAME
        )
        self._sleep = sleep_func or time.sleep
        self.logger = logger if logger is not None else self.service.logger

    # -- paths ----------------------------------------------------------
    def snapshot_path(self) -> Path:
        return self._state_dir / SNAPSHOT_FILENAME

    def checkpoint_path(self) -> Path:
        return self._state_dir / CHECKPOINT_FILENAME

    # -- candidate snapshot (read-only DB, private, atomic, verified) ---
    def snapshot_candidates(self, refresh: bool = False) -> list:
        """Return the persisted candidate snapshot, creating it once if needed.

        The initial snapshot is written atomically (temp + ``os.replace``), is
        private (dir 0700 / file 0600) and is integrity-verified on every load.
        """
        path = self.snapshot_path()
        if path.exists() and not refresh:
            return self.load_snapshot()
        rows = self._select_candidates_readonly()
        self._write_snapshot(rows)
        # Re-read to prove the artifact round-trips before it is trusted.
        return self.load_snapshot()

    def _select_candidates_readonly(self) -> list:
        from scrap_goonet_cars_botasaurus.database import get_connection

        conn = get_connection()
        try:
            # READ-ONLY transaction: this tool must never write the raw DB.
            conn.set_session(readonly=True)
            with conn.cursor() as cursor:
                cursor.execute(CANDIDATE_SQL)
                rows = [
                    {"catalog_spec_id": str(row[0]), "spec_url": row[1]}
                    for row in cursor.fetchall()
                ]
            conn.rollback()
            return rows
        finally:
            conn.close()

    def _write_snapshot(self, rows: list) -> None:
        normalized = sorted(
            (
                {"catalog_spec_id": str(row["catalog_spec_id"]), "spec_url": row["spec_url"]}
                for row in rows
            ),
            key=lambda item: item["catalog_spec_id"],
        )
        payload = {
            "artifact": SNAPSHOT_ARTIFACT,
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "count": len(normalized),
            "sha256": _rows_digest(normalized),
            "rows": normalized,
        }
        directory = self._state_dir
        self.service._ensure_secure_dir(directory)
        final_path = self.snapshot_path()
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".snapshot_", suffix=".tmp", dir=str(directory))
        try:
            os.fchmod(tmp_fd, 0o600)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, default=str)
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

    def load_snapshot(self) -> list:
        """Load and integrity-verify the snapshot; fail closed on anything odd."""
        path = self.snapshot_path()
        if path.is_symlink():
            raise SnapshotIntegrityError("refusing to load a symlinked snapshot")
        if not path.exists():
            raise SnapshotIntegrityError("candidate snapshot is missing")
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("artifact") != SNAPSHOT_ARTIFACT:
            raise SnapshotIntegrityError("not a GNC-FULL-03E-1 snapshot artifact")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise SnapshotIntegrityError("snapshot has no rows list")
        if payload.get("count") != len(rows):
            raise SnapshotIntegrityError("snapshot count does not match its rows")
        if _rows_digest(rows) != payload.get("sha256"):
            raise SnapshotIntegrityError("snapshot SHA-256 mismatch (corrupt or edited)")
        seen: set = set()
        for row in rows:
            spec_id = str(row.get("catalog_spec_id") or "")
            if not re.fullmatch(r"\d+", spec_id):
                raise SnapshotIntegrityError("snapshot has a non-numeric catalog_spec_id")
            if spec_id in seen:
                raise SnapshotIntegrityError("snapshot has duplicate catalog_spec_id values")
            seen.add(spec_id)
            if self.service._validated_spec_url(row.get("spec_url"), spec_id) is None:
                raise SnapshotIntegrityError(
                    "snapshot row is not the canonical detail URL for its id"
                )
        return rows

    # -- checkpoint -----------------------------------------------------
    def load_checkpoint(self) -> dict:
        """Read recorded ids; a torn trailing line is ignored, never guessed."""
        return self._read_checkpoint()[0]

    def _read_checkpoint(self) -> tuple:
        """Return ``(classified_by_id, valid_byte_end)``.

        Only complete newline-terminated JSON lines count. ``valid_byte_end`` is
        the byte offset just past the last complete line, so a torn trailing
        record (crash mid-append) can be truncated before the next append instead
        of gluing it onto a new record and corrupting the whole tail.
        """
        path = self.checkpoint_path()
        classified: dict = {}
        valid_end = 0
        if not path.exists():
            return classified, valid_end
        with path.open("rb") as handle:
            offset = 0
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
                spec_id = str(record.get("catalog_spec_id") or "")
                offset += len(raw_line)
                if spec_id:
                    classified[spec_id] = record
                    valid_end = offset
        return classified, valid_end

    def _truncate_checkpoint(self, valid_end: int) -> None:
        """Drop a torn trailing record so later appends stay well-formed."""
        path = self.checkpoint_path()
        if not path.exists():
            return
        with path.open("r+b") as handle:
            handle.truncate(valid_end)
            handle.flush()
            os.fsync(handle.fileno())

    def _append_checkpoint(self, record: dict) -> None:
        path = self.checkpoint_path()
        self.service._ensure_secure_dir(path.parent)
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if (path.stat().st_mode & 0o777) != 0o600:
            os.chmod(path, 0o600)

    # -- scan -----------------------------------------------------------
    def run(
        self,
        *,
        limit: int = 0,
        max_seconds: float = 0.0,
        delay_seconds: float = MIN_INTER_REQUEST_SECONDS,
        max_requests: int = 0,
        refresh_snapshot: bool = False,
    ) -> dict:
        """Classify up to ``limit`` / ``max_seconds`` / ``max_requests``; resumable."""
        delay = max(float(delay_seconds or 0.0), MIN_INTER_REQUEST_SECONDS)
        max_ids = max(int(limit or 0), 0)
        time_budget = max(float(max_seconds or 0.0), 0.0)
        started = time.monotonic()

        # A snapshot refresh would silently mix classifications taken against the
        # OLD id set with the NEW one, so refuse unless the checkpoint is empty.
        existing, valid_end = self._read_checkpoint()
        if refresh_snapshot and existing:
            raise RuntimeError(
                "--snapshot-refresh with a non-empty checkpoint would mix two id "
                "sets; use a fresh --state-dir instead"
            )
        candidates = self.snapshot_candidates(refresh=refresh_snapshot)
        # Repair a torn trailing record left by a crash before appending new
        # records, so the checkpoint tail can never become corrupt.
        self._truncate_checkpoint(valid_end)

        budget = _SourceBudget(delay, max_requests, self._sleep)
        summary = {
            "mode": "read_only_inventory",
            "candidates": len(candidates),
            "already_classified": 0,
            "classified": 0,
            "classified_total": 0,
            "requests": 0,
            "image_probes": 0,
            "source_requests": 0,
            "gallery_has_image": 0,
            "confirmed_no_gallery": 0,
            "unavailable_or_ambiguous": 0,
            "reasons": {},
            "remaining_in_snapshot": 0,
            "status": "complete",
            "stopped_before_id": None,
            "stopped_during_id": None,
            "snapshot_path": str(self.snapshot_path()),
            "checkpoint_path": str(self.checkpoint_path()),
        }

        # Patch every service object the fetch/probe path can reach, so BOTH the
        # page fetch and any image probe are counted/throttled by one budget. Each
        # target delegates to ITS OWN original attribute (they may differ).
        patch_targets = []
        for candidate_service in (self.service, getattr(self.classifier, "service", None)):
            if candidate_service is not None and candidate_service not in patch_targets:
                patch_targets.append(candidate_service)
        originals: list = []
        for target in patch_targets:
            fetch_orig = target.fetch_html
            probe_orig = target.probe_image_url
            originals.append((target, fetch_orig, probe_orig))

            def make_fetch(orig):
                def _fetch(url):
                    budget.before("page")
                    return orig(url)
                return _fetch

            def make_probe(orig):
                def _probe(url):
                    budget.before("probe")
                    return orig(url)
                return _probe

            target.fetch_html = make_fetch(fetch_orig)  # type: ignore[assignment]
            target.probe_image_url = make_probe(probe_orig)  # type: ignore[assignment]

        # Force ONE real HTTP request per page fetch (no internal 429/5xx retries)
        # for the whole scan, so the request budget equals real source requests.
        previous_attempts = os.environ.get(CATALOG_MAX_ATTEMPTS_ENV)
        os.environ[CATALOG_MAX_ATTEMPTS_ENV] = FORCED_MAX_ATTEMPTS
        try:
            for candidate in candidates:
                spec_id = candidate["catalog_spec_id"]
                if spec_id in existing:
                    summary["already_classified"] += 1
                    continue
                if max_ids and summary["classified"] >= max_ids:
                    summary["status"] = "capped"
                    break
                if time_budget and (time.monotonic() - started) >= time_budget:
                    summary["status"] = "capped"
                    break
                spec_url = self.service._validated_spec_url(candidate.get("spec_url"), spec_id)
                if spec_url is None:
                    self._record(summary, spec_id, CATEGORY_UNAVAILABLE, "invalid_spec_url", 0)
                    continue
                try:
                    html = self.service.fetch_html(spec_url)
                except RequestBudgetExceeded as exc:
                    # Budget spent BEFORE this page was requested: the id is
                    # UNVISITED, so do NOT checkpoint it (a resume must retry it).
                    self.logger.warning(
                        "Request budget exhausted before %s; not checkpointed: %s", spec_id, exc
                    )
                    summary["stopped_before_id"] = spec_id
                    summary["status"] = "capped"
                    break
                except Exception as exc:  # noqa: BLE001 - per-spec isolation; type only
                    self._record(
                        summary, spec_id, CATEGORY_UNAVAILABLE, f"fetch_failed_{type(exc).__name__}", 0
                    )
                    continue
                try:
                    category, reason, images_count = self.classifier.classify_html(html, spec_url)
                except RequestBudgetExceeded as exc:
                    # Budget spent mid-probe: leave the id UNCLASSIFIED (no partial
                    # ambiguous checkpoint) so a resume re-visits it wholly.
                    self.logger.warning(
                        "Request budget exhausted during %s; not checkpointed: %s", spec_id, exc
                    )
                    summary["stopped_during_id"] = spec_id
                    summary["status"] = "capped"
                    break
                self._record(summary, spec_id, category, reason, images_count)
        except KeyboardInterrupt:
            # Checkpoint is already durable per record; report an interrupted scan.
            summary["status"] = "interrupted"
        finally:
            for target, fetch_orig, probe_orig in originals:
                target.fetch_html = fetch_orig  # type: ignore[assignment]
                target.probe_image_url = probe_orig  # type: ignore[assignment]
            if previous_attempts is None:
                os.environ.pop(CATALOG_MAX_ATTEMPTS_ENV, None)
            else:
                os.environ[CATALOG_MAX_ATTEMPTS_ENV] = previous_attempts

        summary["requests"] = budget.page_fetches
        summary["image_probes"] = budget.image_probes
        summary["source_requests"] = budget.total
        summary["classified_total"] = summary["already_classified"] + summary["classified"]
        summary["remaining_in_snapshot"] = (
            summary["candidates"] - summary["already_classified"] - summary["classified"]
        )
        self.logger.info(
            "Inventory scan status=%s candidates=%s classified=%s page_fetches=%s probes=%s "
            "gallery=%s no_gallery=%s ambiguous=%s remaining=%s",
            summary["status"],
            summary["candidates"],
            summary["classified"],
            summary["requests"],
            summary["image_probes"],
            summary["gallery_has_image"],
            summary["confirmed_no_gallery"],
            summary["unavailable_or_ambiguous"],
            summary["remaining_in_snapshot"],
        )
        return summary

    def _record(self, summary: dict, spec_id: str, category: str, reason: str, images_count: int) -> None:
        record = {
            "catalog_spec_id": spec_id,
            "category": category,
            "reason": reason,
            "images_count": int(images_count),
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        self._append_checkpoint(record)
        summary["classified"] += 1
        summary[_CATEGORY_SUMMARY_KEY[category]] += 1
        summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Goonet catalog empty-image inventory/classifier (no DB/scrape writes)"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Max ids to classify this run (0 = all candidates)"
    )
    parser.add_argument(
        "--max-seconds", type=float, default=0.0, help="Wall-clock budget in seconds (0 = none)"
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=0,
        help="Hard cap on total source requests (page fetches + image probes; 0 = none)",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=MIN_INTER_REQUEST_SECONDS,
        help=f"Delay between ALL source requests in seconds (enforced minimum {MIN_INTER_REQUEST_SECONDS})",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help=f"Checkpoint/snapshot directory (default: ~/.local/state/{STATE_DIRNAME})",
    )
    parser.add_argument(
        "--snapshot-refresh",
        action="store_true",
        help="Deliberately re-take the candidate id snapshot from the raw DB (read-only)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    runner = ImageInventoryRunner(state_dir=args.state_dir)
    summary = runner.run(
        limit=args.limit,
        max_seconds=args.max_seconds,
        delay_seconds=args.delay_seconds,
        max_requests=args.max_requests,
        refresh_snapshot=args.snapshot_refresh,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
