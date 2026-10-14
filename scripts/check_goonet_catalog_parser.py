from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from scrap_goonet_catalog_botasaurus import catalog_service
from scrap_goonet_catalog_botasaurus.catalog_service import GoonetCatalogService


DETAIL_HTML = """
<html>
  <head><title>TOYOTA ALPHARD, Z catalog</title></head>
  <body>
    <div class="detail-right">
      <span class="type-car">TOYOTA</span>
      <h1 class="name-car">TOYOTA ALPHARD Z</h1>
    </div>
    <div class="general-box">
      <p><strong>Model</strong><span>3BA-AGH40W</span></p>
      <p><strong>Dimension</strong><span>4995×1850×1935mm</span></p>
    </div>
    <div class="specifications-box">
      <h2 class="title-box">Engine Fuel</h2>
      <table>
        <tr><th>Engine Model</th><td>2AR-FE</td></tr>
        <tr><th>Displacement</th><td>2493cc</td></tr>
      </table>
    </div>
    <div class="specifications-box">
      <h2 class="title-box">Drivetrain</h2>
      <table>
        <tr><th>Driving Wheel</th><td>FF</td></tr>
        <tr>
          <th>Gear_ratio</th>
          <td>
            <p><strong>1st Gear</strong><span>3.300</span></p>
            <p><strong>Reverse</strong><span>1.751</span></p>
          </td>
        </tr>
      </table>
    </div>
  </body>
</html>
"""


def test_url_extraction(service: GoonetCatalogService) -> None:
    assert service.extract_make_slug("https://www.goo-net-exchange.com/catalog/TOYOTA/") == "TOYOTA"
    assert service.extract_model_slug("/catalog/TOYOTA__ALPHARD/") == "TOYOTA__ALPHARD"
    assert service.extract_catalog_spec_id("/catalog/TOYOTA__ALPHARD/10163593/") == "10163593"
    assert service.absolute_url("/catalog/TOYOTA/", "https://www.goo-net-exchange.com/catalog/") == "https://www.goo-net-exchange.com/catalog/TOYOTA/"


def test_stable_hash(service: GoonetCatalogService) -> None:
    assert service.stable_json_hash({"b": 2, "a": 1}) == service.stable_json_hash({"a": 1, "b": 2})
    base = {"identity": {"catalog_spec_id": "10163593"}, "source": {"scraped_at": "first", "page_title": "T"}}
    changed_time = {"identity": {"catalog_spec_id": "10163593"}, "source": {"scraped_at": "second", "page_title": "T"}}
    assert service.stable_spec_detail_hash(base) == service.stable_spec_detail_hash(changed_time)


def test_spec_detail_parsing(service: GoonetCatalogService) -> None:
    data = service.parse_spec_detail(
        DETAIL_HTML,
        {"spec_url": "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/"},
    )
    assert data["identity"]["catalog_spec_id"] == "10163593"
    assert data["identity"]["make_slug"] == "TOYOTA"
    assert data["identity"]["model_name"] == "ALPHARD"
    assert data["identity"]["variant_name"] == "Z"
    assert data["general_specification"]["Model"] == "3BA-AGH40W"
    assert data["specifications"]["engine_fuel"]["Displacement"] == "2493cc"


def test_gear_ratio_parsing(service: GoonetCatalogService) -> None:
    data = service.parse_spec_detail(
        DETAIL_HTML,
        {"spec_url": "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/"},
    )
    assert data["specifications"]["drivetrain"]["Driving Wheel"] == "FF"
    assert data["specifications"]["drivetrain"]["Gear_ratio"] == {"1st Gear": "3.300", "Reverse": "1.751"}


def _jpeg(width: int, height: int) -> bytes:
    """Minimal structurally valid baseline JPEG (header bytes only)."""
    return (
        b"\xff\xd8"
        b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        + b"\xff\xd9"
    )


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"", content_type: str = "image/jpeg") -> None:
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def iter_content(self, chunk_size: int = 8192):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


def _gallery_html(main_sources: list[str], thumb_sources: list[str]) -> str:
    slides = "".join(f'<div class="swiper-slide"><p class="img1"><img src="{src}"></p></div>' for src in main_sources)
    thumbs = "".join(f'<div class="swiper-slide"><img src="{src}"></div>' for src in thumb_sources)
    return (
        '<html><body><div class="gallery-main"><div class="swiper-wrapper">'
        + slides
        + '</div></div><div class="gallery-thumbs"><div class="swiper-wrapper">'
        + thumbs
        + "</div></div></body></html>"
    )


NONTHUMB = "https://catalogphoto.goo-net.com/carphoto/30201526_201811z.jpg"
THUMB = "https://catalogphoto.goo-net.com/carphoto/thumb/30201526_201811z.jpg"


def test_media_valid_candidate(service: GoonetCatalogService) -> None:
    """GNC-IMG-01: first slide is base64; its thumb derives a validated non-thumb URL."""
    html = _gallery_html(
        ["data:image/jpeg;base64,AAAA", NONTHUMB],
        [THUMB, "https://catalogphoto.goo-net.com/carphoto/thumb/30201526_201811zbk.jpg"],
    )
    calls: list[str] = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return _FakeResponse(200, _jpeg(580, 290))

    with unittest.mock.patch.object(catalog_service.requests, "get", fake_get):
        media = service.parse_spec_detail(html, {})["media"]

    assert media["main_image"] == NONTHUMB
    assert media["images"] == [NONTHUMB]
    assert not any("/thumb/" in url for url in media["images"])
    assert not any(url.startswith("data:") for url in media["images"])
    # Only the derived candidate needed validation; the direct URL was not probed.
    assert calls == [NONTHUMB]


def _assert_abstains(service: GoonetCatalogService, html: str) -> "catalog_service.MediaUnresolved":
    """A gallery whose derived candidate cannot be validated must abstain."""
    try:
        service.parse_spec_detail(html, {})
    except catalog_service.MediaUnresolved as exc:
        return exc
    raise AssertionError("expected MediaUnresolved (abstain), but media was returned")


def test_media_candidate_404_abstains_terminal(service: GoonetCatalogService) -> None:
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(404)):
        exc = _assert_abstains(service, html)
    assert exc.retryable is False


def test_media_candidate_nonimage_abstains(service: GoonetCatalogService) -> None:
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(200, b"<html>nope</html>", "text/html")):
        _assert_abstains(service, html)


def test_media_candidate_zero_dimensions_abstains(service: GoonetCatalogService) -> None:
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(200, _jpeg(0, 0))):
        _assert_abstains(service, html)


def test_media_candidate_timeout_abstains_retryable(service: GoonetCatalogService) -> None:
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])

    def timeout_get(*args, **kwargs):
        raise requests.Timeout("timed out")

    with unittest.mock.patch.object(catalog_service.requests, "get", timeout_get):
        exc = _assert_abstains(service, html)
    assert exc.retryable is True


def test_media_candidate_429_abstains_retryable(service: GoonetCatalogService) -> None:
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(429)):
        exc = _assert_abstains(service, html)
    assert exc.retryable is True


def test_media_no_gallery_is_definitive_empty(service: GoonetCatalogService) -> None:
    """A page with no gallery at all is a true no-image page (no abstention)."""
    html = "<html><body><div class='detail-right'><h1 class='name-car'>X Y</h1></div></body></html>"
    media = service.parse_spec_detail(html, {})["media"]
    assert media == {"main_image": None, "thumbnails": [], "images": []}


def test_media_untrusted_direct_slide_fails_closed(service: GoonetCatalogService) -> None:
    """An existing gallery-main slide with an untrusted direct src must fail closed,
    never emit empty media that would erase previously stored images."""
    html = _gallery_html(["https://evil.example.com/carphoto/x.jpg"], ["https://evil.example.com/carphoto/thumb/x.jpg"])
    calls: list[str] = []
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: calls.append(a[0]) or _FakeResponse(200, _jpeg(10, 10))):
        _assert_abstains(service, html)
    assert calls == []


def test_media_base64_missing_thumb_fails_closed(service: GoonetCatalogService) -> None:
    """base64 slide whose thumbnail is absent cannot be resolved -> fail closed."""
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [])
    calls: list[str] = []
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: calls.append(a[0]) or _FakeResponse(200, _jpeg(10, 10))):
        _assert_abstains(service, html)
    assert calls == []


def test_media_base64_malformed_thumb_fails_closed(service: GoonetCatalogService) -> None:
    """base64 slide whose thumbnail is malformed (bad host/suffix/shape) -> fail closed."""
    malformed = [
        "https://evil.example.com/carphoto/thumb/x.jpg",     # untrusted host
        "http://catalogphoto.goo-net.com/carphoto/thumb/x.jpg",  # non-https
        "https://catalogphoto.goo-net.com/carphoto/thumb/x.php",  # bad suffix
        "https://catalogphoto.goo-net.com/carphoto/thumb/",   # empty name
        "data:image/jpeg;base64,AAAA",                        # not a thumb url
    ]
    calls: list[str] = []
    for bad in malformed:
        html = _gallery_html(["data:image/jpeg;base64,AAAA"], [bad])
        with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: calls.append(a[0]) or _FakeResponse(200, _jpeg(10, 10))):
            _assert_abstains(service, html)
    assert calls == []


def test_probe_classification(service: GoonetCatalogService) -> None:
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(200, _jpeg(10, 10))):
        assert service.probe_image_url(NONTHUMB) == catalog_service.MEDIA_VALID
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(404)):
        assert service.probe_image_url(NONTHUMB) == catalog_service.MEDIA_INVALID
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(429)):
        assert service.probe_image_url(NONTHUMB) == catalog_service.MEDIA_TRANSIENT
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(503)):
        assert service.probe_image_url(NONTHUMB) == catalog_service.MEDIA_TRANSIENT

    def timeout_get(*a, **k):
        raise requests.Timeout("t")

    with unittest.mock.patch.object(catalog_service.requests, "get", timeout_get):
        assert service.probe_image_url(NONTHUMB) == catalog_service.MEDIA_TRANSIENT


def test_run_media_unresolved_preserves_and_stays_partial(service: GoonetCatalogService) -> None:
    """GNC-IMG-01 retention: a transient media failure must NOT upsert (so any
    previously stored media survives) and must leave the run partial, not success."""
    spec = {"catalog_spec_id": "10119889", "spec_url": "https://www.goo-net-exchange.com/catalog/ALFA_ROMEO__4C/10119889/"}
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])
    upsert = unittest.mock.Mock(return_value=True)
    with unittest.mock.patch.object(service, "get_pending_spec_details", lambda *a, **k: [spec]), \
         unittest.mock.patch.object(service, "fetch_html", lambda *a, **k: html), \
         unittest.mock.patch.object(service, "upsert_spec_detail", upsert), \
         unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(429)):
        summary = service.sync_pending_spec_details()
    assert upsert.call_count == 0, "must not write empty media over existing row"
    assert summary["status"] == "partial"
    assert summary["media_unresolved"] == 1
    assert any(error["stage"] == "spec_details_media" for error in summary["errors"])
    assert summary["errors"][0]["error"] == "image validation pending retry"


def test_run_unresolved_missing_thumb_no_upsert(service: GoonetCatalogService) -> None:
    """A base64 slide with a missing thumbnail must NOT upsert (previously stored
    media survives) and must leave the run partial, not success."""
    spec = {"catalog_spec_id": "10119889", "spec_url": "https://www.goo-net-exchange.com/catalog/ALFA_ROMEO__4C/10119889/"}
    html = _gallery_html(["data:image/jpeg;base64,AAAA"], [])
    upsert = unittest.mock.Mock(return_value=True)
    with unittest.mock.patch.object(service, "get_pending_spec_details", lambda *a, **k: [spec]), \
         unittest.mock.patch.object(service, "fetch_html", lambda *a, **k: html), \
         unittest.mock.patch.object(service, "upsert_spec_detail", upsert):
        summary = service.sync_pending_spec_details()
    assert upsert.call_count == 0, "must not write empty media over existing row"
    assert summary["status"] == "partial"
    assert summary["media_unresolved"] == 1
    assert any(error["stage"] == "spec_details_media" for error in summary["errors"])


def test_run_unexpected_parser_error_fails_closed(service: GoonetCatalogService) -> None:
    """An unexpected exception inside media parsing must NOT upsert (so previously
    stored media survives) and must leave the run partial, not success."""
    spec = {"catalog_spec_id": "10163593", "spec_url": "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/"}
    html = DETAIL_HTML + "<div class='gallery-main'><img src='data:image/jpeg;base64,AAAA'></div>"
    upsert = unittest.mock.Mock(return_value=True)

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET-TOKEN-AND-HUGE-PAGE-BLOB")

    with unittest.mock.patch.object(service, "get_pending_spec_details", lambda *a, **k: [spec]), \
         unittest.mock.patch.object(service, "fetch_html", lambda *a, **k: html), \
         unittest.mock.patch.object(service, "upsert_spec_detail", upsert), \
         unittest.mock.patch.object(service, "_collect_media", boom):
        summary = service.sync_pending_spec_details()
    assert upsert.call_count == 0, "must not write empty media over existing row"
    assert summary["status"] == "partial"
    assert summary["media_unresolved"] == 1
    assert any(error["stage"] == "spec_details_media" for error in summary["errors"])
    assert summary["errors"][0]["error"] == "image validation pending retry"
    assert all("SECRET-TOKEN" not in str(error) for error in summary["errors"])


