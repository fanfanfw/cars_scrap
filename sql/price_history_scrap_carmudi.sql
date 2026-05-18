-- Table: price_history_scrap_carmudi
-- Records price changes when `cars_scrap_carmudi.price` changes on UPDATE/UPSERT.

CREATE TABLE IF NOT EXISTS price_history_scrap_carmudi (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL REFERENCES cars_scrap_carmudi(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  old_price BIGINT,
  new_price BIGINT,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_scrap_carmudi_listing_id
  ON price_history_scrap_carmudi (listing_id);

CREATE INDEX IF NOT EXISTS idx_price_history_scrap_carmudi_changed_at
  ON price_history_scrap_carmudi (changed_at);

CREATE OR REPLACE FUNCTION trg_log_carmudi_car_price_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  -- Only log actual price changes (handles NULL <-> value too).
  IF NEW.price IS DISTINCT FROM OLD.price THEN
    INSERT INTO price_history_scrap_carmudi (listing_id, listing_url, old_price, new_price, changed_at)
    VALUES (NEW.listing_id, NEW.listing_url, OLD.price, NEW.price, NOW());
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS log_carmudi_car_price_change ON cars_scrap_carmudi;

CREATE TRIGGER log_carmudi_car_price_change
AFTER UPDATE OF price ON cars_scrap_carmudi
FOR EACH ROW
EXECUTE FUNCTION trg_log_carmudi_car_price_change();
