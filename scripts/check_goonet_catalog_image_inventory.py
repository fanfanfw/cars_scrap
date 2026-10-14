"""Offline check for the GNC-FULL-03E-1 read-only image-inventory runner.

No network, no PostgreSQL, no filesystem writes outside a temp dir. Proves:

* the candidate snapshot is persisted once (read-only txn), is private/atomic,
  is integrity-verified on load, and resumes without re-SELECTing;
* a corrupt or poisoned snapshot fails closed;
* ``gallery_has_image`` requires an embedded slide whose OWN thumb derives the
  returned image AND a page that self-declares the expected spec id;
* an image from a different slide, a fallback node, a wrong-spec page, or an
  id-less page is never called a gallery, and a wrong-spec/id-less page never
  becomes ``confirmed_no_gallery``;
* the request budget counts page fetches AND image probes, spaces them with the
  delay, and fails closed at the hard cap;
* the checkpoint is private, resumable, and repairs a torn trailing line;
* only read-only SELECTs touch the DB.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrap_goonet_catalog_botasaurus import catalog_service, image_inventory  # noqa: E402
from scrap_goonet_catalog_botasaurus.catalog_service import GoonetCatalogService  # noqa: E402
from scrap_goonet_catalog_botasaurus.image_inventory import (  # noqa: E402
    CATEGORY_GALLERY,
    CATEGORY_NO_GALLERY,
    CATEGORY_UNAVAILABLE,
    ImageInventoryClassifier,
    ImageInventoryRunner,
    SnapshotIntegrityError,
)

NONTHUMB = "https://catalogphoto.goo-net.com/carphoto/30201526_201811z.jpg"
OTHER = "https://catalogphoto.goo-net.com/carphoto/30201526_201811zother.jpg"
THUMB = "https://catalogphoto.goo-net.com/carphoto/thumb/30201526_201811z.jpg"
_ID = "1000192"


def _url(sid: str) -> str:
    return f"https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/{sid}/"


def _page(body: str, *, declared_id: str | None = _ID) -> str:
    head = f"<link rel='canonical' href='{_url(declared_id)}'>" if declared_id else ""
    return f"<html><head>{head}</head><body>{body}</body></html>"


EMBEDDED = "<div class='gallery-main'><img src='data:image/jpeg;base64,AAAA'></div>"
THUMBS = f"<div class='gallery-thumbs'><img src='{THUMB}'></div>"


def test_classifier_adversarial() -> None:
    service = GoonetCatalogService()
    classifier = ImageInventoryClassifier(service)
    service.probe_image_url = lambda url: (
        catalog_service.MEDIA_VALID if url in (NONTHUMB, OTHER) else catalog_service.MEDIA_INVALID
    )

    # Proven embedded gallery + id self-declaration => gallery.
    assert classifier.classify_html(_page(EMBEDDED + THUMBS), _url(_ID)) == (
        CATEGORY_GALLERY, "embedded_slide_thumb_probe_valid", 1,
    )

    # An image that is NOT the embedded slide's own thumb candidate is never
    # attributed to it, even if _collect_media returned it.
    with unittest.mock.patch.object(
        service, "_collect_media", lambda soup, base: {"thumbnails": [], "images": [OTHER]}
    ):
        cat, reason, _ = classifier.classify_html(_page(EMBEDDED + THUMBS), _url(_ID))
        assert (cat, reason) == (CATEGORY_UNAVAILABLE, "gallery_unproven"), (cat, reason)

    # Fallback node (no gallery) and a direct-only slide are not galleries.
    bodies = (
        f"<div class='hero'><img src='{NONTHUMB}'></div>",
        f"<div class='gallery-main'><img src='{NONTHUMB}'></div>",
    )
    for body in bodies:
        cat, reason, _ = classifier.classify_html(_page(body), _url(_ID))
        assert cat == CATEGORY_UNAVAILABLE, (cat, reason)

    # A real gallery that self-declares a DIFFERENT id, and one that declares no
    # id, are both rejected for this spec.
    cat, reason, _ = classifier.classify_html(_page(EMBEDDED + THUMBS, declared_id="99999999"), _url(_ID))
    assert (cat, reason) == (CATEGORY_UNAVAILABLE, "gallery_identity_mismatch"), (cat, reason)
    cat, reason, _ = classifier.classify_html(_page(EMBEDDED + THUMBS, declared_id=None), _url(_ID))
    assert cat == CATEGORY_UNAVAILABLE and reason.startswith("gallery_"), (cat, reason)

    # Identity-verified, no gallery/photo nodes => credible no-gallery.
    identity = "<div class='detail-right'><h1 class='name-car'>TOYOTA ALPHARD Z</h1></div>"
    assert classifier.classify_html(_page(identity), _url(_ID)) == (
        CATEGORY_NO_GALLERY, "detail_identity_ok_no_gallery", 0,
    )

    # Wrong-spec 200 page and id-less generic error/h1 page => ambiguous, NEVER
    # confirmed_no_gallery.
    for body, declared in (("<h1>NOT FOUND</h1>", "99999999"), ("<h1>503 Service Unavailable</h1>", None)):
        cat, reason, _ = classifier.classify_html(_page(body, declared_id=declared), _url(_ID))
        assert cat == CATEGORY_UNAVAILABLE and reason in ("identity_mismatch", "identity_unverified"), (cat, reason)

    # Transient probe => ambiguous.
    service.probe_image_url = lambda url: catalog_service.MEDIA_TRANSIENT
    assert classifier.classify_html(_page(EMBEDDED + THUMBS), _url(_ID))[0:2] == (
        CATEGORY_UNAVAILABLE, "gallery_media_transient",
    )

    # Names only (no id) => ambiguous, not no-gallery.
    assert classifier.classify_html(_page(identity, declared_id=None), _url(_ID))[0:2] == (
        CATEGORY_UNAVAILABLE, "identity_name_only",
    )


class _Cursor:
    def __init__(self, rows, executed):
        self._rows, self._executed, self._result = rows, executed, None

    def execute(self, statement, params=None):
        self._executed.append(statement)
        assert " ".join(statement.split()).lower().startswith("select"), statement
        self._result = list(self._rows)

    def fetchall(self):
        return list(self._result or [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self, rows, executed, sessions):
        self._rows, self._executed, self._sessions = rows, executed, sessions

    def set_session(self, readonly=False, **kw):
        self._sessions.append(readonly)

    def rollback(self):
        return None

    def cursor(self, *a, **k):
        return _Cursor(self._rows, self._executed)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None


def _rows(n=3):
    return [(f"1000000{i}", _url(f"1000000{i}")) for i in range(1, n + 1)]


class _Env:
    """Context: patch DB factory + fetch_html; yields (runner, executed, sessions)."""

    def __init__(self, state_dir, rows, fetch, service, classifier):
        self.state_dir, self.rows, self.fetch = state_dir, rows, fetch
        self.service, self.classifier = service, classifier
        self.executed: list = []
        self.sessions: list = []

    def __enter__(self):
        db_mod = __import__("scrap_goonet_cars_botasaurus.database", fromlist=["get_connection"])
        self._p1 = unittest.mock.patch.object(
            db_mod, "get_connection", lambda: _Conn(self.rows, self.executed, self.sessions)
        )
        self._p1.start()
        if self.fetch is not None:
            self._p2 = unittest.mock.patch.object(self.service, "fetch_html", self.fetch)
            self._p2.start()
        else:
            self._p2 = None
        self.runner = ImageInventoryRunner(
            service=self.service, classifier=self.classifier, state_dir=self.state_dir
        )
        return self

    def __exit__(self, *exc):
        if self._p2 is not None:
            self._p2.stop()
        self._p1.stop()
        return False


def _service(probe=CATEGORY_GALLERY):
    service = GoonetCatalogService()
    service.probe_image_url = lambda url: catalog_service.MEDIA_VALID
    return service


def test_snapshot_persisted_once_readonly_and_verified() -> None:
    service = _service()
    no_photo = lambda url: _page("<div class='detail-right'><h1>X</h1></div>")
    with tempfile.TemporaryDirectory() as tmp:
        env = _Env(Path(tmp) / "inv", _rows(3), no_photo, service, ImageInventoryClassifier(service))
        with env as e:
            first = e.runner.snapshot_candidates()
            assert [r["catalog_spec_id"] for r in first] == ["10000001", "10000002", "10000003"]
            assert len(e.executed) == 1 and e.sessions == [True], (e.executed, e.sessions)  # read-only txn
            snap = e.runner.snapshot_path()
            assert (snap.parent.stat().st_mode & 0o777) == 0o700 and (snap.stat().st_mode & 0o777) == 0o600
            # Second load must NOT re-SELECT.
            assert e.runner.snapshot_candidates() == first and len(e.executed) == 1

            # Corrupt rows (count fixed) -> digest guard; count mismatch -> count guard.
            payload = json.loads(snap.read_text())
            payload["rows"].append({"catalog_spec_id": "10000004", "spec_url": _url("10000004")})
            payload["count"] = len(payload["rows"])
            for bad_payload, needle in ((payload, "SHA-256"), (None, "count")):
                if bad_payload is None:
                    p = json.loads(snap.read_text())
                    p["rows"].append({"catalog_spec_id": "10000005", "spec_url": _url("10000005")})
                else:
                    p = bad_payload
                snap.write_text(json.dumps(p))
                try:
                    e.runner.load_snapshot()
                    raise AssertionError("expected SnapshotIntegrityError")
                except SnapshotIntegrityError as exc:
                    assert needle in str(exc), exc

            # Poisoned (non-canonical URL) row fails closed even with a valid digest.
            bad_row = [{"catalog_spec_id": "1", "spec_url": "https://evil.example.com/x"}]
            snap.write_text(json.dumps({
                "artifact": image_inventory.SNAPSHOT_ARTIFACT, "version": 1, "count": 1,
                "sha256": image_inventory._rows_digest(bad_row), "rows": bad_row,
            }))
            try:
                e.runner.load_snapshot()
                raise AssertionError("poisoned snapshot must fail closed")
            except SnapshotIntegrityError:
                pass

        assert all(s.strip().lower().startswith("select") for s in e.executed)


def test_budget_covers_probes_delay_and_cap() -> None:
    service = _service()
    displayed = []

    def fetch(url):
        sid = url.rstrip("/").rsplit("/", 1)[-1]
        return _page(EMBEDDED + THUMBS, declared_id=sid)

    with tempfile.TemporaryDirectory() as tmp:
        env = _Env(Path(tmp) / "inv", _rows(3), fetch, service, ImageInventoryClassifier(service))
        with env as e:
            e.runner._sleep = displayed.append
            summary = e.runner.run(delay_seconds=0.2)
            # 3 pages => 3 fetches + 3 probes = 6 requests, 5 delay gaps.
            assert (summary["requests"], summary["image_probes"], summary["source_requests"]) == (3, 3, 6), summary
            assert summary["gallery_has_image"] == 3 and displayed == [0.2] * 5, (summary, displayed)

        env = _Env(Path(tmp) / "inv2", _rows(3), fetch, service, ImageInventoryClassifier(service))
        with env as e:
            e.runner._sleep = lambda _s: None
            # Budget 3: page1 fetch+probe (2), page2 fetch (3), page2 probe blocked.
            capped = e.runner.run(max_requests=3)
            assert capped["source_requests"] <= 3 and capped["status"] == "capped", capped
            assert capped["stopped_during_id"] == "10000002", capped
            # The probe-starved id is NOT checkpointed (a resume must retry it).
            assert "request_budget_exhausted" not in capped["reasons"], capped
            checkpoint = e.runner.load_checkpoint()
            assert "10000002" not in checkpoint and "10000001" in checkpoint, checkpoint
            # A plain resume re-visits the unclassified ids.
            resumed = e.runner.run()
            assert (resumed["already_classified"], resumed["classified"]) == (1, 2), resumed

        # Budget spent BEFORE a page fetch: the unvisited id is not checkpointed.
        env = _Env(Path(tmp) / "inv3", _rows(3), fetch, service, ImageInventoryClassifier(service))
        with env as e:
            e.runner._sleep = lambda _s: None
            # Budget 2: page1 fetch+probe (2), page2 fetch blocked before any request.
            capped = e.runner.run(max_requests=2)
            assert capped["stopped_before_id"] == "10000002", capped
            assert capped["source_requests"] == 2, capped
            assert "request_budget_exhausted" not in capped["reasons"], capped
            checkpoint = e.runner.load_checkpoint()
            assert "10000002" not in checkpoint and "10000001" in checkpoint, checkpoint


def test_forces_single_http_attempt_and_restores_env() -> None:
    service = _service()
    calls: list = []
    seen_env: list = []

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = ""

    original = os.environ.get(image_inventory.CATALOG_MAX_ATTEMPTS_ENV)
    with tempfile.TemporaryDirectory() as tmp:
        env = _Env(Path(tmp) / "inv", _rows(1), None, service, ImageInventoryClassifier(service))
        with env as e:
            # A 429 would normally be retried; the scan must force ONE attempt.
            os.environ[image_inventory.CATALOG_MAX_ATTEMPTS_ENV] = "7"
            baseline = os.environ.get(image_inventory.CATALOG_MAX_ATTEMPTS_ENV)

            def fake_get(url, **kw):
                seen_env.append(os.environ.get(image_inventory.CATALOG_MAX_ATTEMPTS_ENV))
                calls.append(url)
                return _Resp(429)

            with unittest.mock.patch.object(catalog_service.requests, "get", fake_get):
                e.runner.run()
            # Exactly one HTTP attempt, and the forced value was live at request time.
            assert len(calls) == 1, calls
            assert seen_env == ["1"], seen_env
            # The caller's env value is restored after the scan.
            assert os.environ.get(image_inventory.CATALOG_MAX_ATTEMPTS_ENV) == baseline
    if original is None:
        os.environ.pop(image_inventory.CATALOG_MAX_ATTEMPTS_ENV, None)
    else:
        os.environ[image_inventory.CATALOG_MAX_ATTEMPTS_ENV] = original


def test_snapshot_refresh_refuses_nonempty_checkpoint() -> None:
    service = _service()
    no_identity = lambda url: _page("<div class='detail-right'><h1>X</h1></div>", declared_id=None)
    with tempfile.TemporaryDirectory() as tmp:
        env = _Env(Path(tmp) / "inv", _rows(3), no_identity, service, ImageInventoryClassifier(service))
        with env as e:
            e.runner.run(limit=1)
            assert e.runner.load_checkpoint(), "expected a non-empty checkpoint"
            try:
                e.runner.run(refresh_snapshot=True)
                raise AssertionError("refresh with a non-empty checkpoint must be refused")
            except RuntimeError as exc:
                assert "snapshot-refresh" in str(exc), exc
        # A fresh state dir (empty checkpoint) may take a new snapshot.
        env = _Env(Path(tmp) / "fresh", _rows(3), None, service, ImageInventoryClassifier(service))
        with env as e:
            assert len(e.runner.snapshot_candidates(refresh=True)) == 3


def test_resume_and_torn_checkpoint() -> None:
    service = _service()
    fetched = []
    executed: list = []

    def fetch(url):
        fetched.append(url)
        return _page("<div class='detail-right'><h1>X</h1></div>", declared_id=url.rstrip("/").rsplit("/", 1)[-1])

    with tempfile.TemporaryDirectory() as tmp:
        env = _Env(Path(tmp) / "inv", _rows(3), fetch, service, ImageInventoryClassifier(service))
        with env as e:
            first = e.runner.run(limit=2)
            assert first["classified"] == 2 and first["status"] == "capped" and len(fetched) == 2
            second = e.runner.run()
            assert second["already_classified"] == 2 and second["classified"] == 1 and len(fetched) == 3

            ckpt = e.runner.checkpoint_path()
            text = ckpt.read_text()
            assert (ckpt.stat().st_mode & 0o777) == 0o600 and "http" not in text and "<html" not in text
            assert len([ln for ln in text.splitlines() if ln]) == 3

            # Torn trailing line is ignored and repaired on the next run.
            with ckpt.open("a", encoding="utf-8") as h:
                h.write('{"catalog_spec_id": "1000000')
            assert sorted(e.runner.load_checkpoint()) == ["10000001", "10000002", "10000003"]
            repaired = e.runner.run()
            assert repaired["already_classified"] == 3 and repaired["requests"] == 0 and len(fetched) == 3
            for line in ckpt.read_text().splitlines():
                json.loads(line)

        assert all(s.strip().lower().startswith("select") for s in e.executed)


def main() -> None:
    test_classifier_adversarial()
    test_snapshot_persisted_once_readonly_and_verified()
    test_budget_covers_probes_delay_and_cap()
    test_resume_and_torn_checkpoint()
    test_forces_single_http_attempt_and_restores_env()
    test_snapshot_refresh_refuses_nonempty_checkpoint()
    print("goonet catalog image inventory checks passed")
    print("network_access: never_performed; db_access: faked_readonly_select; writes: asserted_absent")


if __name__ == "__main__":
    main()