def test_run_definitive_empty_media_is_written(service: GoonetCatalogService) -> None:
    """Control: a genuinely image-less page writes empty media (no abstention)."""
    spec = {"catalog_spec_id": "1008389", "spec_url": "https://www.goo-net-exchange.com/catalog/TOYOTA__SPRINTER/1008389/"}
    html = "<html><body><div class='detail-right'><h1 class='name-car'>X</h1></div></body></html>"
    upsert = unittest.mock.Mock(return_value=True)
    with unittest.mock.patch.object(service, "get_pending_spec_details", lambda *a, **k: [spec]), \
         unittest.mock.patch.object(service, "fetch_html", lambda *a, **k: html), \
         unittest.mock.patch.object(service, "upsert_spec_detail", upsert):
        summary = service.sync_pending_spec_details()
    assert upsert.call_count == 1
    written = upsert.call_args[0][0]["data"]["media"]
    assert written == {"main_image": None, "thumbnails": [], "images": []}
    assert summary["status"] == "success"


def test_media_multiple_galleries(service: GoonetCatalogService) -> None:
    html = (
        _gallery_html(["data:image/jpeg;base64,AAAA"], [THUMB])
        + _gallery_html(
            ["data:image/jpeg;base64,BBBB"],
            ["https://catalogphoto.goo-net.com/carphoto/thumb/999_201901aa.jpg"],
        )
    )
    validated = {
        NONTHUMB: _jpeg(580, 290),
        "https://catalogphoto.goo-net.com/carphoto/999_201901aa.jpg": _jpeg(450, 225),
    }

    def fake_get(url, *args, **kwargs):
        return _FakeResponse(200, validated[url])

    with unittest.mock.patch.object(catalog_service.requests, "get", fake_get):
        media = service.parse_spec_detail(html, {})["media"]

    assert media["images"] == [
        NONTHUMB,
        "https://catalogphoto.goo-net.com/carphoto/999_201901aa.jpg",
    ]
    assert media["main_image"] == NONTHUMB


def test_media_direct_url_retained_without_probe(service: GoonetCatalogService) -> None:
    """Direct non-thumb gallery URLs stay (natural order) and are not network-probed."""
    html = _gallery_html(
        ["data:image/jpeg;base64,AAAA", NONTHUMB, "https://catalogphoto.goo-net.com/carphoto/3030_2020zbk.jpg"],
        [THUMB, "https://catalogphoto.goo-net.com/carphoto/thumb/30201526_201811zbk.jpg"],
    )

    def fake_get(url, *args, **kwargs):
        return _FakeResponse(200, _jpeg(580, 290))

    with unittest.mock.patch.object(catalog_service.requests, "get", fake_get):
        media = service.parse_spec_detail(html, {})["media"]

    assert media["images"] == [NONTHUMB, "https://catalogphoto.goo-net.com/carphoto/3030_2020zbk.jpg"]
    assert media["main_image"] == NONTHUMB


def test_media_direct_allowed_suffixes_retained(service: GoonetCatalogService) -> None:
    """Trusted direct URLs with any allowed image suffix are kept without probing."""
    html = _gallery_html(
        [
            "https://catalogphoto.goo-net.com/carphoto/a_2020aa.png",
            "https://catalogphoto.goo-net.com/carphoto/a_2020aa.jpeg",
            "https://catalogphoto.goo-net.com/carphoto/a_2020aa.gif",
        ],
        [],
    )
    calls: list[str] = []
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: calls.append(a[0]) or _FakeResponse(200, _jpeg(10, 10))):
        media = service.parse_spec_detail(html, {})["media"]
    assert media["images"] == [
        "https://catalogphoto.goo-net.com/carphoto/a_2020aa.png",
        "https://catalogphoto.goo-net.com/carphoto/a_2020aa.jpeg",
        "https://catalogphoto.goo-net.com/carphoto/a_2020aa.gif",
    ]
    assert calls == []


def test_media_unique_images(service: GoonetCatalogService) -> None:
    """Duplicate slides collapse; a direct copy of the validated URL is not repeated."""
    html = _gallery_html(
        ["data:image/jpeg;base64,AAAA", NONTHUMB, NONTHUMB],
        [THUMB, THUMB, THUMB],
    )
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(200, _jpeg(580, 290))):
        media = service.parse_spec_detail(html, {})["media"]
    assert media["images"] == [NONTHUMB]
    assert media["thumbnails"] == [THUMB]


def test_media_parse_failure_fails_closed(service: GoonetCatalogService) -> None:
    """An unexpected parser exception must fail closed (raise), never yield empty
    media that would hash/upsert and erase previously stored media."""
    html = DETAIL_HTML + "<div class='gallery-main'><img src='data:image/jpeg;base64,AAAA'></div>"

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET-TOKEN-AND-HUGE-PAGE-BLOB")

    logs: list[str] = []
    with unittest.mock.patch.object(service.logger, "warning", lambda msg, *a, **k: logs.append(msg % a if a else msg)):
        with unittest.mock.patch.object(service, "_collect_media", boom):
            try:
                service.parse_spec_detail(html, {"spec_url": "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/"})
            except catalog_service.MediaUnresolved as exc:
                caught = exc
            else:
                raise AssertionError("expected MediaUnresolved (fail closed), got media")
    assert caught.retryable is True
    assert str(caught) == "image validation pending retry"
    # Only the exception type is logged; untrusted/large message text is dropped.
    assert logs and "RuntimeError" in logs[0]
    assert all("SECRET-TOKEN" not in line for line in logs)


def test_validate_image_url_rejects_bad_input(service: GoonetCatalogService) -> None:
    assert service.validate_image_url(None) is False
    assert service.validate_image_url("") is False
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(200, b"", "image/jpeg")):
        assert service.validate_image_url(NONTHUMB) is False


def test_validate_rejects_bogus_image_bytes(service: GoonetCatalogService) -> None:
    """image/jpeg content type with non-image bytes must NOT be accepted."""
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: _FakeResponse(200, b"\x00\x01junk-not-an-image", "image/jpeg")):
        assert service.validate_image_url(NONTHUMB) is False


def test_validate_rejects_unsafe_urls_without_request(service: GoonetCatalogService) -> None:
    calls: list[str] = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return _FakeResponse(200, _jpeg(10, 10))

    unsafe = [
        "https://evil.example.com/carphoto/x.jpg",          # untrusted host
        "https://catalogphoto.goo-net.com.evil.com/carphoto/x.jpg",  # suffix host
        "http://catalogphoto.goo-net.com/carphoto/x.jpg",    # non-https
        "https://catalogphoto.goo-net.com:8443/carphoto/x.jpg",  # explicit port
        "https://user:pw@catalogphoto.goo-net.com/carphoto/x.jpg",  # userinfo
        "https://catalogphoto.goo-net.com/carphoto/x.jpg#frag",  # fragment
        "https://catalogphoto.goo-net.com/carphoto/thumb/x.jpg",  # thumb
        "https://catalogphoto.goo-net.com/carphoto/x.php",   # bad suffix
        "https://catalogphoto.goo-net.com/other/x.jpg",      # outside /carphoto/
        "file:///etc/passwd",
        None,
        "",
    ]
    with unittest.mock.patch.object(catalog_service.requests, "get", fake_get):
        for url in unsafe:
            assert service.validate_image_url(url) is False, url
    assert calls == []


def test_validate_rejects_redirect_and_disables_following(service: GoonetCatalogService) -> None:
    captured: dict = {}

    def fake_get(url, *args, **kwargs):
        captured.update(kwargs)
        return _FakeResponse(302, _jpeg(10, 10), "image/jpeg")

    with unittest.mock.patch.object(catalog_service.requests, "get", fake_get):
        assert service.validate_image_url(NONTHUMB) is False
    assert captured.get("allow_redirects") is False


def test_media_malformed_thumb_sources_not_probed(service: GoonetCatalogService) -> None:
    terrible = [
        "https://evil.example.com/carphoto/thumb/x.jpg",       # untrusted host
        "http://catalogphoto.goo-net.com/carphoto/thumb/x.jpg",  # non-https
        "https://catalogphoto.goo-net.com/carphoto/thumb/../evil.jpg",  # path traversal
        "https://catalogphoto.goo-net.com/carphoto/thumb/x.php",  # bad suffix
        "https://catalogphoto.goo-net.com/carphoto/thumb/",     # empty name
        "javascript:alert(1)",
        "data:image/jpeg;base64,AAAA",
    ]
    calls: list[str] = []
    for bad in terrible:
        html = _gallery_html(["data:image/jpeg;base64,AAAA"], [bad])
        with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: calls.append(a[0]) or _FakeResponse(200, _jpeg(10, 10))):
            _assert_abstains(service, html)
    assert calls == []


def test_media_direct_untrusted_host_fails_closed(service: GoonetCatalogService) -> None:
    """Existing direct slides pointing off the trusted photo source fail closed:
    they are neither stored nor allowed to collapse to empty media."""
    html = _gallery_html(
        [
            "https://evil.example.com/carphoto/3030_2020zbk.jpg",
            "http://catalogphoto.goo-net.com/carphoto/3030_2020zbk.jpg",
            "https://catalogphoto.goo-net.com:8443/carphoto/3030_2020zbk.jpg",
            "https://catalogphoto.goo-net.com/carphoto/3030_2020zbk.bin",
        ],
        [],
    )
    calls: list[str] = []
    with unittest.mock.patch.object(catalog_service.requests, "get", lambda *a, **k: calls.append(a[0]) or _FakeResponse(200, _jpeg(10, 10))):
        _assert_abstains(service, html)
    assert calls == []


# ---------------------------------------------------------------------------
# GNC-IMG-03 offline deterministic checks: bounded sample media refresh.
# No network, no DB: fetch_html is faked, probe is faked, and get_connection is a
# fake in-memory connection that records every cursor.execute, so "no write"
# claims are asserted from real call logs. Snapshot artifacts go to a temp dir.
# ---------------------------------------------------------------------------

_SNAPSHOT_TMP = None
# When a catalog_spec_id is present here, the fake cursor raises on its UPDATE,
# used to prove a mid-batch restore failure rolls the whole transaction back.
_FAIL_UPDATE_IDS: set = set()


