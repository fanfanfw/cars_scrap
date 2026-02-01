-- Table: cars_scrap_carlist
-- Notes:
-- - `listing_id` is UNIQUE for upserts.

CREATE TABLE IF NOT EXISTS cars_scrap_carlist (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE,
  listing_url TEXT NOT NULL,
  title VARCHAR,
  price INTEGER,
  location VARCHAR,
  make VARCHAR,
  model VARCHAR,
  year INTEGER,
  mileage INTEGER,
  transmission VARCHAR,
  fuel_type VARCHAR,
  seller_type VARCHAR,
  images TEXT[],
  status VARCHAR NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cars_scrap_carlistmy
  ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'active';

ALTER TABLE cars_scrap_carlistmy
  ALTER COLUMN status SET DEFAULT 'active';

UPDATE cars_scrap_carlistmy
SET status = 'active'
WHERE status IS NULL OR status = 'ok';

ALTER TABLE cars_scrap_carlistmy
  ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE cars_scrap_carlistmy
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE cars_scrap_carlistmy
  ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

