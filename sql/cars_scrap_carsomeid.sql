-- Table: cars_scrap_carsomeid
-- Notes:
-- - `listing_id` is UNIQUE for upserts.
-- - CarsomeID detail data is fetched from the CarListDetail API.

CREATE TABLE IF NOT EXISTS cars_scrap_carsomeid (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL UNIQUE,
  listing_url TEXT NOT NULL,
  car_no VARCHAR,
  reg_no VARCHAR,
  car_id BIGINT,
  image TEXT,
  brand VARCHAR(100),
  model VARCHAR(100),
  model_group VARCHAR(100) DEFAULT 'NO MODEL GROUP',
  variant VARCHAR(100),
  year INTEGER,
  mileage INTEGER,
  price BIGINT,
  cash_price BIGINT,
  installment_price BIGINT,
  monthly_payment BIGINT,
  location VARCHAR,
  transmission VARCHAR,
  engine_cc VARCHAR(50),
  fuel_type VARCHAR,
  seat_capacity VARCHAR(50),
  car_type VARCHAR(100),
  color VARCHAR(100),
  images TEXT[],
  status VARCHAR NOT NULL DEFAULT 'active',
  source VARCHAR(50) NOT NULL DEFAULT 'carsomeid',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_carsomeid_brand_model
  ON cars_scrap_carsomeid (brand, model);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_carsomeid_year
  ON cars_scrap_carsomeid (year);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_carsomeid_price
  ON cars_scrap_carsomeid (price);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_carsomeid_status
  ON cars_scrap_carsomeid (status);
