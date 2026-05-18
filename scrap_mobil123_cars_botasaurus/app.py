from __future__ import annotations

import os

from flask import Flask, jsonify, request

from scrap_mobil123_cars_botasaurus.mobil123_service import Mobil123CarsService

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


@app.post("/scrape/mobil123-cars")
def scrape_mobil123_cars():
    data = request.get_json(silent=True) or {}
    start_url = data.get("start_url")
    max_pages = int(data.get("max_pages", 1))

    scraper = Mobil123CarsService()
    try:
        links = scraper.scrape_all_from_main(start_url=start_url, max_pages=max_pages)
        return jsonify({"count": len(links), "sample": links[:200]}), 200
    finally:
        scraper.close()


@app.post("/scrape/mobil123-cars-details")
def scrape_mobil123_cars_details():
    data = request.get_json(silent=True) or {}
    start_url = data.get("start_url")
    max_pages = int(data.get("max_pages", 1))
    links = data.get("links")

    scraper = Mobil123CarsService()
    try:
        if not links:
            links = scraper.scrape_all_from_main(start_url=start_url, max_pages=max_pages)
        details = scraper.scrape_listing_details_with_run_retries(
            links,
            allow_revive_sold=True,
            require_brand_model=True,
        )
        return jsonify({"count": len(details), "sample": details[:50], "stored_to_db": True}), 200
    finally:
        scraper.close()


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5203"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
