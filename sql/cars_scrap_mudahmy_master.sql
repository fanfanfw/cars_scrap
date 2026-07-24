CREATE TABLE IF NOT EXISTS cars_scrap_mudahmy_makes (
  id BIGSERIAL PRIMARY KEY,
  make_id VARCHAR NOT NULL UNIQUE,
  make_name TEXT NOT NULL,
  make_slug TEXT NOT NULL,
  founding_country TEXT,
  status VARCHAR NOT NULL DEFAULT 'active',
  raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cars_scrap_mudahmy_models (
  id BIGSERIAL PRIMARY KEY,
  make_id VARCHAR NOT NULL REFERENCES cars_scrap_mudahmy_makes(make_id) ON UPDATE CASCADE ON DELETE CASCADE,
  model_id VARCHAR NOT NULL,
  model_name TEXT NOT NULL,
  model_slug TEXT NOT NULL,
  status VARCHAR NOT NULL DEFAULT 'active',
  last_listing_scraped_at TIMESTAMPTZ,
  last_listing_scrape_status TEXT,
  last_listing_scrape_count INTEGER,
  last_listing_scrape_error TEXT,
  listing_claimed_by TEXT,
  listing_claimed_at TIMESTAMPTZ,
  listing_lease_until TIMESTAMPTZ,
  listing_attempt_count INTEGER NOT NULL DEFAULT 0,
  listing_total_pages INTEGER,
  listing_last_page INTEGER NOT NULL DEFAULT 0,
  listing_failed_page INTEGER,
  raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (make_id, model_id)
);

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_claimed_by TEXT;

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_claimed_at TIMESTAMPTZ;

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_lease_until TIMESTAMPTZ;

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_attempt_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_total_pages INTEGER;

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_last_page INTEGER NOT NULL DEFAULT 0;

ALTER TABLE cars_scrap_mudahmy_models
  ADD COLUMN IF NOT EXISTS listing_failed_page INTEGER;

CREATE INDEX IF NOT EXISTS idx_cars_scrap_mudahmy_models_make_id
  ON cars_scrap_mudahmy_models (make_id);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_mudahmy_models_last_listing_scraped_at
  ON cars_scrap_mudahmy_models (last_listing_scraped_at);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_mudahmy_models_listing_lease_until
  ON cars_scrap_mudahmy_models (listing_lease_until);