class _FakeCursor:
    """Minimal DB-API cursor backed by a scripted in-memory table.

    ``table`` maps catalog_spec_id -> row dict. ``executed`` records every
    statement + params. SELECT ... FOR UPDATE, the optimistic identity guard, the
    guarded write and the guarded restore are emulated so the tests assert real
    behaviour (including conflicts and concurrent edits).
    """

    def __init__(self, table: dict, executed: list, connection: "_FakeConnection") -> None:
        self._table = table
        self._executed = executed
        self._conn = connection
        self._result = None
        self.rowcount = 0
        self.description = None

    @staticmethod
    def _unwrap(value):
        return getattr(value, "adapted", value)

    def execute(self, statement: str, params=None):
        self._executed.append((statement, params))
        normalized = " ".join(statement.split()).lower()
        self._result = None
        self.rowcount = 0
        if normalized.startswith("select") and "for update" in normalized and "status, data" in normalized:
            # GNC-FULL-03E-2a: the proven-gallery apply locks status + data too so
            # it can assert the row is still active and media-empty at write time.
            row = self._table.get(params[0])
            self._result = None if row is None else (
                row["content_hash"], row["last_changed_at"], row["spec_url"], row["status"], row["data"],
            )
            return
        if normalized.startswith("select") and "for update" in normalized and "content_hash, last_changed_at, spec_url, status" in normalized:
            # GNC-DETAIL-01: the detail write locks the row's identity + status +
            # model_slug so it can assert the row is unchanged at write time
            # (status/model_slug are read but never written).
            row = self._table.get(params[0])
            self._result = None if row is None else (
                row["content_hash"], row["last_changed_at"], row["spec_url"], row["status"], row["model_slug"],
            )
            return
        if normalized.startswith("select") and "for update" in normalized and "content_hash, last_changed_at, spec_url" in normalized:
            row = self._table.get(params[0])
            self._result = None if row is None else (row["content_hash"], row["last_changed_at"], row["spec_url"])
            return
        if normalized.startswith("update cars_scrap_goonet_catalog_specs set content_hash = %(content_hash)s"):
            # GNC-DETAIL-01 guarded single-row detail write. It deliberately OMITS
            # BOTH `status` and `last_seen_at`, so both must survive verbatim.
            spec_id = params["catalog_spec_id"]
            row = self._table.get(spec_id)
            if row is None:
                return
            row["data"] = self._unwrap(params["data"])
            row["content_hash"] = params["content_hash"]
            row["last_changed_at"] = params["last_changed_at"]
            self.rowcount = 1
            return
        if normalized.startswith("select") and "for update" in normalized and normalized.startswith("select content_hash, last_changed_at from"):
            row = self._table.get(params[0])
            self._result = None if row is None else (row["content_hash"], row["last_changed_at"])
            return
        if normalized.startswith("select") and "where catalog_spec_id = any" in normalized:
            ids = params[0]
            self._result = [dict(self._table[i]) for i in ids if i in self._table]
            return
        if normalized.startswith("update cars_scrap_goonet_catalog_specs set data = %(data)s"):
            spec_id = params["catalog_spec_id"]
            if spec_id in _FAIL_UPDATE_IDS:
                raise RuntimeError(f"injected update failure for {spec_id}")
            row = self._table.get(spec_id)
            if row is None:
                return
            row["data"] = self._unwrap(params["data"])
            row["content_hash"] = params["content_hash"]
            if "first_seen_at" in params:  # guarded restore path
                row["first_seen_at"] = params["first_seen_at"]
                row["last_seen_at"] = params["last_seen_at"]
                row["last_changed_at"] = params["last_changed_at"]
                row["status"] = params["status"]
                row["spec_url"] = params["spec_url"]
            else:  # guarded media-refresh path: explicit post-write timestamp
                row["last_changed_at"] = params["last_changed_at"]
                row["last_seen_at"] = "NOW"
                row["status"] = "active"
            self.rowcount = 1
            return
        if normalized.startswith("select pg_try_advisory_lock") or normalized.startswith("select pg_advisory_unlock"):
            self._result = (True,)
            return
        if normalized.startswith("insert into cars_scrap_goonet_catalog_runs"):
            self._result = (1, "2026-01-01T00:00:00+00:00")
            return
        if normalized.startswith("update cars_scrap_goonet_catalog_runs") or normalized.startswith(
            "update cars_scrap_goonet_catalog_specs set status"
        ):
            self.rowcount = 0
            return
        if normalized.startswith("select count(*) from cars_scrap_goonet_catalog_specs"):
            self._result = (0,)
            return
        if normalized.startswith("select catalog_spec_id from cars_scrap_goonet_catalog_specs where model_slug"):
            # GNC-IMG-12B: mirror the real thumb-aware completion predicate so the
            # selection is asserted against the rows a real DB would return.
            self._result = [(spec_id,) for spec_id, row in sorted(self._table.items())
                            if row.get("model_slug") == params[0] and _row_is_complete(row)]
            return
        if normalized.startswith("select content_hash from cars_scrap_goonet_catalog_specs where catalog_spec_id"):
            row = self._table.get(params[0])
            self._result = None if row is None else (row["content_hash"],)
            return
        if normalized.startswith("insert into cars_scrap_goonet_catalog_specs"):
            # Emulate `INSERT ... ON CONFLICT (catalog_spec_id) DO UPDATE`: when the
            # content_hash changes the WHOLE data (including media.images) is
            # replaced with EXCLUDED.data, never merged/appended.
            spec_id = params["catalog_spec_id"]
            row = self._table.get(spec_id)
            if row is None:
                row = dict(params)
                row["data"] = self._unwrap(params["data"])
                self._table[spec_id] = row
                self.rowcount = 1
                return
            if row.get("content_hash") != params["content_hash"]:
                row["data"] = self._unwrap(params["data"])
                row["content_hash"] = params["content_hash"]
            row["spec_url"] = params["spec_url"]
            row["last_seen_at"] = "NOW"
            row["status"] = "active"
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected statement in offline check: {statement}")

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None


class _FakeConnection:
    def __init__(self, table: dict, executed: list) -> None:
        self._table = table
        self._executed = executed
        self.commits = 0
        self.cur = None

    def cursor(self, *args, **kwargs):
        self.cur = _FakeCursor(self._table, self._executed, self)
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Emulate transaction rollback semantics: on exception the fake table is
        # restored from the snapshot taken at __enter__ so "no partial commit"
        # can be asserted.
        if exc_type is not None:
            for key, value in self._table.items():
                value.clear()
                value.update(self._snapshot[key])
        return False


def _fake_connection_factory(table, executed):
    state = {"n": 0}

    def factory():
        state["n"] += 1
        conn = _FakeConnection(table, executed)
        conn._snapshot = {k: dict(v) for k, v in table.items()}
        return conn

    return factory


def _row_is_complete(row) -> bool:
    """Pure-Python mirror of the GNC-IMG-12B ``_completed_spec_ids`` predicate.

    A row counts as complete only when it is non-empty + hashed AND none of its
    ``data.media.images`` entries contains ``/thumb/``. A missing/non-array
    ``images`` value contributes no thumb, matching the SQL jsonb_typeof guard
    (which substitutes ``[]``), so it stays complete.
    """
    data = row.get("data")
    if data is None or row.get("content_hash") is None:
        return False
    if isinstance(data, dict) and not data:
        return False
    media = data.get("media") if isinstance(data, dict) else None
    images = media.get("images") if isinstance(media, dict) else None
    if not isinstance(images, list):
        return True
    return not any(isinstance(url, str) and "/thumb/" in url for url in images)


def _stored_row(spec_id: str, *, content_hash: str = "hash-old", spec_url: str | None = None,
                data=None, last_changed_at="2026-01-01T00:00:00+00:00"):
    return {
        "id": 1,
        "catalog_spec_id": spec_id,
        "make_slug": "TOYOTA",
        "model_slug": "TOYOTA__ALPHARD",
        "make_name": "TOYOTA",
        "model_name": "ALPHARD",
        "variant_name": "Z",
        "release_month": "2023/06",
        "sale_title": None,
        "spec_url": spec_url or f"https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/{spec_id}/",
        "model_code": "3BA-AGH40W",
        "engine": "2400cc",
        "doors": 5,
        "transmission": "CVT",
        "drive": "FF",
        "dimension": "4995x1850x1935mm",
        "weight": "2060kg",
        "msrp": "5,200,000yen",
        "data": data if data is not None else {
            "identity": {"catalog_spec_id": spec_id},
            "general_specification": {"Model": "KEEP-ME"},
            "specifications": {"engine_fuel": {"Displacement": "2493cc"}},
            "media": {"main_image": "old.jpg", "thumbnails": ["old_thumb.jpg"], "images": ["old.jpg"]},
            "source": {"scraped_at": "2020-01-01T00:00:00+00:00", "page_title": "old"},
        },
        "raw_list_row": {"href": "/catalog/x/"},
        "content_hash": content_hash,
        "first_seen_at": "2020-01-01T00:00:00+00:00",
        "last_seen_at": "2020-01-02T00:00:00+00:00",
        "last_changed_at": last_changed_at,
        "status": "active",
    }


def _new_media_html(name: str = "30201526_201811z") -> str:
    return (
        "<html><head><title>DETAIL</title></head><body>"
        "<div class='detail-right'><span class='type-car'>TOYOTA</span>"
        "<h1 class='name-car'>TOYOTA ALPHARD Z</h1></div>"
        "<div class='general-box'><p><strong>Model</strong><span>3BA-AGH40W</span></p></div>"
        "<div class='gallery-main'><img src='data:image/jpeg;base64,AAAA'></div>"
        f"<div class='gallery-thumbs'><img src='https://catalogphoto.goo-net.com/carphoto/thumb/{name}.jpg'></div>"
        "</body></html>"
    )


def _same_media_html() -> str:
    """A page whose resolved media equals the stored media (no change)."""
    return _new_media_html()


def _patch_refresh_env(service, table, executed, html, *, probe_ok=True, tmp_root=None):
    import contextlib

    def fake_probe(url):
        return catalog_service.MEDIA_VALID if probe_ok else catalog_service.MEDIA_TRANSIENT

    db_mod = __import__("scrap_goonet_cars_botasaurus.database", fromlist=["get_connection"])
    stack = contextlib.ExitStack()
    stack.enter_context(unittest.mock.patch.object(db_mod, "get_connection", _fake_connection_factory(table, executed)))
    stack.enter_context(unittest.mock.patch.object(service, "fetch_html", lambda url: html))
    stack.enter_context(unittest.mock.patch.object(service, "probe_image_url", fake_probe))
    if tmp_root is not None:
        stack.enter_context(unittest.mock.patch.object(service, "_snapshot_dir", lambda: Path(tmp_root)))
    return stack


def _writes(executed):
    return [s for s, _ in executed if s.strip().lower().startswith(("insert", "update", "delete"))]


def _identical_media_data(spec_id: str):
    """Stored data whose media already equals what _new_media_html() resolves to."""
    return {
        "identity": {"catalog_spec_id": spec_id},
        "general_specification": {"Model": "KEEP-ME"},
        "media": {"main_image": NONTHUMB, "thumbnails": [THUMB], "images": [NONTHUMB]},
        "source": {"scraped_at": "2020-01-01T00:00:00+00:00", "page_title": "old"},
    }


def test_refresh_single_id_default_read_only(service: GoonetCatalogService) -> None:
    """1 explicit id, no --apply: plan computed, ZERO writes, no snapshot."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"])
        snapshot_files = list(Path(tmp).iterdir())
    assert _writes(executed) == []
    assert summary["mode"] == "dry_run" and summary["applied"] is False
    assert summary["planned"] == 1 and summary["planned_changed"] == 1
    assert summary["snapshot_path"] is None
    assert snapshot_files == [], snapshot_files
    assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg"


def test_refresh_three_ids_ok_and_more_than_three_rejected(service: GoonetCatalogService) -> None:
    """3 ids allowed (read-only); a 4th id is rejected before any work."""
    for count, should_pass in ((3, True), (4, False)):
        ids = [str(10163593 + i) for i in range(count)]
        table = {i: _stored_row(i) for i in ids}
        executed: list = []
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
                try:
                    summary = service.refresh_sample_specs(ids)
                except ValueError as exc:
                    assert not should_pass, exc
                    assert "at most 3" in str(exc)
                    continue
        assert should_pass, f"{count} ids should be rejected"
        assert summary["planned"] == 3
        assert all(s.strip().lower().startswith("select") for s, _ in executed)


def test_refresh_raw_length_capped_before_dedup(service: GoonetCatalogService) -> None:
    """4 copies of the SAME id must be rejected (cap binds the raw argument)."""
    for bad in (["1", "1", "1", "1"], ["1", "2", "2", "3"], ["10163593"] * 4):
        try:
            service.refresh_sample_specs(bad)
        except ValueError as exc:
            assert "may be supplied" in str(exc), str(exc)
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
    # Exactly 3 raw ids (even repeated) is still allowed and de-duplicates.
    assert service._normalize_refresh_ids(["1", "1", "1"]) == ["1"]
    # Direct service guard matches the public path.
    for bad in (["1", "1", "1", "1"], ["1"] * 4):
        try:
            service._normalize_refresh_ids(bad)
        except ValueError:
            continue
        raise AssertionError(f"_normalize_refresh_ids accepted {bad!r}")


def test_refresh_requires_explicit_ids_no_auto_select(service: GoonetCatalogService) -> None:
    """No ids (or blank/non-numeric) must never silently auto-select."""
    for bad in ([], None, [""], ["abc"], ["1", "2", "3", "4"]):
        try:
            service.refresh_sample_specs(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")
    assert catalog_service.SAMPLE_REFRESH_MAX_IDS == 3


def test_refresh_apply_success_snapshot_and_media_only(service: GoonetCatalogService) -> None:
    """--apply writes only the media block; snapshot records pre + post state."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
            snap = json.loads(Path(summary["snapshot_path"]).read_text())
            snap_mode = oct(Path(summary["snapshot_path"]).stat().st_mode & 0o777)
    assert summary["status"] == "success"
    assert summary["changed"] == 1 and summary["refreshed"] == 1
    updated = table["10163593"]
    assert updated["data"]["general_specification"] == {"Model": "KEEP-ME"}, "non-media preserved"
    assert updated["data"]["specifications"]["engine_fuel"]["Displacement"] == "2493cc"
    assert updated["data"]["media"]["main_image"] == NONTHUMB
    assert updated["content_hash"] != "hash-old"
    assert snap["artifact"] == "gnc-img-03-sample-refresh-snapshot"
    assert snap["columns"] == list(catalog_service.SAMPLE_REFRESH_SNAPSHOT_COLUMNS)
    assert snap["rows"][0]["data"]["media"]["main_image"] == "old.jpg"
    assert snap["rows"][0]["last_changed_at"] == "2026-01-01T00:00:00+00:00"
    assert snap["applied_states"]["10163593"]["content_hash"] == updated["content_hash"]
    assert snap_mode == "0o600", snap_mode
    # Apply must not echo media URLs into the summary.
    assert all(item.get("media") is None for item in summary["results"])


