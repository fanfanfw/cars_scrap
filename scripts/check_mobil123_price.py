"""Offline check: Mobil123 _parse_rupiah_price override and batch tuple price slot."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub heavy DB/env deps so mobil123_service.py can import offline.
for _mod in ("psycopg2", "psycopg2.extras", "dotenv"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)
sys.modules["psycopg2.extras"].Json = type("Json", (), {"__init__": lambda self, v: None})
sys.modules["psycopg2.extras"].execute_values = lambda *a, **kw: None
sys.modules["dotenv"].load_dotenv = lambda **kw: None

from scrap_mobil123_cars_botasaurus.mobil123_service import Mobil123CarsService


# Price slot index in rows tuple (L1955-1981 of mobil123_service.py).
_PRICE_SLOT = 9


def test_parse_rupiah_price_override() -> None:
    """Full matrix on Mobil123 override, not base parser."""
    svc = Mobil123CarsService.__new__(Mobil123CarsService)
    parse = svc._parse_rupiah_price

    # valid rupiah values
    assert parse("Rp 150.000.000") == 150_000_000, "normal rupiah"
    assert parse("150000000") == 150_000_000, "bare digits"
    assert parse(500_000_000) == 500_000_000, "int valid"

    # valid upper bound
    assert parse("Rp 100.000.000.000") == 100_000_000_000, "ceiling"
    assert parse("100000000000") == 100_000_000_000, "ceiling bare"

    # above ceiling -> None
    assert parse("Rp 100.000.000.001") is None, "above ceiling"

    # above PostgreSQL BIGINT max -> None
    assert parse("9.999.999.999.999.999.999") is None, "above BIGINT max"
    assert parse("Rp 9.223.372.036.854.775.808") is None, "BIGINT max + 1"
    assert parse(9_223_372_036_854_775_808) is None, "int above BIGINT max"

    # URL/image-like -> None
    assert parse("https://cdn.example.com/img.webp") is None, "URL"
    assert parse("icarcdn://something") is None, "icarcdn"
    assert parse("photo.jpg") is None, "image ext"

    # missing/empty/zero -> None
    assert parse(None) is None, "None"
    assert parse("") is None, "empty"
    assert parse("   ") is None, "whitespace"
    assert parse("Rp 0") is None, "zero"
    assert parse(0) is None, "int zero"

    # negative string -> None (override rejects leading minus)
    assert parse("-5000000") is None, "str negative"
    assert parse("  -150000000") is None, "str negative with spaces"
    assert parse("-Rp 50.000.000") is None, "str negative rupiah prefix"

    # negative int -> None (base parser bound)
    assert parse(-100) is None, "int negative"
    assert parse(-1) is None, "int minus one"

    # no rupiah pattern and short digits -> None
    assert parse("hello") is None, "no digits"
    assert parse("abc123") is None, "short no rp"


def test_tuple_price_slot() -> None:
    """Construct representative pending row tuple; assert price slot bounded int or None."""
    svc = Mobil123CarsService.__new__(Mobil123CarsService)
    parse = svc._parse_rupiah_price

    cases = [
        ("Rp 150.000.000", 150_000_000),
        ("https://cdn.example.com/img.webp", None),
        (None, None),
        ("Rp 0", None),
        ("-5000000", None),
        ("Rp 9.223.372.036.854.775.808", None),
        ("Rp 999.999.999.999.999.999", None),
        ("Rp 100.000.000.000", 100_000_000_000),
        ("Rp 100.000.000.001", None),
        ("photo.jpg", None),
        ("", None),
        ("   ", None),
        (0, None),
        (-1, None),
        (500_000_000, 500_000_000),
        (9_223_372_036_854_775_808, None),
    ]

    for raw, expected in cases:
        price_value = parse(raw)
        assert price_value == expected, f"parse({raw!r}) = {price_value!r}, want {expected!r}"
        # Build representative tuple matching rows.append at L1955-1981
        row = (
            "listing_123",           # 0 listing_id
            "https://example.com",   # 1 listing_url
            "used",                  # 2 condition
            "Toyota",                # 3 brand
            "Avanza",                # 4 model
            None,                    # 5 variant
            None,                    # 6 information_ads
            None,                    # 7 information_ads_date
            None,                    # 8 location
            price_value,             # 9 price
            2020,                    # 10 year
            50000,                   # 11 mileage
            None,                    # 12 transmission
            "7",                     # 13 seat_capacity
            "1500",                  # 14 engine_cc
            None,                    # 15 fuel_type
            None,                    # 16 series
            None,                    # 17 type
            None,                    # 18 images
            None,                    # 19 whatsapp_number
            None,                    # 20 contact_seller
            None,                    # 21 seller_name
            "active",                # 22 status
        )
        slot = row[_PRICE_SLOT]
        assert slot is None or (isinstance(slot, int) and 0 < slot <= 100_000_000_000), (
            f"price slot not bounded int or None: {slot!r} from raw {raw!r}"
        )


def main() -> None:
    test_parse_rupiah_price_override()
    test_tuple_price_slot()
    print("mobil123 price parser checks passed")


if __name__ == "__main__":
    main()
