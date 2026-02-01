-- Table: price_history_scrap_mudahmy
-- Records price changes when `cars_scrap_mudahmy.price` changes on UPDATE/UPSERT.

CREATE TABLE IF NOT EXISTS price_history_scrap_mudahmy (
  id BIGSERIAL PRIMARY KEY,
  listing_id BIGINT NOT NULL REFERENCES cars_scrap_mudahmy(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  old_price INTEGER,
  new_price INTEGER,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_scrap_mudahmy_listing_id
  ON price_history_scrap_mudahmy (listing_id);

CREATE INDEX IF NOT EXISTS idx_price_history_scrap_mudahmy_changed_at
  ON price_history_scrap_mudahmy (changed_at);

CREATE OR REPLACE FUNCTION trg_log_mudahmy_car_price_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  -- Only log actual price changes (handles NULL <-> value too).
  IF NEW.price IS DISTINCT FROM OLD.price THEN
    INSERT INTO price_history_scrap_mudahmy (listing_id, listing_url, old_price, new_price, changed_at)
    VALUES (NEW.listing_id, NEW.listing_url, OLD.price, NEW.price, NOW());
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS log_mudahmy_car_price_change ON cars_scrap_mudahmy;

CREATE TRIGGER log_mudahmy_car_price_change
AFTER UPDATE OF price ON cars_scrap_mudahmy
FOR EACH ROW
EXECUTE FUNCTION trg_log_mudahmy_car_price_change();

