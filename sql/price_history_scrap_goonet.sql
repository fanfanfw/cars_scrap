-- Table: price_history_scrap_goonet
-- Records price changes when `cars_scrap_goonet.price_jpy` or `cars_scrap_goonet.price_usd` changes on UPDATE/UPSERT.

CREATE TABLE IF NOT EXISTS price_history_scrap_goonet (
  id BIGSERIAL PRIMARY KEY,
  listing_id VARCHAR NOT NULL REFERENCES cars_scrap_goonet(listing_id) ON UPDATE CASCADE ON DELETE CASCADE,
  listing_url TEXT NOT NULL,
  old_price_jpy BIGINT,
  new_price_jpy BIGINT,
  old_price_usd BIGINT,
  new_price_usd BIGINT,
  changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_scrap_goonet_listing_id
  ON price_history_scrap_goonet (listing_id);

CREATE INDEX IF NOT EXISTS idx_price_history_scrap_goonet_changed_at
  ON price_history_scrap_goonet (changed_at);

CREATE OR REPLACE FUNCTION trg_log_goonet_car_price_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.price_jpy IS DISTINCT FROM OLD.price_jpy OR NEW.price_usd IS DISTINCT FROM OLD.price_usd THEN
    INSERT INTO price_history_scrap_goonet (
      listing_id,
      listing_url,
      old_price_jpy,
      new_price_jpy,
      old_price_usd,
      new_price_usd,
      changed_at
    )
    VALUES (
      NEW.listing_id,
      NEW.listing_url,
      OLD.price_jpy,
      NEW.price_jpy,
      OLD.price_usd,
      NEW.price_usd,
      NOW()
    );
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS log_goonet_car_price_change ON cars_scrap_goonet;

CREATE TRIGGER log_goonet_car_price_change
AFTER UPDATE OF price_jpy, price_usd ON cars_scrap_goonet
FOR EACH ROW
EXECUTE FUNCTION trg_log_goonet_car_price_change();
