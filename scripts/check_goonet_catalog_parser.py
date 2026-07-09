from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def main() -> None:
    service = GoonetCatalogService()
    test_url_extraction(service)
    test_stable_hash(service)
    test_spec_detail_parsing(service)
    test_gear_ratio_parsing(service)
    print("goonet catalog parser checks passed")


if __name__ == "__main__":
    main()