def test_refresh_unchanged_does_zero_writes(service: GoonetCatalogService) -> None:
    """Media identical to stored: NO write at all (not even last_seen_at)."""
    table = {"10163593": _stored_row("10163593", data=_identical_media_data("10163593"),
                                     content_hash=None)}
    # Force "unchanged": compute the stored row's real hash first.
    table["10163593"]["content_hash"] = service.stable_spec_detail_hash(table["10163593"]["data"])
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
    assert _writes(executed) == [], _writes(executed)
    assert summary["unchanged"] == 1 and summary["changed"] == 0
    assert summary["snapshot_path"] is None, "no snapshot for an all-unchanged run"
    assert table["10163593"]["last_seen_at"] == "2020-01-02T00:00:00+00:00"


def test_refresh_backup_failure_prevents_write(service: GoonetCatalogService) -> None:
    """If the snapshot cannot be produced, NO write happens."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            with unittest.mock.patch.object(service, "_write_rollback_backup", side_effect=RuntimeError("disk full")):
                try:
                    service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
                except RuntimeError as exc:
                    assert "disk full" in str(exc)
                else:
                    raise AssertionError("expected backup failure to propagate")
    assert _writes(executed) == []
    assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg"


def test_snapshot_dir_symlink_and_chmod_fail_closed(service: GoonetCatalogService) -> None:
    """A symlinked snapshot dir, or a failed chmod, must abort before any write."""
    with tempfile.TemporaryDirectory() as tmp:
        real = Path(tmp) / "real"
        link = Path(tmp) / "link"
        real.mkdir()
        os.symlink(real, link)
        try:
            service._ensure_secure_dir(link)
        except RuntimeError as exc:
            assert "symlink" in str(exc)
        else:
            raise AssertionError("expected symlinked snapshot dir to be refused")

        target = Path(tmp) / "fresh"
        with unittest.mock.patch("os.chmod", side_effect=OSError("nope")):
            try:
                service._ensure_secure_dir(target)
            except RuntimeError as exc:
                assert "0700" in str(exc)
            else:
                raise AssertionError("expected chmod failure to fail closed")


def test_refresh_validation_failure_prevents_write(service: GoonetCatalogService) -> None:
    """A stored spec_url that is not the canonical URL for its id is refused."""
    bad_urls = (
        "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/99999999/",  # id mismatch
        "https://evil.example.com/catalog/TOYOTA__ALPHARD/10163593/",          # off-host SSRF
        "http://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/",   # non-https
        "https://www.goo-net-exchange.com:8443/catalog/TOYOTA__ALPHARD/10163593/",  # port
        "https://user:pw@www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/",  # userinfo
        "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/?x=1",  # query
        "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/#f",  # fragment
        "https://www.goo-net-exchange.com/catalog/A/B/10163593/",  # bad model slug
    )
    for bad_url in bad_urls:
        table = {"10163593": _stored_row("10163593", spec_url=bad_url)}
        executed: list = []
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
                summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
        assert _writes(executed) == [], bad_url
        assert summary["status"] == "partial", bad_url
        assert summary["snapshot_path"] is None, bad_url
        assert any(error["stage"] == "refresh" for error in summary["errors"]), bad_url
        assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg", bad_url


def test_refresh_concurrency_guard_skips_clobber(service: GoonetCatalogService) -> None:
    """A concurrent change between read and write aborts the whole batch."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    real_batch = service._apply_sample_refresh_batch

    def racing_batch(plans, write_timestamp):
        table["10163593"]["content_hash"] = "someone-else-wrote"
        return real_batch(plans, write_timestamp)

    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            with unittest.mock.patch.object(service, "_apply_sample_refresh_batch", racing_batch):
                summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
    assert summary["status"] == "partial"
    assert summary["changed"] == 0
    assert any(error["stage"] == "refresh_conflict" for error in summary["errors"])
    assert table["10163593"]["content_hash"] == "someone-else-wrote"
    assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg"


def test_refresh_media_unresolved_preserves_row(service: GoonetCatalogService) -> None:
    """429/transient media leaves the row untouched and reports partial."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), probe_ok=False, tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
    assert summary["status"] == "partial"
    assert any(error["stage"] == "refresh_media" for error in summary["errors"])
    assert _writes(executed) == []
    assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg"


def test_refresh_missing_row_not_fabricated(service: GoonetCatalogService) -> None:
    """An unknown id is reported missing; never inserted."""
    table: dict = {}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
    assert summary["missing"] == ["10163593"]
    assert summary["snapshot_path"] is None
    assert len(executed) == 1 and executed[0][0].strip().lower().startswith("select")


def test_refresh_rollback_restores_only_changed_rows(service: GoonetCatalogService) -> None:
    """Round trip: apply then guarded rollback restores data + timestamps."""
    original = _stored_row("10163593")
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
            assert table["10163593"]["data"]["media"]["main_image"] == NONTHUMB
            result = service.rollback_sample_refresh(summary["snapshot_path"], apply=True)
    assert result["restored"] == 1
    assert table["10163593"]["data"] == original["data"]
    assert table["10163593"]["content_hash"] == original["content_hash"]
    assert table["10163593"]["first_seen_at"] == original["first_seen_at"]
    assert table["10163593"]["last_seen_at"] == original["last_seen_at"]
    assert table["10163593"]["status"] == "active"


def test_refresh_rollback_dry_run_writes_nothing(service: GoonetCatalogService) -> None:
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
            executed.clear()
            plan = service.rollback_sample_refresh(summary["snapshot_path"])  # no apply
    assert plan["mode"] == "rollback_dry_run" and plan["applied"] is False
    assert plan["candidates"] == ["10163593"]
    assert executed == [], executed
    assert table["10163593"]["data"]["media"]["main_image"] == NONTHUMB  # untouched


def test_refresh_rollback_skips_concurrent_edit(service: GoonetCatalogService) -> None:
    """An external edit after apply must be preserved and abort the whole restore."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
            # Someone else edits after our refresh.
            table["10163593"]["content_hash"] = "external-edit"
            table["10163593"]["data"]["corrected"] = True
            try:
                service.rollback_sample_refresh(summary["snapshot_path"], apply=True)
            except catalog_service.SampleRefreshRollbackError as exc:
                assert "concurrent change" in str(exc)
            else:
                raise AssertionError("expected fail-closed rollback abort")
    assert table["10163593"]["content_hash"] == "external-edit"
    assert table["10163593"]["data"].get("corrected") is True


def test_refresh_rollback_two_row_conflict_rolls_back_all(service: GoonetCatalogService) -> None:
    """A conflict on row #2 must abort the WHOLE restore: row #1 stays applied."""
    ids = ["10163593", "10163594"]
    table = {i: _stored_row(i) for i in ids}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            summary = service.refresh_sample_specs(ids, apply=True, sleep_func=lambda *_: None)
            assert all(table[i]["data"]["media"]["main_image"] == NONTHUMB for i in ids)
            # Concurrent edit to the SECOND candidate (sorted order is ...593, ...594).
            table[sorted(ids)[-1]]["content_hash"] = "external-edit"
            executed.clear()
            try:
                service.rollback_sample_refresh(summary["snapshot_path"], apply=True)
            except catalog_service.SampleRefreshRollbackError:
                pass
            else:
                raise AssertionError("expected fail-closed rollback abort")
    # Fail closed: NEITHER row was restored; the first candidate keeps applied media.
    assert all(table[i]["data"]["media"]["main_image"] == NONTHUMB for i in ids), "partial restore happened"
    # The concurrent edit is preserved, never clobbered.
    assert table[sorted(ids)[-1]]["content_hash"] == "external-edit"


def test_refresh_rollback_never_restores_unapplied_row(service: GoonetCatalogService) -> None:
    """A batch that conflicts on the first write applies NOTHING, so its snapshot
    candidate set is empty and rollback can never touch that row."""
    ids = ["10163593", "10163594"]
    table = {i: _stored_row(i) for i in ids}
    executed: list = []
    real_batch = service._apply_sample_refresh_batch

    def conflicting_batch(plans, write_timestamp):
        # Simulate an external edit landing on the 2nd row before the batch runs.
        table[sorted(ids)[-1]]["content_hash"] = "external-edit"
        return real_batch(plans, write_timestamp)

    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            with unittest.mock.patch.object(service, "_apply_sample_refresh_batch", conflicting_batch):
                summary = service.refresh_sample_specs(ids, apply=True, sleep_func=lambda *_: None)
            assert summary["changed"] == 0, summary
            assert summary["status"] == "partial"
            assert summary["snapshot_path"] is None, "pre-write artifact must not be offered for rollback"
    # No row was applied: the first keeps its pre-write media, the edited row keeps its hash.
    assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg"
    assert table[sorted(ids)[-1]]["content_hash"] == "external-edit"


def test_refresh_rollback_rejects_foreign_and_bad_artifacts(service: GoonetCatalogService) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cases = {
            "foreign.json": {"artifact": "something-else", "rows": [{"catalog_spec_id": "1"}]},
            "norows.json": {"artifact": "gnc-img-03-sample-refresh-snapshot", "rows": []},
            "noid.json": {"artifact": "gnc-img-03-sample-refresh-snapshot", "rows": [{"catalog_spec_id": "x"}], "applied_states": {"x": {}}},
            # Applied row missing its post-write hash would bypass the guard when
            # the live row's content_hash is NULL -> must be rejected.
            "nohash.json": {
                "artifact": "gnc-img-03-sample-refresh-snapshot",
                "rows": [{"catalog_spec_id": "10163593"}],
                "applied_states": {"10163593": {}},
            },
            "nonstrhash.json": {
                "artifact": "gnc-img-03-sample-refresh-snapshot",
                "rows": [{"catalog_spec_id": "10163593"}],
                "applied_states": {"10163593": {"content_hash": None}},
            },
        }
        for name, body in cases.items():
            path = Path(tmp) / name
            path.write_text(json.dumps(body))
            try:
                service.rollback_sample_refresh(str(path), apply=True)
            except ValueError:
                continue
            raise AssertionError(f"expected rejection for {name}")


def test_refresh_rollback_null_hash_row_cannot_be_clobbered(service: GoonetCatalogService) -> None:
    """A tampered artifact must never restore over a NULL-hash external edit."""
    table = {"10163593": _stored_row("10163593", content_hash=None)}
    table["10163593"]["data"]["note"] = "EXTERNAL-EDIT-MUST-SURVIVE"
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        art = Path(tmp) / "tampered.json"
        art.write_text(json.dumps({
            "artifact": "gnc-img-03-sample-refresh-snapshot",
            "rows": [{"catalog_spec_id": "10163593", "data": {"media": {"main_image": "x"}}}],
            "applied_states": {"10163593": {}},
        }))
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            try:
                service.rollback_sample_refresh(str(art), apply=True)
            except ValueError:
                pass
            else:
                raise AssertionError("expected tampered artifact to be rejected")
    assert _writes(executed) == [], executed
    assert table["10163593"]["data"].get("note") == "EXTERNAL-EDIT-MUST-SURVIVE"


def test_refresh_rollback_is_atomic_on_midbatch_failure(service: GoonetCatalogService) -> None:
    """A failure while restoring row #2 must leave row #1 restored too (no partial)."""
    ids = ["10163593", "10163594"]
    table = {i: _stored_row(i) for i in ids}
    originals = {i: json.loads(json.dumps(table[i]["data"])) for i in ids}
    executed: list = []
    _FAIL_UPDATE_IDS.clear()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
                service.refresh_sample_specs(ids, apply=True, sleep_func=lambda *_: None)
                assert all(table[i]["data"]["media"]["main_image"] == NONTHUMB for i in ids)
                # Snapshot the true post-write state, then fail on the second
                # restored id (sorted order) to prove atomicity.
                post = {
                    i: {"content_hash": table[i]["content_hash"], "last_changed_at": table[i]["last_changed_at"]}
                    for i in ids
                }
                _FAIL_UPDATE_IDS.add(sorted(ids)[-1])
                try:
                    service.rollback_sample_refresh(
                        service._write_rollback_backup([dict(table[i]) for i in ids], applied_states=post),
                        apply=True,
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("expected injected mid-batch failure")
    finally:
        _FAIL_UPDATE_IDS.clear()
    # The whole transaction rolled back: neither row was reverted.
    for i in ids:
        assert table[i]["data"]["media"]["main_image"] == NONTHUMB, f"{i} partially restored"
        assert table[i]["data"] != originals[i]


def test_refresh_unexpected_error_does_not_leak(service: GoonetCatalogService) -> None:
    """An unexpected per-spec error must not echo untrusted text into the report."""
    table = {"10163593": _stored_row("10163593")}
    executed: list = []

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET-TOKEN-AND-HUGE-PAGE-BLOB")

    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            with unittest.mock.patch.object(service, "parse_spec_detail", boom):
                summary = service.refresh_sample_specs(["10163593"], apply=True, sleep_func=lambda *_: None)
    assert summary["status"] == "partial"
    assert any(error["stage"] == "refresh" for error in summary["errors"])
    assert all("SECRET-TOKEN" not in json.dumps(summary) for _ in (0,))
    assert all(error["error"] == "refresh failed (RuntimeError)" for error in summary["errors"])
    assert _writes(executed) == []
    assert table["10163593"]["data"]["media"]["main_image"] == "old.jpg"


def test_refresh_throttles_between_requests(service: GoonetCatalogService) -> None:
    """Distinct fetches are spaced by at least the min interval (never on #1)."""
    ids = ["10163593", "10163594", "10163595"]
    table = {i: _stored_row(i) for i in ids}
    executed: list = []
    sleeps: list = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _new_media_html(), tmp_root=tmp):
            service.refresh_sample_specs(ids, min_interval_seconds=2.5, sleep_func=sleeps.append)
    assert sleeps == [2.5, 2.5], sleeps


