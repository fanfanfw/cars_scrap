-- Table: cars_scrap_goonet
-- Notes:
-- - `listing_id` is UNIQUE for upserts.
-- - Goonet exposes both JPY and USD FOB prices, so both are stored.

CREATE TABLE IF NOT EXISTS cars_scrap_goonet (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE,
  stock_number VARCHAR,
  listing_url TEXT NOT NULL,
  condition VARCHAR(50),
  brand VARCHAR(100),
  model VARCHAR(100),
  variant VARCHAR(150),
  information_ads TEXT,
  information_ads_date DATE,
  location VARCHAR,
  year INTEGER,
  mileage INTEGER,
  price_jpy BIGINT,
  price_usd BIGINT,
  transmission VARCHAR,
  seat_capacity VARCHAR(10),
  engine_cc VARCHAR(50),
  fuel_type VARCHAR,
  series VARCHAR(100),
  type VARCHAR(100),
  images TEXT[],
  video_url TEXT,
  whatsapp_number TEXT[],
  contact_seller TEXT,
  seller_name TEXT,
  status VARCHAR NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS listing_id VARCHAR;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS stock_number VARCHAR;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS listing_url TEXT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS condition VARCHAR(50);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS brand VARCHAR(100);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS model VARCHAR(100);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS variant VARCHAR(150);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS information_ads TEXT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS information_ads_date DATE;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS location VARCHAR;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS year INTEGER;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS mileage INTEGER;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS price_jpy BIGINT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS price_usd BIGINT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS transmission VARCHAR;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS seat_capacity VARCHAR(10);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS engine_cc VARCHAR(50);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS fuel_type VARCHAR;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS series VARCHAR(100);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS type VARCHAR(100);

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS images TEXT[];

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS video_url TEXT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS whatsapp_number TEXT[];

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS contact_seller TEXT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS seller_name TEXT;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'active';

ALTER TABLE cars_scrap_goonet
  ALTER COLUMN status SET DEFAULT 'active';

UPDATE cars_scrap_goonet
SET status = 'active'
WHERE status IS NULL OR status = 'ok';

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE cars_scrap_goonet
  ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_cars_scrap_goonet_listing_id
  ON cars_scrap_goonet (listing_id);
