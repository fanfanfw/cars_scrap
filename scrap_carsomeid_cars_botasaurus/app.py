from __future__ import annotations

import os

from flask import Flask, jsonify, request

from scrap_carsomeid_cars_botasaurus.carsomeid_service import CarsomeIdCarsService

app = Flask(__name__)


def compact_sample(items: list[dict], limit: int = 10) -> list[dict]:
    out: list[dict] = []
    for row in items[:limit]:
        item = dict(row)
        raw = item.pop("raw_detail", None)
        if isinstance(raw, dict):
            item["raw_detail_keys"] = sorted(raw.keys())
        out.append(item)
    return out


@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


@app.post("/scrape/carsomeid-cars")
def scrape_carsomeid_cars():
    data = request.get_json(silent=True) or {}
    start_url = data.get("start_url")
    max_pages = int(data.get("max_pages", 1))

    scraper = CarsomeIdCarsService()
    try:
        links = scraper.scrape_all_from_main(start_url=start_url, max_pages=max_pages)
        return jsonify({"count": len(links), "sample": links[:200]}), 200
    finally:
        scraper.close()


@app.post("/scrape/carsomeid-cars-details")
def scrape_carsomeid_cars_details():
    data = request.get_json(silent=True) or {}
    start_url = data.get("start_url")
    max_pages = int(data.get("max_pages", 1))
    links = data.get("links")

    scraper = CarsomeIdCarsService()
    try:
        if not links:
            links = scraper.scrape_all_from_main(start_url=start_url, max_pages=max_pages)
        details = scraper.scrape_listing_details(links)
        errors = [row for row in details if row.get("error")]
        return jsonify(
            {
                "count": len(details),
                "upserted_count": len(details) - len(errors),
                "errors_count": len(errors),
                "sample": compact_sample(details),
                "stored_to_db": len(details) > len(errors),
            }
        ), 200
    finally:
        scraper.close()


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5206"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
