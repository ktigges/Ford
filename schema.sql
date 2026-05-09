CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =========================
-- Core vehicle metadata
-- =========================
CREATE TABLE garage (
    vin TEXT PRIMARY KEY,
    vehicle_id UUID,
    nickname TEXT,
    make TEXT,
    model_name TEXT,
    model_code TEXT,
    model_year INTEGER,
    vehicle_type TEXT,
    color TEXT,
    engine_type TEXT,
    tcu_enabled BOOLEAN,
    ng_sdn_managed BOOLEAN,
    vehicle_authorization_indicator BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- Immutable telemetry log
-- =========================
CREATE TABLE telemetry (
    id BIGSERIAL PRIMARY KEY,
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    vehicle_id UUID,

    polled_at TIMESTAMPTZ NOT NULL,
    poll_interval_seconds INTEGER CHECK (poll_interval_seconds > 0),

    raw_metrics JSONB NOT NULL,

    created_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- Vehicle state
-- =========================
CREATE TABLE vehicle_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    ignition_status TEXT,
    speed_mph REAL CHECK (speed_mph >= 0),
    gear_position TEXT,
    odometer_miles REAL CHECK (odometer_miles >= 0),
    lifecycle_mode TEXT
);

-- =========================
-- Battery state
-- =========================
CREATE TABLE battery_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    soc_percent REAL CHECK (soc_percent BETWEEN 0 AND 100),
    actual_soc_percent REAL CHECK (actual_soc_percent BETWEEN 0 AND 100),
    energy_remaining_kwh REAL CHECK (energy_remaining_kwh >= 0),
    capacity_kwh REAL CHECK (capacity_kwh >= 0),

    voltage REAL,
    current REAL,
    temperature_c REAL,
    performance_status TEXT,
    load_status TEXT,
    range_miles REAL CHECK (range_miles >= 0)
);

-- =========================
-- Charging state
-- =========================
CREATE TABLE charging_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    plug_status TEXT,
    charger_power_type TEXT,
    communication_status TEXT,
    charge_display_status TEXT,
    
    time_to_full_min REAL CHECK (time_to_full_min >= 0),
    charger_current REAL,
    charger_voltage REAL,
    evse_dc_current REAL
);

CREATE TABLE charging_history (
    id BIGSERIAL PRIMARY KEY,
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    polled_at TIMESTAMPTZ NOT NULL,
    charging_session_uuid UUID,

    plug_status TEXT,
    charger_power_type TEXT,
    communication_status TEXT,
    time_to_full_min REAL CHECK (time_to_full_min >= 0),
    charger_current REAL,
    charger_voltage REAL,
    evse_dc_current REAL,
    charge_power_kw REAL,

    soc_percent REAL CHECK (soc_percent BETWEEN 0 AND 100),
    actual_soc_percent REAL CHECK (actual_soc_percent BETWEEN 0 AND 100),
    energy_remaining_kwh REAL CHECK (energy_remaining_kwh >= 0),
    battery_temp_c REAL,
    outside_temp_c REAL,
    ambient_temp_c REAL,

    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE charging_sessions (
    id BIGSERIAL PRIMARY KEY,
    session_uuid UUID NOT NULL UNIQUE,
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    started_at TIMESTAMPTZ NOT NULL,
    last_update TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    in_progress BOOLEAN NOT NULL DEFAULT TRUE,

    charger_power_type TEXT,
    start_soc_percent REAL CHECK (start_soc_percent BETWEEN 0 AND 100),
    end_soc_percent REAL CHECK (end_soc_percent BETWEEN 0 AND 100),
    start_energy_remaining_kwh REAL CHECK (start_energy_remaining_kwh >= 0),
    end_energy_remaining_kwh REAL CHECK (end_energy_remaining_kwh >= 0),
    max_power_kw REAL CHECK (max_power_kw >= 0),
    sample_count INTEGER NOT NULL DEFAULT 1 CHECK (sample_count >= 1),

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- Location / navigation state
-- =========================
CREATE TABLE location_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude_m DOUBLE PRECISION,

    heading_deg REAL CHECK (heading_deg BETWEEN 0 AND 360),
    compass_direction TEXT
);

-- =========================
-- Tire state (per wheel)
-- =========================
CREATE TABLE tire_state (
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    wheel_position TEXT NOT NULL,

    pressure_kpa REAL CHECK (pressure_kpa >= 0),
    status TEXT,
    placard_kpa REAL CHECK (placard_kpa >= 0),
    last_update TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (vin, wheel_position)
);

-- =========================
-- Door state
-- =========================
CREATE TABLE door_state (
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    door TEXT NOT NULL,

    status TEXT,
    lock_status TEXT,
    presence_status TEXT,
    last_update TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (vin, door)
);

-- =========================
-- Window state
-- =========================

CREATE TABLE window_state (
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    window_position TEXT NOT NULL,

    lower_bound REAL,
    upper_bound REAL,
    last_update TIMESTAMPTZ NOT NULL,

    PRIMARY KEY (vin, window_position)
);


-- =========================
-- Brake & torque state
-- =========================
CREATE TABLE brake_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    brake_pedal_status TEXT,
    brake_torque REAL,
    parking_brake_status TEXT,
    wheel_torque_status TEXT,
    transmission_torque REAL
);

