-- Table: cars_scrap_mudahmy
-- Notes:
-- - `listing_id` is UNIQUE for upserts.

CREATE TABLE IF NOT EXISTS cars_scrap_mudahmy (
  id BIGSERIAL PRIMARY KEY,
  listing_id BIGINT NOT NULL UNIQUE,
  listing_url TEXT NOT NULL,
  condition VARCHAR(50),
  brand VARCHAR(100),
  model VARCHAR(100),
  variant VARCHAR(100),
  information_ads TEXT,
  information_ads_date DATE,
  location VARCHAR(255),
  price INTEGER,
  year INTEGER,
  mileage INTEGER,
  transmission VARCHAR(50),
  seat_capacity VARCHAR(2),
  engine_cc VARCHAR(50),
  fuel_type VARCHAR(50),
  series VARCHAR(100),
  type VARCHAR(100),
  images TEXT[],
  whatsapp_number TEXT[],
  contact_seller TEXT,
  seller_name TEXT,
  status VARCHAR NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS condition VARCHAR(50);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS brand VARCHAR(100);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS model VARCHAR(100);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS variant VARCHAR(100);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS information_ads TEXT;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS information_ads_date DATE;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS location VARCHAR(255);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS price INTEGER;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS year INTEGER;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS mileage INTEGER;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS transmission VARCHAR(50);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS seat_capacity VARCHAR(2);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS engine_cc VARCHAR(50);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS fuel_type VARCHAR(50);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS series VARCHAR(100);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS type VARCHAR(100);

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS images TEXT[];

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS whatsapp_number TEXT[];

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS contact_seller TEXT;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS seller_name TEXT;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'active';

ALTER TABLE cars_scrap_mudahmy
  ALTER COLUMN status SET DEFAULT 'active';

UPDATE cars_scrap_mudahmy
SET status = 'active'
WHERE status IS NULL OR status = 'ok';

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE cars_scrap_mudahmy
  ADD COLUMN IF NOT EXISTS last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
