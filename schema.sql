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

    time_to_full_min REAL CHECK (time_to_full_min >= 0),
    charger_current REAL,
    charger_voltage REAL,
    evse_dc_current REAL
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

    -- Battery
    soc_percent REAL,
    actual_soc_percent REAL,
    energy_remaining_kwh REAL,
    battery_voltage REAL,
    battery_current REAL,
    battery_temp_c REAL,

    -- Powertrain
    motor_current REAL,
    motor_voltage REAL,
    torque_at_transmission REAL,
    accelerator_pedal_pct REAL,
    brake_torque REAL,

    -- Environment
    ambient_temp_c REAL,
    outside_temp_c REAL,

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