# ---------------------------------------------------------------------------
# GNC-FULL-03E-2a offline deterministic checks: the OPT-IN proven-gallery apply
# gate. A real inventory snapshot + checkpoint (written by the real runner into a
# temp state dir) is the only thing that can arm the gate; every adversarial case
# must refuse the write. No network, no real DB (fake connection above).
# ---------------------------------------------------------------------------

_PROVEN_ID = "10163593"
_NOGALLERY_ID = "10163594"


def _proven_page(spec_id: str, *, declared: str | None = None, gallery: bool = True) -> str:
    declared = spec_id if declared is None else declared
    head = ""
    if declared:
        head = f"<link rel='canonical' href='https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/{declared}/'>"
    body = "<div class='detail-right'><h1 class='name-car'>TOYOTA ALPHARD Z</h1></div>"
    if gallery:
        body += (
            "<div class='gallery-main'><img src='data:image/jpeg;base64,AAAA'></div>"
            f"<div class='gallery-thumbs'><img src='{THUMB}'></div>"
        )
    return f"<html><head>{head}<title>DETAIL</title></head><body>{body}</body></html>"


def _empty_media_row(spec_id: str):
    """A stored row whose data block exists but has NO media (inventory candidate)."""
    return _stored_row(
        spec_id,
        content_hash="hash-empty",
        data={"identity": {"catalog_spec_id": spec_id}, "general_specification": {"Model": "KEEP-ME"}},
    )


def _write_inventory(state_dir: Path, entries) -> None:
    """Write a REAL inventory snapshot + checkpoint for ``(id, category)`` entries."""
    from scrap_goonet_catalog_botasaurus.image_inventory import ImageInventoryRunner

    runner = ImageInventoryRunner(state_dir=state_dir)
    runner._write_snapshot(
        [{"catalog_spec_id": spec_id, "spec_url": _stored_row(spec_id)["spec_url"]} for spec_id, _ in entries]
    )
    for spec_id, category in entries:
        runner._append_checkpoint(
            {"catalog_spec_id": spec_id, "category": category, "reason": "test", "images_count": 1}
        )


def _run_gated(service, table, executed, html, state_dir, *, ids, probe_ok=True):
    """Run a proven-gallery apply against the fake DB with the gate armed."""
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, html, probe_ok=probe_ok, tmp_root=tmp):
            with unittest.mock.patch.object(service, "_inventory_state_dir", lambda: Path(state_dir)):
                return service.refresh_sample_specs(
                    ids, apply=True, sleep_func=lambda *_: None, min_interval_seconds=0.0,
                    require_proven_gallery=True,
                )


def test_refresh_proven_gallery_gate_adversarial(service: GoonetCatalogService) -> None:
    """The OPT-IN gate writes ONLY a proven, active, still-empty gallery row."""
    from scrap_goonet_catalog_botasaurus.image_inventory import CATEGORY_GALLERY, CATEGORY_NO_GALLERY

    # (1) HAPPY PATH: proven gallery + active empty row + strongly self-declaring
    # page + proven embedded thumb => media-only, non-thumb write.
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    executed: list = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID), state, ids=[_PROVEN_ID])
    assert summary["status"] == "success", summary
    assert summary["changed"] == 1 and summary["rejected"] == [], summary
    updated = table[_PROVEN_ID]
    assert updated["data"]["media"]["images"] == [NONTHUMB], updated["data"]["media"]
    assert not any("/thumb/" in url for url in updated["data"]["media"]["images"])
    # Media-only: the unrelated stored field survives byte-for-byte.
    assert updated["data"]["general_specification"] == {"Model": "KEEP-ME"}
    assert summary["results"][0]["media"] is None  # apply never echoes media

    # (2) SOURCE CHANGED TO NO-GALLERY: the row is proven, but the current page now
    # resolves empty media. The gate must refuse (this is the exact hole the gate
    # exists for), leaving the row untouched.
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    executed: list = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID, gallery=False), state, ids=[_PROVEN_ID])
    assert summary["status"] == "partial", summary
    assert summary["changed"] == 0 and _writes(executed) == [], (summary, _writes(executed))
    assert summary["rejected"] == [{"catalog_spec_id": _PROVEN_ID, "status": "media_empty"}], summary
    assert table[_PROVEN_ID]["data"] == _empty_media_row(_PROVEN_ID)["data"]

    # (2b) A direct (non-embedded) or thumb-only "gallery" is never proven.
    direct = f"<div class='gallery-main'><img src='{NONTHUMB}'></div><div class='gallery-thumbs'><img src='{THUMB}'></div>"
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID, gallery=False).replace(
            "</body>", direct + "</body>"), state, ids=[_PROVEN_ID])
    assert _writes(executed) == [], (summary, _writes(executed))
    assert summary["rejected"][0]["status"] in ("gallery_unproven", "media_empty"), summary

    # (3) NO-GALLERY ID (checkpointed confirmed_no_gallery): never written even
    # with a gallery page now.
    table = {_NOGALLERY_ID: _empty_media_row(_NOGALLERY_ID)}
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY), (_NOGALLERY_ID, CATEGORY_NO_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_NOGALLERY_ID), state, ids=[_NOGALLERY_ID])
    assert _writes(executed) == [], (summary, _writes(executed))
    assert summary["rejected"] == [{"catalog_spec_id": _NOGALLERY_ID, "status": "not_proven_gallery"}], summary

    # (3b) An id NOT in the inventory snapshot at all is refused.
    table = {"10163599": _empty_media_row("10163599")}
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page("10163599"), state, ids=["10163599"])
    assert _writes(executed) == [] and summary["rejected"][0]["status"] == "not_proven_gallery", summary

    # (4) WRONG PAGE: the proven id now serves a distinctly-different spec's page.
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID, declared="99999999"), state, ids=[_PROVEN_ID])
    assert _writes(executed) == [], (summary, _writes(executed))
    assert summary["rejected"] == [{"catalog_spec_id": _PROVEN_ID, "status": "page_identity_mismatch"}], summary
    # (4b) An id-less page is equally unproven.
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID, declared=""), state, ids=[_PROVEN_ID])
    assert _writes(executed) == [] and summary["rejected"][0]["status"].startswith("page_identity"), summary

    # (5) STALE RAW: the row already regained media between inventory and apply.
    table = {_PROVEN_ID: _stored_row(_PROVEN_ID, content_hash="hash-media", data=_clean_media_data(_PROVEN_ID))}
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID), state, ids=[_PROVEN_ID])
    assert _writes(executed) == [], (summary, _writes(executed))
    assert summary["rejected"] == [{"catalog_spec_id": _PROVEN_ID, "status": "media_not_empty"}], summary

    # (5b) STALE RAW: the row is no longer active.
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    table[_PROVEN_ID]["status"] = "missing"
    executed = []
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        summary = _run_gated(service, table, executed, _proven_page(_PROVEN_ID), state, ids=[_PROVEN_ID])
    assert _writes(executed) == [] and summary["rejected"][0]["status"] == "row_not_active", summary

    # (5c) MISSING/POISONED inventory state fails the WHOLE invocation closed
    # before any row read or request.
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    executed = []
    with tempfile.TemporaryDirectory() as state:
        try:
            _run_gated(service, table, executed, _proven_page(_PROVEN_ID), state, ids=[_PROVEN_ID])
        except catalog_service.ProvenGalleryRejected as exc:
            assert exc.status.startswith("inventory_snapshot_invalid"), exc.status
        else:
            raise AssertionError("a missing inventory snapshot must fail closed")
    assert executed == [] and _writes(executed) == [], executed
    with tempfile.TemporaryDirectory() as state:
        _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
        snap = Path(state) / "snapshot.json"
        payload = json.loads(snap.read_text())
        payload["rows"].append({"catalog_spec_id": "10000004", "spec_url": _stored_row("10000004")["spec_url"]})
        snap.write_text(json.dumps(payload))  # digest no longer matches
        try:
            _run_gated(service, table, executed, _proven_page(_PROVEN_ID), state, ids=[_PROVEN_ID])
        except catalog_service.ProvenGalleryRejected as exc:
            assert exc.status.startswith("inventory_snapshot_invalid"), exc.status
        else:
            raise AssertionError("a tampered inventory snapshot must fail closed")

    # (6) CONCURRENCY: the row flips status/empty-media after the read, under lock.
    for mutate, label in (
        (lambda row: row.update({"status": "missing"}), "row_not_active_at_write"),
        (lambda row: row.update({"data": _clean_media_data(_PROVEN_ID)}), "media_not_empty_at_write"),
    ):
        table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
        executed = []
        real_batch = service._apply_sample_refresh_batch

        def racing(plans, write_timestamp, *, require_active_empty_media=False):
            mutate(table[_PROVEN_ID])
            return real_batch(plans, write_timestamp, require_active_empty_media=require_active_empty_media)

        with tempfile.TemporaryDirectory() as state:
            _write_inventory(Path(state), [(_PROVEN_ID, CATEGORY_GALLERY)])
            with _patch_refresh_env(service, table, executed, _proven_page(_PROVEN_ID), tmp_root=state):
                with unittest.mock.patch.object(service, "_inventory_state_dir", lambda: Path(state)):
                    with unittest.mock.patch.object(service, "_apply_sample_refresh_batch", racing):
                        summary = service.refresh_sample_specs(
                            [_PROVEN_ID], apply=True, sleep_func=lambda *_: None,
                            min_interval_seconds=0.0, require_proven_gallery=True,
                        )
        assert summary["status"] == "partial" and summary["changed"] == 0, (label, summary)
        assert any(e["stage"] == "refresh_conflict" for e in summary["errors"]), (label, summary)

    # (7) DEFAULT (gate unset) is byte-for-byte unchanged: the same changed row is
    # written with no inventory state present at all.
    table = {_PROVEN_ID: _empty_media_row(_PROVEN_ID)}
    executed = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _proven_page(_PROVEN_ID), tmp_root=tmp):
            summary = service.refresh_sample_specs([_PROVEN_ID], apply=True, sleep_func=lambda *_: None)
    assert summary["changed"] == 1 and summary["rejected"] == [], summary
    assert summary["require_proven_gallery"] is False, summary



# ---------------------------------------------------------------------------
# GNC-DETAIL-01 offline deterministic checks: the OPT-IN `--detail-url`
# write. `fetch_html`/`parse_spec_detail` are faked and the DB is the fake
# in-memory connection above, so no network/DB write occurs. Every claim the
# contract makes is asserted from real call logs: default read-only, explicit
# write, canonical URL + strong page identity required, existing `missing` row
# stays `missing`, no insert for an unknown id, pre-write snapshot + optimistic
# guard, and no broader status alteration.
# ---------------------------------------------------------------------------

_DETAIL_ID = "10163593"


def _detail_identity_page(spec_id: str, *, declared: str | None = None, slug: str = "TOYOTA__ALPHARD") -> str:
    """A page that self-declares its canonical URL (strong identity)."""
    declared = spec_id if declared is None else declared
    head = ""
    if declared:
        head = f"<link rel='canonical' href='https://www.goo-net-exchange.com/catalog/{slug}/{declared}/'>"
    return (
        f"<html><head>{head}<title>DETAIL</title></head><body>"
        "<div class='detail-right'><span class='type-car'>TOYOTA</span>"
        "<h1 class='name-car'>TOYOTA ALPHARD Z</h1></div>"
        "<div class='general-box'><p><strong>Model</strong><span>3BA-AGH40W</span></p></div>"
        "<div class='gallery-main'><img src='data:image/jpeg;base64,AAAA'></div>"
        f"<div class='gallery-thumbs'><img src='{THUMB}'></div>"
        "</body></html>"
    )


