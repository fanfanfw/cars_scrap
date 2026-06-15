-- Table: cars_scrap_goonet_details
-- Notes:
-- - 1 row per listing_id (UNIQUE), for storing Goonet detail page extra fields.
-- - JSONB fields are flexible for varying Goonet page content.

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_details (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE REFERENCES cars_scrap_goonet(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  media JSONB,
  overview JSONB,
  ratings JSONB,
  certification JSONB,
  specifications JSONB,
  equipment JSONB,
  hidden_codes JSONB,
  raw_json_ld JSONB
);

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS listing_url TEXT;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS media JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS overview JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS ratings JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS certification JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS specifications JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS equipment JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS hidden_codes JSONB;

ALTER TABLE cars_scrap_goonet_details
  ADD COLUMN IF NOT EXISTS raw_json_ld JSONB;
