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
        if normalized.startswith("select") and "for update" in normalized and "content_hash, last_changed_at, spec_url" in normalized:
            row = self._table.get(params[0])
            self._result = None if row is None else (row["content_hash"], row["last_changed_at"], row["spec_url"])
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


def _patch_sync_env(service, table, executed, specs, detail_factory):
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
    test_completed_spec_ids_thumb_aware_selection(service)
    test_sync_all_revisits_thumb_specs_replaces_images(service)
    test_sync_all_media_failure_no_upsert_and_partial(service)
    test_completed_sql_predicate_readonly_on_postgres(service)
    print("goonet catalog parser checks passed")
    print("network_access: never_performed; db_access: faked; writes: asserted_absent_by_default")


if __name__ == "__main__":
    main()