def _detail_url(spec_id: str, slug: str = "TOYOTA__ALPHARD") -> str:
    return f"https://www.goo-net-exchange.com/catalog/{slug}/{spec_id}/"


def _run_detail_write(service, table, executed, html, *, url, apply, tmp_root, probe_ok=True):
    with _patch_refresh_env(service, table, executed, html, probe_ok=probe_ok, tmp_root=tmp_root):
        return service.write_detail_url(url, apply=apply)


def test_detail_url_default_is_read_only(service: GoonetCatalogService) -> None:
    """Without apply, NO write happens and NO snapshot is produced."""
    table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        summary = _run_detail_write(
            service, table, executed, _detail_identity_page(_DETAIL_ID),
            url=_detail_url(_DETAIL_ID), apply=False, tmp_root=tmp,
        )
        snapshot_files = list(Path(tmp).iterdir())
    assert _writes(executed) == [], _writes(executed)
    assert summary["mode"] == "dry_run" and summary["applied"] is False
    assert summary["result"] == "planned" and summary["changed"] == 1
    assert summary["snapshot_path"] is None and snapshot_files == [], snapshot_files
    assert table[_DETAIL_ID]["data"]["media"]["main_image"] == "old.jpg"


def test_detail_url_rejects_non_canonical_url(service: GoonetCatalogService) -> None:
    """A non-canonical / off-shape URL is refused BEFORE any DB read or request."""
    table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
    bad_urls = [
        "https://evil.example.com/catalog/TOYOTA__ALPHARD/10163593/",   # untrusted host
        "http://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/",  # non-https
        "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/?x=1",  # query
        "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/#f",  # fragment
        "https://user:pw@www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/10163593/",  # userinfo
        "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/",  # no id
    ]
    for url in bad_urls:
        executed: list = []
        with tempfile.TemporaryDirectory() as tmp:
            try:
                _run_detail_write(service, table, executed, _detail_identity_page(_DETAIL_ID), url=url, apply=True, tmp_root=tmp)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for {url!r}")
        assert executed == [], (url, executed)


def test_detail_url_write_preserves_missing_status(service: GoonetCatalogService) -> None:
    """The CORE invariant: an existing `missing` row stays `missing` after a write."""
    table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
    table[_DETAIL_ID]["status"] = "missing"
    original_last_seen = table[_DETAIL_ID]["last_seen_at"]
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        summary = _run_detail_write(
            service, table, executed, _detail_identity_page(_DETAIL_ID),
            url=_detail_url(_DETAIL_ID), apply=True, tmp_root=tmp,
        )
        snap = json.loads(Path(summary["snapshot_path"]).read_text())
        snap_mode = oct(Path(summary["snapshot_path"]).stat().st_mode & 0o777)
    assert summary["result"] == "changed" and summary["changed"] == 1, summary
    assert summary["status_preserved"] == "missing", summary
    assert table[_DETAIL_ID]["status"] == "missing", "detail write must NOT flip missing -> active"
    # The UPDATE names neither `status` NOR `last_seen_at`.
    updates = [s for s, _ in executed if s.strip().lower().startswith("update ")]
    assert updates and all("status" not in s.lower().split("set", 1)[1] for s in updates), updates
    assert all("last_seen_at" not in s.lower().split("set", 1)[1] for s in updates), updates
    # Listing membership timestamp is IDENTITY, never advanced by a detail write.
    assert table[_DETAIL_ID]["last_seen_at"] == original_last_seen, "last_seen_at must be untouched"
    # Data was refreshed (media + non-media), snapshot records pre + post state.
    assert table[_DETAIL_ID]["data"]["media"]["main_image"] == NONTHUMB
    assert table[_DETAIL_ID]["content_hash"] != "hash-old"
    assert snap["artifact"] == "gnc-img-03-sample-refresh-snapshot"
    assert snap["rows"][0]["status"] == "missing"
    assert snap["applied_states"][_DETAIL_ID]["content_hash"] == table[_DETAIL_ID]["content_hash"]
    assert snap_mode == "0o600", snap_mode


def test_detail_url_missing_row_not_inserted(service: GoonetCatalogService) -> None:
    """An unknown id is reported missing; NEVER inserted and NO request sent."""
    table: dict = {}
    executed: list = []
    fetched: list = []

    def fake_fetch(url):
        fetched.append(url)
        return _detail_identity_page(_DETAIL_ID)

    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _detail_identity_page(_DETAIL_ID), tmp_root=tmp):
            with unittest.mock.patch.object(service, "fetch_html", fake_fetch):
                summary = service.write_detail_url(_detail_url(_DETAIL_ID), apply=True)
    assert summary["result"] == "missing", summary
    assert summary["snapshot_path"] is None
    assert fetched == [], "a missing row must not trigger a network fetch"
    assert _writes(executed) == [], _writes(executed)
    assert len(executed) == 1 and executed[0][0].strip().lower().startswith("select")


def test_detail_url_requires_strong_identity(service: GoonetCatalogService) -> None:
    """A wrong-spec or id-less page is refused; the row is untouched."""
    for declared, label in (("99999999", "identity_mismatch"), ("", "identity_unverified")):
        table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
        table[_DETAIL_ID]["status"] = "missing"
        executed: list = []
        with tempfile.TemporaryDirectory() as tmp:
            summary = _run_detail_write(
                service, table, executed, _detail_identity_page(_DETAIL_ID, declared=declared),
                url=_detail_url(_DETAIL_ID), apply=True, tmp_root=tmp,
            )
        assert summary["result"].startswith("page_"), (label, summary)
        assert _writes(executed) == [], (label, _writes(executed))
        assert table[_DETAIL_ID]["status"] == "missing"
        assert table[_DETAIL_ID]["data"]["media"]["main_image"] == "old.jpg"


def test_detail_url_transient_media_preserves_row(service: GoonetCatalogService) -> None:
    """A transient gallery abstains: no write, no snapshot, row untouched."""
    table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
    table[_DETAIL_ID]["status"] = "missing"
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        summary = _run_detail_write(
            service, table, executed, _detail_identity_page(_DETAIL_ID),
            url=_detail_url(_DETAIL_ID), apply=True, tmp_root=tmp, probe_ok=False,
        )
    assert summary["result"] == "media_unresolved", summary
    assert summary["snapshot_path"] is None
    assert _writes(executed) == [], _writes(executed)
    assert table[_DETAIL_ID]["data"]["media"]["main_image"] == "old.jpg"


def test_detail_url_unchanged_does_zero_writes(service: GoonetCatalogService) -> None:
    """Detail identical to stored: NO write at all (not even last_seen_at)."""
    page = _detail_identity_page(_DETAIL_ID)
    with unittest.mock.patch.object(service, "probe_image_url", lambda url: catalog_service.MEDIA_VALID):
        parsed = service.parse_spec_detail(
            page, {"catalog_spec_id": _DETAIL_ID, "spec_url": _detail_url(_DETAIL_ID)}
        )
    table = {
        _DETAIL_ID: _stored_row(
            _DETAIL_ID,
            data=parsed,
            spec_url=_detail_url(_DETAIL_ID),
            content_hash=service.stable_spec_detail_hash(parsed),
        )
    }
    table[_DETAIL_ID]["status"] = "missing"
    executed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        summary = _run_detail_write(
            service, table, executed, page,
            url=_detail_url(_DETAIL_ID), apply=True, tmp_root=tmp,
        )
    assert summary["result"] == "unchanged" and summary["changed"] == 0, summary
    assert summary["snapshot_path"] is None
    assert _writes(executed) == [], _writes(executed)
    assert table[_DETAIL_ID]["last_seen_at"] == "2020-01-02T00:00:00+00:00"
    assert table[_DETAIL_ID]["status"] == "missing"


def test_detail_url_concurrency_guard_and_backup(service: GoonetCatalogService) -> None:
    """A concurrent change aborts the write; a backup failure prevents it.

    The lock-time guard must reject ANY change since the read — including a
    ``missing``→``active``/``active``→``missing`` status flip or a model change —
    so a stale listing interpretation is never committed.
    """
    real_apply = service._apply_detail_write

    # (a) Concurrent changes between read and write => conflict, nothing written.
    for label, mutate in (
        ("content_hash", lambda row: row.update({"content_hash": "changed-by-someone-else"})),
        ("status_flip", lambda row: row.update({"status": "active"})),
        ("model_slug", lambda row: row.update({"model_slug": "TOYOTA__OTHER"})),
        ("spec_url", lambda row: row.update({"spec_url": _detail_url(_DETAIL_ID, "TOYOTA__OTHER")})),
    ):
        table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
        table[_DETAIL_ID]["status"] = "missing"
        executed: list = []

        def racing(spec_id, data, new_hash, write_timestamp, stored, _mutate=mutate):
            _mutate(table[spec_id])
            return real_apply(spec_id, data, new_hash, write_timestamp, stored)

        with tempfile.TemporaryDirectory() as tmp:
            with _patch_refresh_env(service, table, executed, _detail_identity_page(_DETAIL_ID), tmp_root=tmp):
                with unittest.mock.patch.object(service, "_apply_detail_write", racing):
                    summary = service.write_detail_url(_detail_url(_DETAIL_ID), apply=True)
        assert summary["result"] == "conflict", (label, summary)
        assert summary["snapshot_path"] is None, label
        assert _writes(executed) == [] or all(
            s.lower().startswith("update cars_scrap_goonet_catalog_specs") for s, _ in _writes(executed)
        ), (label, _writes(executed))
        # The guarded write is inside ONE transaction that raised, so the fake
        # connection rolls it back; assert the row's data is untouched by the
        # write (status may equal the injected flip, never our new media).
        assert table[_DETAIL_ID]["data"]["media"]["main_image"] == "old.jpg", label

    # (b) Snapshot failure prevents the write entirely.
    table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=_detail_url(_DETAIL_ID))}
    table[_DETAIL_ID]["status"] = "missing"
    executed = []
    with tempfile.TemporaryDirectory() as tmp:
        with _patch_refresh_env(service, table, executed, _detail_identity_page(_DETAIL_ID), tmp_root=tmp):
            with unittest.mock.patch.object(service, "_write_rollback_backup", side_effect=RuntimeError("disk full")):
                try:
                    service.write_detail_url(_detail_url(_DETAIL_ID), apply=True)
                except RuntimeError as exc:
                    assert "disk full" in str(exc)
                else:
                    raise AssertionError("expected backup failure to propagate")
    assert _writes(executed) == []
    assert table[_DETAIL_ID]["data"]["media"]["main_image"] == "old.jpg"
    assert table[_DETAIL_ID]["status"] == "missing"


def test_detail_url_stored_row_url_mismatch_refused(service: GoonetCatalogService) -> None:
    """A stored row whose OWN spec_url is not this id's canonical URL is refused.

    A poisoned/foreign stored row (different model or a non-canonical URL) must
    never redirect the fetch, and must never be written.
    """
    cases = {
        "foreign_model": _detail_url(_DETAIL_ID, "TOYOTA__OTHER"),
        "non_canonical": "https://evil.example.com/catalog/TOYOTA__ALPHARD/10163593/",
        "query": _detail_url(_DETAIL_ID) + "?x=1",
    }
    for label, stored_url in cases.items():
        table = {_DETAIL_ID: _stored_row(_DETAIL_ID, spec_url=stored_url)}
        table[_DETAIL_ID]["status"] = "missing"
        executed: list = []
        fetched: list = []

        def fake_fetch(url):
            fetched.append(url)
            return _detail_identity_page(_DETAIL_ID)

        with tempfile.TemporaryDirectory() as tmp:
            with _patch_refresh_env(service, table, executed, _detail_identity_page(_DETAIL_ID), tmp_root=tmp):
                with unittest.mock.patch.object(service, "fetch_html", fake_fetch):
                    summary = service.write_detail_url(_detail_url(_DETAIL_ID), apply=True)
        assert summary["result"] == "row_url_mismatch", (label, summary)
        assert fetched == [], (label, "must refuse before any fetch")
        assert _writes(executed) == [], (label, _writes(executed))
        assert table[_DETAIL_ID]["data"]["media"]["main_image"] == "old.jpg", label


