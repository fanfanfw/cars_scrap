-- Table: cars_scrap_olx_details
-- Notes:
-- - 1 row per listing_id (UNIQUE), for storing OLX detail page extra fields.

CREATE TABLE IF NOT EXISTS cars_scrap_olx_details (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE REFERENCES cars_scrap_olx(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  raw_detail JSONB
);

ALTER TABLE cars_scrap_olx_details
  ADD COLUMN IF NOT EXISTS listing_url TEXT;

ALTER TABLE cars_scrap_olx_details
  ADD COLUMN IF NOT EXISTS raw_detail JSONB;
