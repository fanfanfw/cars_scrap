-- Table: cars_scrap_mudahmy_details
-- Notes:
-- - 1 row per listing_id (UNIQUE), for storing "detail page" extra fields.
-- - `car_specifications` is JSONB for flexible querying and indexing.

CREATE TABLE IF NOT EXISTS cars_scrap_mudahmy_details (
  id BIGSERIAL PRIMARY KEY,
  listing_id BIGINT NOT NULL UNIQUE REFERENCES cars_scrap_mudahmy(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  ad_highlights TEXT,
  description TEXT,
  car_specifications JSONB
);

ALTER TABLE cars_scrap_mudahmy_details
  ADD COLUMN IF NOT EXISTS listing_url TEXT;

ALTER TABLE cars_scrap_mudahmy_details
  ADD COLUMN IF NOT EXISTS ad_highlights TEXT;

ALTER TABLE cars_scrap_mudahmy_details
  ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE cars_scrap_mudahmy_details
  ADD COLUMN IF NOT EXISTS car_specifications JSONB;
