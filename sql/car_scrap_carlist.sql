-- Table: car_scrap_carlist
-- Notes:
-- - `listing_id` is UNIQUE for upserts.

CREATE TABLE IF NOT EXISTS car_scrap_carlist (
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

ALTER TABLE car_scrap_carlist
  ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'active';

ALTER TABLE car_scrap_carlist
  ALTER COLUMN status SET DEFAULT 'active';

UPDATE car_scrap_carlist
SET status = 'active'
WHERE status IS NULL OR status = 'ok';

ALTER TABLE car_scrap_carlist
  ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE car_scrap_carlist
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE car_scrap_carlist
  ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

