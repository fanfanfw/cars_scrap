-- Master data tables for Goonet Exchange maker/model/grade hierarchy.

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_makers (
  id BIGSERIAL PRIMARY KEY,
  maker_cd VARCHAR NOT NULL UNIQUE,
  maker_nm TEXT NOT NULL,
  maker_name TEXT,
  listing_count INTEGER,
  raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_models (
  id BIGSERIAL PRIMARY KEY,
  maker_cd VARCHAR NOT NULL REFERENCES cars_scrap_goonet_makers(maker_cd) ON UPDATE CASCADE ON DELETE CASCADE,
  car_cd VARCHAR NOT NULL UNIQUE,
  car_nm TEXT NOT NULL,
  model_name TEXT,
  listing_count INTEGER,
  last_listing_scraped_at TIMESTAMPTZ,
  last_listing_scrape_status TEXT,
  last_listing_scrape_count INTEGER,
  last_listing_scrape_error TEXT,
  raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_grades (
  id BIGSERIAL PRIMARY KEY,
  maker_cd VARCHAR NOT NULL REFERENCES cars_scrap_goonet_makers(maker_cd) ON UPDATE CASCADE ON DELETE CASCADE,
  car_cd VARCHAR NOT NULL REFERENCES cars_scrap_goonet_models(car_cd) ON UPDATE CASCADE ON DELETE CASCADE,
  grade_cd VARCHAR NOT NULL,
  grade_nm TEXT NOT NULL,
  grade_name TEXT,
  listing_count INTEGER,
  raw JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (car_cd, grade_cd)
);

ALTER TABLE cars_scrap_goonet_models
  ADD COLUMN IF NOT EXISTS last_listing_scraped_at TIMESTAMPTZ;

ALTER TABLE cars_scrap_goonet_models
  ADD COLUMN IF NOT EXISTS last_listing_scrape_status TEXT;

ALTER TABLE cars_scrap_goonet_models
  ADD COLUMN IF NOT EXISTS last_listing_scrape_count INTEGER;

ALTER TABLE cars_scrap_goonet_models
  ADD COLUMN IF NOT EXISTS last_listing_scrape_error TEXT;

CREATE INDEX IF NOT EXISTS idx_cars_scrap_goonet_models_maker_cd
  ON cars_scrap_goonet_models (maker_cd);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_goonet_models_last_listing_scraped_at
  ON cars_scrap_goonet_models (last_listing_scraped_at);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_goonet_grades_maker_cd
  ON cars_scrap_goonet_grades (maker_cd);

CREATE INDEX IF NOT EXISTS idx_cars_scrap_goonet_grades_car_cd
  ON cars_scrap_goonet_grades (car_cd);
