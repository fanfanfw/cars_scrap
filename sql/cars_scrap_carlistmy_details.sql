-- Table: cars_scrap_carlistmy_details
-- Notes:
-- - 1 row per listing_id (UNIQUE), for storing "detail page" extra fields.
-- - JSONB fields are flexible for varying page content.

CREATE TABLE IF NOT EXISTS cars_scrap_carlistmy_details (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE REFERENCES cars_scrap_carlistmy(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  carousel JSONB,
  seller_notes TEXT,
  specifications JSONB,
  equipment JSONB
);

ALTER TABLE cars_scrap_carlistmy_details
  ADD COLUMN IF NOT EXISTS listing_url TEXT;

ALTER TABLE cars_scrap_carlistmy_details
  ADD COLUMN IF NOT EXISTS carousel JSONB;

ALTER TABLE cars_scrap_carlistmy_details
  ADD COLUMN IF NOT EXISTS seller_notes TEXT;

ALTER TABLE cars_scrap_carlistmy_details
  ADD COLUMN IF NOT EXISTS specifications JSONB;

ALTER TABLE cars_scrap_carlistmy_details
  ADD COLUMN IF NOT EXISTS equipment JSONB;
