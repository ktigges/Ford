-- Reset charging telemetry so charging collection can start fresh.
--
-- This script safely deletes charging_history, charging_sessions, and charging_state rows.
-- When clearing all VINs, it also resets the sequence for clean IDs.
--
-- ============================================================================
-- DATABASE_URL SETUP:
-- ============================================================================
--
-- Option 1: Set environment variable and run
--   export DATABASE_URL="postgresql://lightning:PASSWORD@localhost:5432/lightning"
--   psql "$DATABASE_URL" -f scripts/reset_charging_data.sql
--
-- Option 2: For live server (linux-web.tigges-us.com)
--   export DATABASE_URL="postgresql://lightning:PASSWORD@linux-web.tigges-us.com:5432/lightning"
--   psql "$DATABASE_URL" -f scripts/reset_charging_data.sql
--
-- Option 3: Inline connection string (include it directly)
--   psql "postgresql://lightning:PASSWORD@localhost:5432/lightning" -f scripts/reset_charging_data.sql
--
-- CONNECTION STRING FORMAT: postgresql://USERNAME:PASSWORD@HOST:PORT/DATABASE
--   USERNAME: Usually 'lightning' (check your database setup)
--   PASSWORD: Your database password
--   HOST:     'localhost' for local dev, or 'linux-web.tigges-us.com' for live
--   PORT:     5432 (default PostgreSQL port)
--   DATABASE: Usually 'lightning' (check your database name)
--
-- ============================================================================
-- EDIT TARGET_vin BELOW:
-- ============================================================================
--
-- - NULL                 -> reset all VINs (complete wipe of charging data)
-- - '1FT...'             -> reset only that specific VIN
--
-- After editing, run the script using one of the methods above.
--
-- This script deletes:
--   - charging_history rows
--   - charging_sessions rows
--   - charging_state rows
-- It also resets charging_history.id and charging_sessions.id sequences when clearing all VINs.

BEGIN;

DO $$
DECLARE
    target_vin TEXT := NULL;  -- Set to a VIN string to reset only one vehicle.
    hist_deleted BIGINT := 0;
    session_deleted BIGINT := 0;
    state_deleted BIGINT := 0;
    remaining_hist BIGINT := 0;
    remaining_sessions BIGINT := 0;
    remaining_state BIGINT := 0;
BEGIN
    -- Delete history first, then sessions, then current charging state snapshot.
    DELETE FROM charging_history
    WHERE target_vin IS NULL OR vin = target_vin;
    GET DIAGNOSTICS hist_deleted = ROW_COUNT;

    DELETE FROM charging_sessions
    WHERE target_vin IS NULL OR vin = target_vin;
    GET DIAGNOSTICS session_deleted = ROW_COUNT;

    DELETE FROM charging_state
    WHERE target_vin IS NULL OR vin = target_vin;
    GET DIAGNOSTICS state_deleted = ROW_COUNT;

    -- If all VINs were cleared, reset sequence to 1 for clean IDs.
    IF target_vin IS NULL THEN
        PERFORM setval(pg_get_serial_sequence('charging_history', 'id'), 1, false);
        PERFORM setval(pg_get_serial_sequence('charging_sessions', 'id'), 1, false);
    END IF;

    SELECT count(*) INTO remaining_hist
    FROM charging_history
    WHERE target_vin IS NULL OR vin = target_vin;

    SELECT count(*) INTO remaining_state
    FROM charging_state
    WHERE target_vin IS NULL OR vin = target_vin;

    SELECT count(*) INTO remaining_sessions
    FROM charging_sessions
    WHERE target_vin IS NULL OR vin = target_vin;

    RAISE NOTICE 'charging_history deleted: %', hist_deleted;
    RAISE NOTICE 'charging_sessions deleted: %', session_deleted;
    RAISE NOTICE 'charging_state deleted: %', state_deleted;
    RAISE NOTICE 'charging_history remaining in scope: %', remaining_hist;
    RAISE NOTICE 'charging_sessions remaining in scope: %', remaining_sessions;
    RAISE NOTICE 'charging_state remaining in scope: %', remaining_state;
END $$;

COMMIT;
