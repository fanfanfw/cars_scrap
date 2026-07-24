from __future__ import annotations

import os
import re
import time
from urllib.parse import urlencode

import requests
from psycopg2.extras import Json, execute_values

from scrap_mudahmy_cars_botasaurus.database import get_connection
from scrap_mudahmy_cars_botasaurus.mudahmy_service import MudahMyCarsService


class MudahMyModelWorkerService:
    def sync_master_data(self) -> dict:
        filter_url = (os.getenv("MUDAHMY_FILTER_URL") or "https://khal.mudah.my/v1/filter/1020").strip()
        response = requests.get(filter_url, timeout=30)
        response.raise_for_status()
        attributes = response.json()["data"]["attributes"]
        makes = attributes["make"]["values"]
        make_ids = {str(make["id"]) for make in makes}
        models = []
        seen = set()
        for group in attributes["model"]["values"]:
            make_id = str(group["parent_id"])
            if make_id not in make_ids:
                raise ValueError(f"Unknown Mudah make parent_id={make_id!r}")
            for model in group.get("values") or []:
                key = (make_id, str(model["id"]))
                if key in seen:
                    continue
                seen.add(key)
                models.append((*key, str(model["name"]).strip(), str(model["slug"]).strip(), Json(model)))

        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE cars_scrap_mudahmy_models SET status = 'inactive', updated_at = NOW()")
            cursor.execute("UPDATE cars_scrap_mudahmy_makes SET status = 'inactive', updated_at = NOW()")
            execute_values(cursor, """
                INSERT INTO cars_scrap_mudahmy_makes (make_id, make_name, make_slug, founding_country, status, raw)
                VALUES %s ON CONFLICT (make_id) DO UPDATE SET make_name = EXCLUDED.make_name,
                make_slug = EXCLUDED.make_slug, founding_country = EXCLUDED.founding_country,
                status = EXCLUDED.status, raw = EXCLUDED.raw, updated_at = NOW()
            """, [(str(x["id"]), str(x["name"]).strip(), str(x["slug"]).strip(), x.get("founding_country"), "active", Json(x)) for x in makes])
            execute_values(cursor, """
                INSERT INTO cars_scrap_mudahmy_models (make_id, model_id, model_name, model_slug, status, raw)
                VALUES %s ON CONFLICT (make_id, model_id) DO UPDATE SET model_name = EXCLUDED.model_name,
                model_slug = EXCLUDED.model_slug, status = 'active', raw = EXCLUDED.raw, updated_at = NOW()
            """, [(a, b, c, d, "active", e) for a, b, c, d, e in models], page_size=500)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
        return {"makes_count": len(makes), "models_count": len(models)}

    def bootstrap_existing_models(self) -> dict:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                WITH existing AS (
                  SELECT lower(regexp_replace(brand, '[^a-zA-Z0-9]+', '', 'g')) AS brand_key,
                         lower(regexp_replace(model, '[^a-zA-Z0-9]+', '', 'g')) AS model_key,
                         count(*) AS listing_count, max(last_scraped_at) AS scraped_at
                  FROM cars_scrap_mudahmy
                  WHERE brand IS NOT NULL AND model IS NOT NULL
                  GROUP BY 1, 2
                )
                UPDATE cars_scrap_mudahmy_models md SET
                  last_listing_scraped_at = existing.scraped_at,
                  last_listing_scrape_status = 'bootstrap_existing',
                  last_listing_scrape_count = existing.listing_count,
                  last_listing_scrape_error = NULL,
                  updated_at = NOW()
                FROM cars_scrap_mudahmy_makes ma, existing
                WHERE ma.make_id = md.make_id
                  AND lower(regexp_replace(ma.make_name, '[^a-zA-Z0-9]+', '', 'g')) = existing.brand_key
                  AND lower(regexp_replace(md.model_name, '[^a-zA-Z0-9]+', '', 'g')) = existing.model_key
                  AND md.last_listing_scraped_at IS NULL
            """)
            matched = cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
        return {"matched_models": matched}

    def get_status(self, status_filter: str | None = None, limit: int = 50) -> dict:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT COALESCE(last_listing_scrape_status, 'pending'), count(*)
                FROM cars_scrap_mudahmy_models
                WHERE status = 'active'
                GROUP BY 1 ORDER BY 1
            """)
            summary = {status: count for status, count in cursor.fetchall()}
            params: list[object] = []
            where = "md.status = 'active'"
            if status_filter:
                where += " AND COALESCE(md.last_listing_scrape_status, 'pending') = %s"
                params.append(status_filter)
            params.append(limit)
            cursor.execute(f"""
                SELECT ma.make_name, md.model_name,
                  COALESCE(md.last_listing_scrape_status, 'pending') AS scrape_status,
                  md.last_listing_scrape_count, md.listing_last_page,
                  md.listing_total_pages, md.listing_failed_page,
                  md.listing_claimed_by, md.listing_lease_until,
                  md.last_listing_scraped_at, md.last_listing_scrape_error
                FROM cars_scrap_mudahmy_models md
                JOIN cars_scrap_mudahmy_makes ma ON ma.make_id = md.make_id
                WHERE {where}
                ORDER BY md.last_listing_scraped_at DESC NULLS LAST,
                  ma.make_name, md.model_name
                LIMIT %s
            """, params)
            columns = [column[0] for column in cursor.description]
            models = [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()
            conn.close()
        return {"summary": summary, "filter": status_filter or "all", "models": models}

    def claim_model(self, worker_id: str, lease_minutes: int) -> dict | None:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                WITH candidate AS (
                  SELECT md.id FROM cars_scrap_mudahmy_models md
                  JOIN cars_scrap_mudahmy_makes ma ON ma.make_id = md.make_id
                  WHERE md.status = 'active' AND ma.status = 'active'
                    AND (md.listing_lease_until IS NULL OR md.listing_lease_until <= NOW())
                  ORDER BY md.last_listing_scraped_at NULLS FIRST, md.id
                  FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE cars_scrap_mudahmy_models md SET
                  last_listing_scrape_status = 'running', listing_claimed_by = %s,
                  listing_claimed_at = NOW(), listing_lease_until = NOW() + (%s * INTERVAL '1 minute'),
                  listing_attempt_count = listing_attempt_count + 1, listing_last_page = 0,
                  listing_failed_page = NULL, updated_at = NOW()
                FROM candidate, cars_scrap_mudahmy_makes ma
                WHERE md.id = candidate.id AND ma.make_id = md.make_id
                RETURNING md.id, md.make_id, md.model_id, ma.make_name, ma.make_slug, md.model_name, md.model_slug
            """, (worker_id, lease_minutes))
            row = cursor.fetchone()
            conn.commit()
            return dict(zip(("id", "make_id", "model_id", "make_name", "make_slug", "model_name", "model_slug"), row)) if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def update_progress(self, model_id: int, worker_id: str, total_pages: int, last_page: int, failed_page: int | None = None) -> None:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE cars_scrap_mudahmy_models SET listing_total_pages = %s,
                  listing_last_page = %s, listing_failed_page = %s, updated_at = NOW()
                WHERE id = %s AND listing_claimed_by = %s
            """, (total_pages, last_page, failed_page, model_id, worker_id))
            if cursor.rowcount != 1:
                raise RuntimeError("Model claim lost")
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def finish_model(self, model_id: int, worker_id: str, status: str, count: int, error: str | None = None) -> None:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE cars_scrap_mudahmy_models SET last_listing_scraped_at = NOW(),
                  last_listing_scrape_status = %s, last_listing_scrape_count = %s,
                  last_listing_scrape_error = %s, listing_claimed_by = NULL,
                  listing_claimed_at = NULL, listing_lease_until = NULL, updated_at = NOW()
                WHERE id = %s AND listing_claimed_by = %s
            """, (status, count, error, model_id, worker_id))
            if cursor.rowcount != 1:
                raise RuntimeError("Model claim lost")
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _total_pages(self, url: str) -> int:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        pages = [int(value) for value in re.findall(r'Go to page (\d+)', response.text)]
        return max(pages, default=1)

    def scrape_claimed_model(self, model: dict, worker_id: str, max_pages: int, details: bool, page_retries: int = 3) -> dict:
        base_url = f"https://www.mudah.my/malaysia/cars-for-sale/{model['make_slug']}/{model['model_slug']}"
        scraper = MudahMyCarsService()
        links = []
        seen = set()
        try:
            total_pages = self._total_pages(base_url)
            if max_pages > 0:
                total_pages = min(total_pages, max_pages)
            self.update_progress(model["id"], worker_id, total_pages, 0)
            for page in range(1, total_pages + 1):
                url = base_url if page == 1 else f"{base_url}?{urlencode({'o': page})}"
                error = None
                for attempt in range(1, page_retries + 1):
                    try:
                        page_links = scraper.scrape_all_from_main(start_url=url, max_pages=1)
                        break
                    except Exception as exc:
                        error = exc
                        if attempt < page_retries:
                            time.sleep(attempt * 2)
                else:
                    self.update_progress(model["id"], worker_id, total_pages, page - 1, page)
                    raise RuntimeError(f"Page {page} failed after {page_retries} attempts: {error}")
                for link in page_links:
                    if link not in seen:
                        seen.add(link)
                        links.append(link)
                self.update_progress(model["id"], worker_id, total_pages, page)

            results = scraper.scrape_listing_details(links, allow_revive_sold=True, require_brand_model=True) if details and links else []
            detail_errors = sum(1 for result in results if result.get("error") or result.get("skipped_upsert"))
            if detail_errors:
                raise RuntimeError(f"{detail_errors} listing details failed or were skipped")
            self.finish_model(model["id"], worker_id, "success", len(links))
            return {"model": model, "url": base_url, "pages": total_pages, "links_count": len(links), "details_count": len(results), "links": links}
        except Exception as exc:
            self.finish_model(model["id"], worker_id, "error", len(links), str(exc))
            raise
        finally:
            scraper.close()