def test_detail_url_cli_exit_codes_and_no_leak() -> None:
    """A failed/blocked CLI apply MUST exit nonzero, disclosing no URL/secret.

    ``main()`` is driven directly with a scripted service so NO real DB/service
    or network is touched; every non-success result must map to a nonzero exit
    and neither stdout nor stderr may echo the canonical URL.
    """
    import contextlib
    import io

    from scrap_goonet_catalog_botasaurus import run_catalog_scraper

    canonical = _detail_url(_DETAIL_ID)

    def run_cli(summary, argv=None):
        real = GoonetCatalogService()  # safe: constructor only builds a logger

        class _FakeService:
            def write_detail_url(self, url, apply=False):
                # Mirror the REAL canonical-URL validation (pure, no DB/network);
                # an invalid URL must raise ValueError, which the CLI turns into a
                # non-leaky argparse error (exit 2).
                if real._canonical_detail_url(url) is None:
                    raise ValueError("--detail-url must be a canonical catalog detail URL")
                return dict(summary)

        argv = argv or ["--detail-url", canonical, "--write-detail"]
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(run_catalog_scraper, "GoonetCatalogService", _FakeService):
            with unittest.mock.patch.object(sys, "argv", ["run_catalog_scraper", *argv]):
                try:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        run_catalog_scraper.main()
                except SystemExit as exc:
                    return exc.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def summary(result, run_status="partial"):
        return {"mode": "apply", "applied": True, "catalog_spec_id": _DETAIL_ID,
                "result": result, "run_status": run_status, "changed": 0,
                "status_preserved": None, "snapshot_path": None, "media": None}

    # Success paths exit 0.
    for result, rs in (("changed", "success"), ("unchanged", "ok")):
        code, out, err = run_cli(summary(result, rs))
        assert code == 0, (result, code, err)
        assert canonical not in out and canonical not in err

    # Failure/blocked paths MUST exit nonzero (never silently "success").
    for result, rs in (
        ("missing", "ok"),
        ("media_unresolved", "partial"),
        ("error", "partial"),
        ("conflict", "partial"),
        ("page_identity_mismatch", "partial"),
        ("row_url_mismatch", "partial"),
    ):
        code, out, err = run_cli(summary(result, rs))
        assert code != 0, (result, code, out)
        # Bounded labels only: no canonical URL (or any URL/secret) disclosed.
        assert canonical not in out and canonical not in err, (result, out, err)
        assert "goo-net-exchange" not in out and "goo-net-exchange" not in err, (result, out, err)

    # An invalid (non-canonical) URL is rejected by argparse with exit 2, and the
    # offending URL is never echoed back.
    code, out, err = run_cli(
        summary("missing", "ok"),
        argv=["--detail-url", "https://evil.example.com/catalog/TOYOTA__ALPHARD/10163593/", "--write-detail"],
    )
    assert code == 2, (code, out, err)
    assert "evil.example.com" not in out and "evil.example.com" not in err, (out, err)


# ---------------------------------------------------------------------------
# GNC-IMG-12B offline deterministic checks: sync_all re-visits existing spec
# details whose stored data.media.images still carries a `/thumb/` URL. The
# completion predicate is exercised through the real `_completed_spec_ids` and
# real `upsert_spec_detail` against the fake in-memory DB above; fetch/parse are
# scripted so nothing touches the network and no real DB write occurs.
# ---------------------------------------------------------------------------

def _patch_db_only(service, table, executed):
    import contextlib

    db_mod = __import__("scrap_goonet_cars_botasaurus.database", fromlist=["get_connection"])
    stack = contextlib.ExitStack()
    stack.enter_context(unittest.mock.patch.object(db_mod, "get_connection", _fake_connection_factory(table, executed)))
    return stack


def _clean_media_data(spec_id: str = "x"):
    return {
        "identity": {"catalog_spec_id": spec_id},
        "general_specification": {"Model": "KEEP-ME"},
        "media": {"main_image": NONTHUMB, "thumbnails": [THUMB], "images": [NONTHUMB]},
    }


def _thumb_media_data(spec_id: str = "x", full: int = 0, thumbs: int = 1):
    images = [f"https://catalogphoto.goo-net.com/carphoto/full{i}.jpg" for i in range(full)]
    images += [f"https://catalogphoto.goo-net.com/carphoto/thumb/t{i}.jpg" for i in range(thumbs)]
    return {
        "identity": {"catalog_spec_id": spec_id},
        "general_specification": {"Model": "KEEP-ME"},
        "media": {"main_image": (images[0] if images else None), "thumbnails": [], "images": images},
    }


def test_completed_spec_ids_thumb_aware_selection(service: GoonetCatalogService) -> None:
    """GNC-IMG-12B: selection excludes thumb-bearing rows and only them.

    Drives the REAL `_completed_spec_ids` SQL against a mocked DB so the exact
    predicate (not just a re-implementation) is verified.
    """
    table = {
        "10000001": _stored_row("10000001", data=_clean_media_data("10000001")),
        "10000002": _stored_row("10000002", data=_thumb_media_data("10000002", thumbs=1)),
        "10000005": _stored_row("10000005", data=_thumb_media_data("10000005", full=3, thumbs=4)),
        "10000006": _stored_row("10000006", data={}, content_hash=None),
        "10000007": _stored_row("10000007", data={"media": {"images": "not-an-array"}}),
        "10000008": _stored_row("10000008", data={"general_specification": {"Model": "X"}}),
        "10000009": _stored_row("10000009", data=_thumb_media_data("10000009"), content_hash=None),
        "10000010": _stored_row("10000010", data={"media": {"images": []}}),
    }
    executed: list = []
    with _patch_db_only(service, table, executed):
        completed = service._completed_spec_ids("TOYOTA__ALPHARD")

    # Clean (non-thumb) complete rows are skipped.
    assert "10000001" in completed
    assert "10000010" in completed, "empty images array is still complete"
    # Thumb-only and mixed 3 full + 4 thumb rows are re-visited.
    assert "10000002" not in completed, "thumb-only row must be re-visited"
    assert "10000005" not in completed, "mixed 3 full + 4 thumb row must be re-visited"
    # Incomplete / missing JSON never counts as completed.
    assert "10000006" not in completed, "empty JSON with null hash must stay pending"
    assert "10000009" not in completed, "thumb row with null hash must stay pending"
    # Robust jsonb type guard: a non-array images value neither raises nor is
    # treated as a thumb. A missing media/images block likewise stays complete.
    assert "10000007" in completed, "malformed non-array images must not raise"
    assert "10000008" in completed, "missing media block stays complete"
    # The real SELECT was actually issued.
    assert any("catalog_spec_id from cars_scrap_goonet_catalog_specs where model_slug" in " ".join(s.split()).lower()
               for s, _ in executed), executed


def _fresh_detail(spec: dict) -> dict:
    return {
        "identity": {
            "catalog_spec_id": spec["catalog_spec_id"],
            "make_slug": "TOYOTA",
            "model_slug": "TOYOTA__ALPHARD",
            "make_name": "TOYOTA",
            "model_name": "ALPHARD",
            "variant_name": "Z",
        },
        "general_specification": {"Model": "3BA-AGH40W"},
        "specifications": {"engine_fuel": {"Displacement": "2493cc"}},
        "media": {"main_image": NONTHUMB, "thumbnails": [THUMB], "images": [NONTHUMB]},
        "source": {"scraped_at": "2026-02-02T00:00:00+00:00", "page_title": "new"},
    }


_SYNC_MAKES = [{"make_slug": "TOYOTA", "make_name": "TOYOTA", "make_url": "https://www.goo-net-exchange.com/catalog/TOYOTA/", "country": "Japan", "raw": {}}]
_SYNC_MODELS = [{"model_slug": "TOYOTA__ALPHARD", "make_slug": "TOYOTA", "make_name": "TOYOTA", "model_name": "ALPHARD", "model_url": "https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/", "raw": {}}]


def _sync_spec(spec_id: str) -> dict:
    return {
        "catalog_spec_id": spec_id,
        "make_slug": "TOYOTA",
        "model_slug": "TOYOTA__ALPHARD",
        "make_name": "TOYOTA",
        "model_name": "ALPHARD",
        "variant_name": "Z",
        "spec_url": f"https://www.goo-net-exchange.com/catalog/TOYOTA__ALPHARD/{spec_id}/",
    }


def _patch_sync_env(service, table, executed, specs, detail_factory, *, state_root=None, run_ids=None):
    import contextlib

    db_mod = __import__("scrap_goonet_cars_botasaurus.database", fromlist=["get_connection"])
    stack = contextlib.ExitStack()
    stack.enter_context(unittest.mock.patch.object(db_mod, "get_connection", _fake_connection_factory(table, executed)))
    stack.enter_context(unittest.mock.patch.object(service, "fetch_html", lambda url: ""))
    stack.enter_context(unittest.mock.patch.object(service, "parse_makes", lambda *a, **k: list(_SYNC_MAKES)))
    stack.enter_context(unittest.mock.patch.object(service, "parse_models", lambda *a, **k: list(_SYNC_MODELS)))
    stack.enter_context(unittest.mock.patch.object(service, "parse_model_specs", lambda *a, **k: list(specs)))
    stack.enter_context(unittest.mock.patch.object(service, "parse_spec_detail", detail_factory))
    stack.enter_context(unittest.mock.patch.object(service, "get_pending_spec_details", lambda *a, **k: []))
    stack.enter_context(unittest.mock.patch.object(service, "upsert_makes", lambda *a, **k: 0))
    stack.enter_context(unittest.mock.patch.object(service, "upsert_models", lambda *a, **k: 0))
    stack.enter_context(unittest.mock.patch.object(service, "upsert_spec_summaries", lambda *a, **k: 0))
    stack.enter_context(unittest.mock.patch.object(service, "mark_missing_makes", lambda *a, **k: 0))
    stack.enter_context(unittest.mock.patch.object(service, "mark_missing_models", lambda *a, **k: 0))
    stack.enter_context(unittest.mock.patch.object(service, "mark_missing_specs", lambda *a, **k: 0))
    stack.enter_context(unittest.mock.patch.object(service, "_sleep_between_requests", lambda *a, **k: None))
    if state_root is not None:
        # GNC-FULL-02: keep the durable full-refresh markers off the real home dir.
        stack.enter_context(unittest.mock.patch.object(service, "_full_refresh_state_dir", lambda: Path(state_root)))
    if run_ids is not None:
        # Deterministic, distinct catalog run ids per _sync invocation.
        pending = list(run_ids)

        def fake_create_run(resumed_from_run_id=None):
            return (pending.pop(0) if pending else max(run_ids) + 1), "2026-01-01T00:00:00+00:00"

        stack.enter_context(unittest.mock.patch.object(service, "_create_catalog_run", fake_create_run))
    return stack


def test_sync_all_revisits_thumb_specs_replaces_images(service: GoonetCatalogService) -> None:
    """sync_all re-visits thumb rows, skips clean rows, and REPLACES images."""
    table = {
        "10000001": _stored_row("10000001", data=_clean_media_data("10000001")),
        "10000002": _stored_row("10000002", data=_thumb_media_data("10000002", thumbs=1)),
        "10000005": _stored_row("10000005", data=_thumb_media_data("10000005", full=3, thumbs=4)),
    }
    clean_before = json.loads(json.dumps(table["10000001"]["data"]))
    executed: list = []
    visited: list = []

    def detail_factory(html, spec):
        visited.append(spec["catalog_spec_id"])
        return _fresh_detail(spec)

    specs = [_sync_spec("10000001"), _sync_spec("10000002"), _sync_spec("10000005")]
    with _patch_sync_env(service, table, executed, specs, detail_factory):
        summary = service.sync_all()

    assert visited == ["10000002", "10000005"], f"clean row must be skipped, visited={visited}"
    assert summary["status"] == "success", summary
    assert summary["details_skipped"] == 1 and summary["spec_details"] == 2, summary
    # Clean row untouched.
    assert table["10000001"]["data"] == clean_before
    # Thumb rows: images array REPLACED (1 non-thumb), never appended/merged.
    for spec_id in ("10000002", "10000005"):
        images = table[spec_id]["data"]["media"]["images"]
        assert images == [NONTHUMB], (spec_id, images)
        assert not any("/thumb/" in url for url in images)
    # The thumb rows' full detail was re-parsed and re-written (fresh hash).
    assert table["10000005"]["data"]["general_specification"] == {"Model": "3BA-AGH40W"}
    assert table["10000005"]["content_hash"] not in (None, "hash-old")


def test_sync_all_media_failure_no_upsert_and_partial(service: GoonetCatalogService) -> None:
    """An indeterminate media failure must NOT upsert (old thumbs survive) and
    must leave the sync_all summary partial, not success."""
    table = {"10000002": _stored_row("10000002", data=_thumb_media_data("10000002", thumbs=2))}
    before = json.loads(json.dumps(table["10000002"]["data"]))
    executed: list = []

    def detail_factory(html, spec):
        raise catalog_service.MediaUnresolved(retryable=True)

    specs = [_sync_spec("10000002")]
    with _patch_sync_env(service, table, executed, specs, detail_factory):
        summary = service.sync_all()

    spec_inserts = [s for s, _ in executed
                    if " ".join(s.split()).lower().startswith("insert into cars_scrap_goonet_catalog_specs")]
    assert spec_inserts == [], "failed image resolution must not upsert the spec"
    assert table["10000002"]["data"] == before, "stored thumbs must survive untouched"
    assert summary["status"] == "partial", summary
    assert summary["media_unresolved"] == 1, summary
    assert any(error["stage"] == "spec_details_media" for error in summary["errors"]), summary["errors"]


