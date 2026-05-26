-- Table: cars_scrap_carsomeid_details
-- Stores full CarsomeID CarListDetail API response as JSONB.

CREATE TABLE IF NOT EXISTS cars_scrap_carsomeid_details (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE REFERENCES cars_scrap_carsomeid(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  raw_detail JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_carsomeid_details_listing_id
  ON cars_scrap_carsomeid_details (listing_id);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_carsomeid_details_raw_detail_gin
  ON cars_scrap_carsomeid_details USING GIN (raw_detail);