-- =========================
-- Security / alarm state
-- =========================
CREATE TABLE security_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    alarm_status TEXT,
    panic_alarm_status TEXT,
    remote_start_countdown REAL CHECK (remote_start_countdown >= 0)
);

-- =========================
-- Environmental state
-- =========================
CREATE TABLE environment_state (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    ambient_temp_c REAL,
    outside_temp_c REAL
);

-- =========================
-- Vehicle configuration
-- =========================
CREATE TABLE vehicle_configuration (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_update TIMESTAMPTZ NOT NULL,

    remote_start_duration_sec REAL CHECK (remote_start_duration_sec >= 0),
    software_update_opt_in TEXT,
    software_update_schedule JSONB,
    battery_target_range_setting JSONB
);

-- =========================
-- Departure / charge schedules
-- =========================
CREATE TABLE departure_schedule (
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,
    schedule_id TEXT NOT NULL,

    status TEXT,
    schedule JSONB,
    desired_temperature JSONB,
    oem_data JSONB,

    PRIMARY KEY (vin, schedule_id)
);

-- =========================
-- Polling configuration
-- =========================
CREATE TABLE polling_config (
    id SERIAL PRIMARY KEY,
    vin TEXT REFERENCES garage(vin) ON DELETE CASCADE,

    ignition_off_interval_sec INTEGER NOT NULL CHECK (ignition_off_interval_sec > 0),
    ignition_on_interval_sec INTEGER NOT NULL CHECK (ignition_on_interval_sec > 0),
    moving_interval_sec INTEGER NOT NULL CHECK (moving_interval_sec > 0),
    charging_interval_sec INTEGER NOT NULL CHECK (charging_interval_sec > 0),

    min_poll_interval_sec INTEGER DEFAULT 5 CHECK (min_poll_interval_sec > 0),
    max_poll_interval_sec INTEGER DEFAULT 300 CHECK (max_poll_interval_sec > min_poll_interval_sec),

    enabled BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- Collector health / status
-- =========================
CREATE TABLE collector_status (
    vin TEXT PRIMARY KEY REFERENCES garage(vin) ON DELETE CASCADE,
    last_poll TIMESTAMPTZ,
    last_success TIMESTAMPTZ,
    last_error TEXT,
    consecutive_failures INTEGER DEFAULT 0 CHECK (consecutive_failures >= 0)
);

-- =========================
-- Application config
-- =========================
CREATE TABLE app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- OAuth credentials
-- =========================
CREATE TABLE oauth_credentials (
    id SERIAL PRIMARY KEY,

    provider TEXT NOT NULL,
    vin TEXT REFERENCES garage(vin) ON DELETE CASCADE,

    client_id TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    scope TEXT NOT NULL,
    refresh_token TEXT NOT NULL,

    token_endpoint TEXT NOT NULL,

    access_token TEXT,
    access_token_expires_at TIMESTAMPTZ,

    enabled BOOLEAN DEFAULT TRUE,

    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (provider, vin)
);

-- =========================
-- Drive sessions
-- =========================
CREATE TABLE drives (
    id BIGSERIAL PRIMARY KEY,
    drive_uuid UUID NOT NULL DEFAULT gen_random_uuid(),
    vin TEXT NOT NULL REFERENCES garage(vin) ON DELETE CASCADE,

    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,

    -- Odometer at start/end (km from Ford)
    start_odometer_km REAL,
    end_odometer_km REAL,
    distance_km REAL,

    -- Battery at start/end
    start_soc_percent REAL,
    end_soc_percent REAL,
    start_energy_kwh REAL,
    end_energy_kwh REAL,
    energy_used_kwh REAL,

    -- Environment
    avg_ambient_temp_c REAL,
    avg_outside_temp_c REAL,

    -- Location at start/end
    start_lat DOUBLE PRECISION,
    start_lon DOUBLE PRECISION,
    start_heading_deg REAL,
    start_compass TEXT,
    end_lat DOUBLE PRECISION,
    end_lon DOUBLE PRECISION,
    end_heading_deg REAL,
    end_compass TEXT,

    -- Status
    in_progress BOOLEAN DEFAULT TRUE,

    -- Derived stats (updated on drive end)
    duration_sec REAL,
    max_speed_kmh REAL,
    regen_energy_kwh REAL,

    created_at TIMESTAMPTZ DEFAULT now(),

    UNIQUE (drive_uuid)
);

CREATE TABLE drive_points (
    id BIGSERIAL PRIMARY KEY,
    drive_id BIGINT NOT NULL REFERENCES drives(id) ON DELETE CASCADE,

    recorded_at TIMESTAMPTZ NOT NULL,

    -- Motion
    speed_kmh REAL,
    odometer_km REAL,
    heading_deg REAL,
    compass_direction TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    altitude_m DOUBLE PRECISION,
    gear_position TEXT,
    ignition_status TEXT,

    -- Battery
    soc_percent REAL,
    actual_soc_percent REAL,
    energy_remaining_kwh REAL,
    battery_voltage REAL,
    battery_current REAL,
    battery_temp_c REAL,
    battery_max_range_km REAL,

    -- Powertrain
    motor_current REAL,
    motor_voltage REAL,
    torque_at_transmission REAL,
    accelerator_pedal_pct REAL,
    brake_torque REAL,
    hybrid_mode TEXT,

    -- Trip computer (from Ford)
    trip_distance_km REAL,
    trip_regen_range_km REAL,
    trip_regen_charge_kwh REAL,
    trip_fuel_economy REAL,

    -- Environment
    ambient_temp_c REAL,
    outside_temp_c REAL,
    engine_coolant_temp_c REAL,

    created_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- Indexes
-- =========================
CREATE INDEX idx_telemetry_vin_time ON telemetry (vin, polled_at);
CREATE INDEX idx_telemetry_time ON telemetry (polled_at);
CREATE INDEX idx_telemetry_raw_metrics ON telemetry USING GIN (raw_metrics);
CREATE INDEX idx_location_latlon ON location_state (latitude, longitude);
CREATE INDEX idx_drives_vin_time ON drives (vin, started_at);
CREATE INDEX idx_drives_uuid ON drives (drive_uuid);
CREATE INDEX idx_drives_in_progress ON drives (vin) WHERE in_progress = TRUE;
CREATE INDEX idx_drive_points_drive_time ON drive_points (drive_id, recorded_at);
CREATE INDEX idx_charging_history_vin_time ON charging_history (vin, polled_at DESC);
CREATE INDEX idx_charging_history_session_uuid ON charging_history (charging_session_uuid);
CREATE INDEX idx_charging_sessions_vin_start ON charging_sessions (vin, started_at DESC);
CREATE INDEX idx_charging_sessions_open ON charging_sessions (vin) WHERE in_progress = TRUE;

-- =========================
-- EV Charger Network (NLR)
-- =========================
CREATE TABLE ev_stations (
    id BIGSERIAL PRIMARY KEY,
    nlr_station_id BIGINT NOT NULL UNIQUE,
    
    station_name TEXT NOT NULL,
    street_address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    country TEXT DEFAULT 'US',
    
    -- Geo (for route planning and nearest-charger queries)
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    
    -- Status and access
    status_code TEXT,  -- 'E' = operational
    fuel_type_code TEXT DEFAULT 'ELEC',
    access_code TEXT,  -- 'public', 'private', etc.
    access_detail TEXT,
    owner_type_code TEXT,
    facility_type TEXT,
    
    -- Network operator info
    network_name TEXT,  -- e.g., 'SHELL_RECHARGE', 'Non-Networked'
    
    -- Timestamps
    updated_at TIMESTAMPTZ DEFAULT now(),
    nlr_updated_at TIMESTAMPTZ,  -- Last update from NLR API
    created_at TIMESTAMPTZ DEFAULT now(),
    
    -- Raw data for future schema evolution
    raw_data JSONB,
    
    UNIQUE (nlr_station_id)
);

CREATE TABLE ev_charger_connectors (
    id BIGSERIAL PRIMARY KEY,
    station_id BIGINT NOT NULL REFERENCES ev_stations(id) ON DELETE CASCADE,
    nlr_station_id BIGINT NOT NULL,
    
    -- Connector type (J1772, CHADEMO, J1772COMBO, etc.)
    connector_type TEXT NOT NULL,
    
    -- Network / charging level
    network TEXT,  -- 'SHELL_RECHARGE', 'Non-Networked', etc.
    charging_level TEXT,  -- 'level_1', 'level_2', 'dc_fast', etc.
    
    -- Power capacity
    power_kw REAL,  -- Max power output in kW
    port_count INTEGER,  -- Number of ports for this connector type
    
    updated_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now(),
    
    PRIMARY KEY (station_id, connector_type, network),
    UNIQUE (station_id, connector_type, network)
);

-- Track import/sync operations for audit trail and delta updates
CREATE TABLE ev_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    sync_type TEXT NOT NULL,  -- 'manual_import', 'scheduled_sync', etc.
    state_filter TEXT,  -- US state code or 'all'
    status TEXT NOT NULL,  -- 'in_progress', 'completed', 'failed'
    
    -- Timestamps
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    
    -- Statistics
    stations_imported INTEGER DEFAULT 0,
    stations_updated INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    
    created_at TIMESTAMPTZ DEFAULT now()
);

-- =========================
-- Indexes for charger queries
-- =========================
CREATE INDEX idx_ev_stations_state ON ev_stations (state) WHERE country = 'US';
CREATE INDEX idx_ev_stations_location ON ev_stations USING GIST (
    ll_to_earth(latitude, longitude)
);
CREATE INDEX idx_ev_connectors_network ON ev_charger_connectors (network);
CREATE INDEX idx_ev_connectors_charging_level ON ev_charger_connectors (charging_level);
CREATE INDEX idx_ev_sync_runs_status ON ev_sync_runs (status);
CREATE INDEX idx_ev_sync_runs_started ON ev_sync_runs (started_at DESC);