def test_sync_all_full_refresh_opt_in_and_resume(service: GoonetCatalogService) -> None:
    """GNC-FULL-02: opt-in full-detail refresh + safe interrupt/resume.

    Asserts, using the REAL `_sync` path against the fake in-memory DB and a
    scripted `parse_spec_detail` (no network, no real DB):

    1. Default (flag off) still SKIPS complete non-thumb rows and re-visits only
       thumb rows -- behaviour unchanged.
    2. Flagged (`refresh_all_spec_details=True`) revisits EVERY enumerated spec,
       including the already complete non-thumb row, and reports accurate
       `detail_attempts`.
    3. A capped (`--max-specs`) flagged run is still a hard bound.
    4. An interrupted/partial flagged run writes a durable run-scoped marker; a
       later flagged `resume` does NOT re-fetch those specs and does NOT skip the
       ones that were never processed.
    """
    clean = _clean_media_data("10000001")
    # Row 1 is COMPLETE non-thumb, row 2 is a thumb row, row 3 is pending.
    table = {
        "10000001": _stored_row("10000001", data=clean),
        "10000002": _stored_row("10000002", data=_thumb_media_data("10000002", thumbs=1)),
        "10000003": _stored_row("10000003", data={}, content_hash=None),
    }
    specs = [_sync_spec("10000001"), _sync_spec("10000002"), _sync_spec("10000003")]

    def _run(*, refresh_all, run_ids, state_root, max_specs=0, resume_from=None, visit_sink):
        executed: list = []

        def detail_factory(html, spec):
            visit_sink.append(spec["catalog_spec_id"])
            return _fresh_detail(spec)

        with _patch_sync_env(service, table, executed, specs, detail_factory,
                             state_root=state_root, run_ids=run_ids):
            kwargs = {"max_specs": max_specs}
            if resume_from is not None:
                kwargs["resume_from"] = resume_from
            return service._sync(refresh_all_spec_details=refresh_all, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        # (1) DEFAULT: complete non-thumb row 1 is skipped; thumb 2 and pending 3
        # are visited.
        visit: list = []
        summary = _run(refresh_all=False, run_ids=[101], state_root=tmp, visit_sink=visit)
        assert visit == ["10000002", "10000003"], visit
        assert summary["details_skipped"] == 1, summary
        assert summary["detail_attempts"] == 2, summary
        assert summary["status"] == "success", summary
        assert summary["refresh_all_spec_details"] is False

        # (2) FLAGGED: every spec revisited, including the complete non-thumb row.
        visit = []
        summary = _run(refresh_all=True, run_ids=[102], state_root=tmp, visit_sink=visit)
        assert visit == ["10000001", "10000002", "10000003"], visit
        assert summary["details_skipped"] == 0, summary
        assert summary["detail_attempts"] == 3, summary
        assert summary["spec_details"] == 3, summary
        assert summary["status"] == "success", summary
        assert summary["refresh_all_spec_details"] is True
        # Durable marker for run 102 records all three processed ids.
        marker = Path(tmp) / "run-102.jsonl"
        assert sorted(marker.read_text().splitlines()) == ["10000001", "10000002", "10000003"]

        # (3) CAPPED FLAGGED: --max-specs remains a hard bound over the union of
        # model-page specs and pending backfill.
        visit = []
        summary = _run(refresh_all=True, run_ids=[103], state_root=tmp, max_specs=2, visit_sink=visit)
        assert summary["detail_attempts"] == 2, summary
        assert len(visit) == 2, visit
        assert summary["status"] == "partial", summary

        # (4) INTERRUPT + RESUME: run 104 is interrupted after its first spec
        # (details 001, 002 already complete). A flagged resume of run 104 must
        # re-fetch ONLY the not-yet-processed specs (003) and must not repeat 001.
        visit = []

        def interrupted_detail(html, spec):
            visit.append(spec["catalog_spec_id"])
            if spec["catalog_spec_id"] == "10000002":
                raise KeyboardInterrupt()
            return _fresh_detail(spec)

        executed: list = []
        try:
            with _patch_sync_env(service, table, executed, specs, interrupted_detail,
                                 state_root=tmp, run_ids=[104]):
                service._sync(refresh_all_spec_details=True)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("expected KeyboardInterrupt to propagate")
        assert visit == ["10000001", "10000002"], visit
        # Only successfully-upserted spec 001 is durably recorded.
        marker = Path(tmp) / "run-104.jsonl"
        assert marker.read_text().splitlines() == ["10000001"], marker.read_text()

        # Resume run 104 as run 105. The completed-nonthumb 001 must NOT be
        # re-fetched; the thumb row 002 (never completed) and pending 003 are.
        visit = []
        summary = _run(refresh_all=True, run_ids=[105], state_root=tmp, visit_sink=visit,
                       resume_from={"id": 104, "current_make_slug": "TOYOTA",
                                    "current_model_slug": "TOYOTA__ALPHARD",
                                    "status": "interrupted"})
        assert visit == ["10000002", "10000003"], visit
        assert summary["detail_attempts"] == 2, summary
        assert summary["resumed_from_run_id"] == 104, summary
        marker = Path(tmp) / "run-105.jsonl"
        assert sorted(marker.read_text().splitlines()) == ["10000001", "10000002", "10000003"]


def test_completed_sql_predicate_readonly_on_postgres(service: GoonetCatalogService) -> None:
    """Optional (opt-in) read-only check of the SHIPPED SQL on a real Postgres.

    Gate with GNC_IMG_12B_REAL_SQL=1. The EXACT predicate shipped in
    ``_completed_spec_ids`` is executed with its single table identifier
    redirected onto a read-only ``WITH t(...) VALUES`` fixture (zero real table
    access, zero writes). This proves both that the shipped predicate excludes
    thumb rows / incomplete rows and that jsonb_array_elements_text cannot raise
    on a malformed non-array media.images.
    """
    if os.getenv("GNC_IMG_12B_REAL_SQL") != "1":
        print("GNC-IMG-12B real SQL check skipped (set GNC_IMG_12B_REAL_SQL=1 to run)")
        return
    import inspect
    import re as _re

    source = inspect.getsource(service._completed_spec_ids)
    match = _re.search(r'"""(.*?)"""', source, _re.S)
    assert match, "could not extract _completed_spec_ids SQL"
    query = match.group(1)
    # Redirect the shipped predicate onto the fixture CTE. Match the table name
    # as a whole identifier only, and require EXACTLY one substitution so a
    # future edit that adds/removes a reference cannot silently pass.
    table_ident = "cars_scrap_goonet_catalog_specs"
    pattern = rf"(?<![\w]){_re.escape(table_ident)}(?![\w])"
    assert len(_re.findall(pattern, query)) == 1, (
        f"expected exactly one {table_ident!r} reference in shipped SQL"
    )
    projected, substitutions = _re.subn(pattern, "t", query)
    assert substitutions == 1, f"expected exactly one substitution, got {substitutions}"
    db_mod = __import__("scrap_goonet_cars_botasaurus.database", fromlist=["get_connection"])
    conn = db_mod.get_connection()
    try:
        with conn.cursor() as cursor:
            # Read-only: values come from the CTE, never the real table.
            cursor.execute(
                """WITH t(catalog_spec_id, model_slug, data, content_hash) AS (VALUES
                     ('clean','M','{"media":{"images":["https://x/carphoto/a.jpg"]}}'::jsonb,'h'),
                     ('thumb','M','{"media":{"images":["https://x/carphoto/thumb/a.jpg"]}}'::jsonb,'h'),
                     ('mixed','M','{"media":{"images":["https://x/a.jpg","https://x/thumb/b.jpg"]}}'::jsonb,'h'),
                     ('emptyobj','M','{}'::jsonb,NULL),
                     ('nulhash','M','{"media":{"images":["https://x/a.jpg"]}}'::jsonb,NULL),
                     ('nonarray','M','{"media":{"images":"nope"}}'::jsonb,'h'),
                     ('intarr','M','{"media":{"images":[123,{"k":1}]}}'::jsonb,'h'),
                     ('nullel','M','{"media":{"images":[null,"https://x/a.jpg"]}}'::jsonb,'h'),
                     ('scalar','M','"scalar"'::jsonb,'h'),
                     ('arrroot','M','[]'::jsonb,'h'),
                     ('jsonnull','M','null'::jsonb,'h')
                   ) """ + projected,
                ("M",),
            )
            selected = {row[0] for row in cursor.fetchall()}
    finally:
        conn.close()
    expected = {"clean", "nonarray", "intarr", "nullel", "scalar", "arrroot", "jsonnull"}
    assert selected == expected, (selected, expected)
    assert "thumb" not in selected and "mixed" not in selected and "emptyobj" not in selected and "nulhash" not in selected
    print(f"GNC-IMG-12B real read-only SQL selection ok: {sorted(selected)}")


def main() -> None:
    service = GoonetCatalogService()
    test_url_extraction(service)
    test_stable_hash(service)
    test_spec_detail_parsing(service)
    test_gear_ratio_parsing(service)
    test_media_valid_candidate(service)
    test_media_candidate_404_abstains_terminal(service)
    test_media_candidate_nonimage_abstains(service)
    test_media_candidate_zero_dimensions_abstains(service)
    test_media_candidate_timeout_abstains_retryable(service)
    test_media_candidate_429_abstains_retryable(service)
    test_media_no_gallery_is_definitive_empty(service)
    test_media_untrusted_direct_slide_fails_closed(service)
    test_media_base64_missing_thumb_fails_closed(service)
    test_media_base64_malformed_thumb_fails_closed(service)
    test_media_multiple_galleries(service)
    test_media_direct_url_retained_without_probe(service)
    test_media_direct_allowed_suffixes_retained(service)
    test_media_direct_untrusted_host_fails_closed(service)
    test_media_unique_images(service)
    test_media_parse_failure_fails_closed(service)
    test_media_malformed_thumb_sources_not_probed(service)
    test_probe_classification(service)
    test_validate_image_url_rejects_bad_input(service)
    test_validate_rejects_bogus_image_bytes(service)
    test_validate_rejects_unsafe_urls_without_request(service)
    test_validate_rejects_redirect_and_disables_following(service)
    test_run_media_unresolved_preserves_and_stays_partial(service)
    test_run_unresolved_missing_thumb_no_upsert(service)
    test_run_unexpected_parser_error_fails_closed(service)
    test_run_definitive_empty_media_is_written(service)
    test_refresh_single_id_default_read_only(service)
    test_refresh_three_ids_ok_and_more_than_three_rejected(service)
    test_refresh_raw_length_capped_before_dedup(service)
    test_refresh_requires_explicit_ids_no_auto_select(service)
    test_refresh_apply_success_snapshot_and_media_only(service)
    test_refresh_unchanged_does_zero_writes(service)
    test_refresh_backup_failure_prevents_write(service)
    test_snapshot_dir_symlink_and_chmod_fail_closed(service)
    test_refresh_validation_failure_prevents_write(service)
    test_refresh_concurrency_guard_skips_clobber(service)
    test_refresh_media_unresolved_preserves_row(service)
    test_refresh_missing_row_not_fabricated(service)
    test_refresh_rollback_restores_only_changed_rows(service)
    test_refresh_rollback_dry_run_writes_nothing(service)
    test_refresh_rollback_skips_concurrent_edit(service)
    test_refresh_rollback_two_row_conflict_rolls_back_all(service)
    test_refresh_rollback_never_restores_unapplied_row(service)
    test_refresh_rollback_rejects_foreign_and_bad_artifacts(service)
    test_refresh_rollback_null_hash_row_cannot_be_clobbered(service)
    test_refresh_rollback_is_atomic_on_midbatch_failure(service)
    test_refresh_unexpected_error_does_not_leak(service)
    test_refresh_throttles_between_requests(service)
    test_refresh_proven_gallery_gate_adversarial(service)
    test_detail_url_default_is_read_only(service)
    test_detail_url_rejects_non_canonical_url(service)
    test_detail_url_write_preserves_missing_status(service)
    test_detail_url_missing_row_not_inserted(service)
    test_detail_url_requires_strong_identity(service)
    test_detail_url_transient_media_preserves_row(service)
    test_detail_url_unchanged_does_zero_writes(service)
    test_detail_url_concurrency_guard_and_backup(service)
    test_detail_url_stored_row_url_mismatch_refused(service)
    test_detail_url_cli_exit_codes_and_no_leak()
    test_completed_spec_ids_thumb_aware_selection(service)
    test_sync_all_revisits_thumb_specs_replaces_images(service)
    test_sync_all_media_failure_no_upsert_and_partial(service)
    test_sync_all_full_refresh_opt_in_and_resume(service)
    test_completed_sql_predicate_readonly_on_postgres(service)
    print("goonet catalog parser checks passed")
    print("network_access: never_performed; db_access: faked; writes: asserted_absent_by_default")


if __name__ == "__main__":
    main()
