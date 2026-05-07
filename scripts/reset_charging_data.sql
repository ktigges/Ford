-- Reset charging telemetry so charging collection can start fresh.
--
-- Usage:
-- 1) Edit target_vin below:
--    - NULL  -> reset all VINs
--    - '1FT...' -> reset only that VIN
-- 2) Run with psql:
--    psql "$DATABASE_URL" -f scripts/reset_charging_data.sql
--
-- This script deletes:
--   - charging_history rows
--   - charging_state rows
-- It also resets charging_history.id sequence when clearing all VINs.

BEGIN;

DO $$
DECLARE
    target_vin TEXT := NULL;  -- Set to a VIN string to reset only one vehicle.
    hist_deleted BIGINT := 0;
    state_deleted BIGINT := 0;
    remaining_hist BIGINT := 0;
    remaining_state BIGINT := 0;
BEGIN
    -- Delete history first, then current charging state snapshot.
    DELETE FROM charging_history
    WHERE target_vin IS NULL OR vin = target_vin;
    GET DIAGNOSTICS hist_deleted = ROW_COUNT;

    DELETE FROM charging_state
    WHERE target_vin IS NULL OR vin = target_vin;
    GET DIAGNOSTICS state_deleted = ROW_COUNT;

    -- If all VINs were cleared, reset sequence to 1 for clean IDs.
    IF target_vin IS NULL THEN
        PERFORM setval(pg_get_serial_sequence('charging_history', 'id'), 1, false);
    END IF;

    SELECT count(*) INTO remaining_hist
    FROM charging_history
    WHERE target_vin IS NULL OR vin = target_vin;

    SELECT count(*) INTO remaining_state
    FROM charging_state
    WHERE target_vin IS NULL OR vin = target_vin;

    RAISE NOTICE 'charging_history deleted: %', hist_deleted;
    RAISE NOTICE 'charging_state deleted: %', state_deleted;
    RAISE NOTICE 'charging_history remaining in scope: %', remaining_hist;
    RAISE NOTICE 'charging_state remaining in scope: %', remaining_state;
END $$;

COMMIT;
