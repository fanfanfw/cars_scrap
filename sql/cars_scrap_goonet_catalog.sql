CREATE TABLE IF NOT EXISTS cars_scrap_goonet_catalog_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'running',
  makes_count INTEGER NOT NULL DEFAULT 0,
  models_count INTEGER NOT NULL DEFAULT 0,
  specs_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  phase TEXT,
  current_make_slug TEXT,
  current_model_slug TEXT,
  heartbeat_at TIMESTAMPTZ,
  resumed_from_run_id BIGINT
);

ALTER TABLE cars_scrap_goonet_catalog_runs ADD COLUMN IF NOT EXISTS phase TEXT;
ALTER TABLE cars_scrap_goonet_catalog_runs ADD COLUMN IF NOT EXISTS current_make_slug TEXT;
ALTER TABLE cars_scrap_goonet_catalog_runs ADD COLUMN IF NOT EXISTS current_model_slug TEXT;
ALTER TABLE cars_scrap_goonet_catalog_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE cars_scrap_goonet_catalog_runs ADD COLUMN IF NOT EXISTS resumed_from_run_id BIGINT;

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_catalog_makes (
  id BIGSERIAL PRIMARY KEY,
  make_slug TEXT NOT NULL UNIQUE,
  make_name TEXT NOT NULL,
  country TEXT,
  make_url TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status TEXT NOT NULL DEFAULT 'active',
  raw JSONB
);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_makes_country
  ON cars_scrap_goonet_catalog_makes (country);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_makes_status
  ON cars_scrap_goonet_catalog_makes (status);

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_catalog_models (
  id BIGSERIAL PRIMARY KEY,
  model_slug TEXT NOT NULL UNIQUE,
  make_slug TEXT NOT NULL REFERENCES cars_scrap_goonet_catalog_makes(make_slug) ON UPDATE CASCADE ON DELETE CASCADE,
  model_name TEXT NOT NULL,
  production_period TEXT,
  production_start TEXT,
  production_end TEXT,
  model_url TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  status TEXT NOT NULL DEFAULT 'active',
  raw JSONB
);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_models_make_slug
  ON cars_scrap_goonet_catalog_models (make_slug);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_models_status
  ON cars_scrap_goonet_catalog_models (status);

CREATE TABLE IF NOT EXISTS cars_scrap_goonet_catalog_specs (
  id BIGSERIAL PRIMARY KEY,
  catalog_spec_id TEXT NOT NULL UNIQUE,
  make_slug TEXT NOT NULL REFERENCES cars_scrap_goonet_catalog_makes(make_slug) ON UPDATE CASCADE ON DELETE CASCADE,
  model_slug TEXT NOT NULL REFERENCES cars_scrap_goonet_catalog_models(model_slug) ON UPDATE CASCADE ON DELETE CASCADE,
  make_name TEXT,
  model_name TEXT,
  variant_name TEXT,
  release_month TEXT,
  sale_title TEXT,
  spec_url TEXT NOT NULL,
  model_code TEXT,
  engine TEXT,
  doors INTEGER,
  transmission TEXT,
  drive TEXT,
  dimension TEXT,
  weight TEXT,
  msrp TEXT,
  data JSONB NOT NULL DEFAULT '{}'::jsonb,
  raw_list_row JSONB,
  content_hash TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_changed_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_specs_make_slug
  ON cars_scrap_goonet_catalog_specs (make_slug);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_specs_model_slug
  ON cars_scrap_goonet_catalog_specs (model_slug);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_specs_release_month
  ON cars_scrap_goonet_catalog_specs (release_month);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_specs_status
  ON cars_scrap_goonet_catalog_specs (status);

CREATE INDEX IF NOT EXISTS idx_goonet_catalog_specs_data_gin
  ON cars_scrap_goonet_catalog_specs USING GIN (data);
