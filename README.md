# ⚡ Ford Lightning EV Tool — Prototype (Phase 1)

**Author:** Kevin Tigges  
**Version:** 0.1.0  
**Date:** 2026-04-26

---

## Purpose

The ultimate goal of this project is to train an AI model for **user-specific driving behaviour**, incorporating GEO information and charger location data to optimize range prediction and route planning for a Ford F-150 Lightning.

**Phase 1 (this prototype)** focuses solely on **telemetry collection** — making sure we can reliably authenticate with Ford's connected-vehicle API, poll raw telemetry, store it in PostgreSQL, and display it in a lightweight dashboard. The goal is to build a good, representative data sampling pipeline before layering on analytics.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Flask Web UI                        │
│  Dashboard · Vehicle State · Telemetry · Settings ...   │
└────────────────────────┬────────────────────────────────┘
                         │
                    ┌────┴─────┐
                    │  app.py  │  ← routes, Jinja templates
                    └────┬─────┘
         ┌───────────────┼───────────────┐
         │               │               │
    ┌────┴────┐   ┌──────┴─────┐   ┌─────┴─────┐
    │ oauth.py│   │ poller.py  │   │  units.py  │
    │  tokens │   │  API poll  │   │  unit conv │
    └────┬────┘   └──────┬─────┘   └────────────┘
         │               │
         └───────┬───────┘
            ┌────┴────┐
            │  db.py   │  ← psycopg2 connection pool
            └────┬────┘
            ┌────┴────┐
            │ PostgreSQL│  ← 18 tables (schema.sql)
            └─────────┘
```

All configuration is read from `config.json` via `config.py`.

---

## Files

### `config.py` — Configuration Loader

Reads `config.json` once at startup and caches the result. Exposes typed accessor functions so other modules never parse JSON directly.

| Function | Description |
|---|---|
| `load(path)` | Load configuration from disk. Caches after the first call so subsequent calls are free. |
| `get_config()` | Return the cached config dict, loading from disk if not yet loaded. |
| `database()` | Return the `database` section (host, port, name, user, password, connect_timeout). |
| `environment()` | Return the environment name (`development` or `production`). |
| `logging_config()` | Return logging settings (level, log_sql flag). |
| `collector_config()` | Return collector/poller settings (default interval, max failures). |

---

### `db.py` — Database Connection Pool

Thin wrapper around `psycopg2.pool.ThreadedConnectionPool`. Provides context managers for connections and cursors, plus convenience query helpers. All cursors use `RealDictCursor` so rows come back as Python dicts.

| Function | Description |
|---|---|
| `init_pool()` | Create the thread-safe connection pool from `config.json` database settings. Called once at app startup. |
| `close_pool()` | Shut down the pool and release all connections. |
| `get_conn()` | Context manager — borrow a connection from the pool, return it when done. |
| `get_cursor(commit)` | Context manager — yield a `RealDictCursor`. Auto-commits on success if `commit=True`, rolls back on exception. |
| `fetch_one(sql, params)` | Execute a SELECT and return the first row as a dict, or `None`. |
| `fetch_all(sql, params)` | Execute a SELECT and return all rows as a list of dicts. |
| `execute(sql, params)` | Execute a write statement (INSERT/UPDATE/DELETE) and auto-commit. |
| `execute_returning(sql, params)` | Execute a write with a `RETURNING` clause and return the first result row. |
| `active_vin()` | Return the most recently updated VIN from the `garage` table, or `None` if the garage is empty. |

---

### `oauth.py` — OAuth2 Token Management

Self-contained OAuth module for Ford's Azure AD B2C endpoint. Handles token refresh via `multipart/form-data` (Ford's requirement), rotation-safe refresh-token storage, and JWT claim diagnostics for debugging.

| Function | Description |
|---|---|
| `_decode_jwt_claims(token)` | Decode JWT payload without signature verification — for diagnostic logging only, never auth decisions. |
| `log_token_diagnostics(token, context)` | Log non-sensitive JWT claims (aud, scope, exp, iss) to help debug 401/403 errors. |
| `get_credentials(provider, vin)` | Look up the enabled `oauth_credentials` row for a provider/VIN pair from the database. |
| `get_valid_access_token(provider, vin)` | Return a valid access token, refreshing automatically if expired. Returns `None` if credentials are missing or refresh fails. |
| `_build_token_fields(creds)` | Build multipart form fields for a token refresh request. Uses `redirect_url` (Ford's non-standard field name). |
| `refresh_access_token(creds)` | Perform a refresh-token grant against Ford's token endpoint. Persists the new tokens and handles rotation-safe refresh-token updates. |
| `validate_credentials(form_data)` | Test OAuth credentials by attempting a token refresh. Used by the setup UI to verify before saving. Returns `(token_data, None)` or `(None, error_message)`. |
| `save_credentials(provider, vin, form_data, token_data)` | Insert or upsert OAuth credentials with validated token data into the database. |
| `_persist_tokens(cred_id, ...)` | Internal helper to update access_token, expiry, and refresh_token on an existing credential row. |

---

### `poller.py` — Background Telemetry Poller

Daemon thread that periodically calls Ford's telemetry API and stores the results. Handles retry with exponential backoff, classified error responses, and adaptive polling intervals.

#### Control API

| Function | Description |
|---|---|
| `is_running()` | Check whether the poller thread is currently alive. |
| `start()` | Spawn the daemon poller thread. Returns `False` if already running. |
| `stop()` | Signal the poller to stop gracefully after the current cycle. |

#### Core Polling

| Function | Description |
|---|---|
| `_poll_loop()` | Main loop: runs in a daemon thread, polls at adaptive intervals based on ignition/charging/moving state, and stops after max consecutive failures. |
| `_do_poll(provider, vin)` | Execute one poll cycle: get token → fetch telemetry → store raw JSON → upsert all state tables. |
| `poll_once(provider, vin)` | Public alias to run a single poll (used by tests or manual triggers). |
| `initial_setup_poll(provider, vin=None)` | 4-step first-run sequence: get token → fetch garage (discover VIN) → fetch telemetry → store everything. Returns the discovered VIN. |

#### Ford API Interaction

| Function | Description |
|---|---|
| `_build_headers(token, application_id)` | Build HTTP headers required by Ford's API (Bearer token, Application-Id, api-version, User-Agent). |
| `_ford_get(url, token, application_id, label)` | Make a GET request with retry/backoff. Logs request and response to debug files. Classifies errors into auth, entitlement, or service errors. |
| `fetch_garage(token, application_id)` | Call `/fcon-query/v1/garage` to get vehicle metadata (make, model, year, etc.). |
| `fetch_telemetry(token, application_id)` | Call `/fcon-query/v1/telemetry` to get the full metrics snapshot. |
| `_store_garage_data(garage_data)` | Parse the garage API response (which may contain multiple vehicles) and upsert into the `garage` table. Returns the first discovered VIN. |

#### State Upsert Helpers

Each function extracts specific fields from Ford's `metrics` dict and upserts a single row into the corresponding PostgreSQL state table. Ford sends data in metric/SI units; values are stored as-is.

| Function | Target Table | Key Fields |
|---|---|---|
| `_upsert_vehicle_state()` | `vehicle_state` | ignition, speed (km/h), gear, odometer (km), lifecycle mode |
| `_upsert_battery_state()` | `battery_state` | SOC %, actual SOC %, energy remaining (kWh), capacity, voltage, current, temperature (°C), range (km) |
| `_upsert_charging_state()` | `charging_state` | plug status, charger power type, communication status, time to full (min), charger current/voltage |
| `_upsert_location_state()` | `location_state` | latitude, longitude, altitude (m), heading (°), compass direction |
| `_upsert_tire_state()` | `tire_state` | per-wheel pressure (kPa), status, placard pressure |
| `_upsert_door_state()` | `door_state` | per-door status, lock status, presence — merged from 3 separate Ford arrays |
| `_upsert_window_state()` | `window_state` | per-window open range (lowerBound/upperBound) |
| `_upsert_brake_state()` | `brake_state` | brake pedal, brake torque (Nm), parking brake, wheel torque, transmission torque |
| `_upsert_security_state()` | `security_state` | alarm status, panic alarm, remote start countdown |
| `_upsert_environment_state()` | `environment_state` | ambient temp (°C), outside temp (°C) |
| `_upsert_vehicle_configuration()` | `vehicle_configuration` | remote start duration, software update settings, battery target range |
| `_upsert_departure_schedules()` | `departure_schedule` | per-schedule rows with day/time, status, OEM data |

#### Utility

| Function | Description |
|---|---|
| `_v(raw, *keys, default)` | Safely traverse nested dicts without raising `KeyError`. |
| `_door_key(entry)` | Build a composite door identifier from Ford's multi-field door entry (vehicleDoor + vehicleSide). |
| `_mask_token(token)` | Truncate a token string for safe log output. |
| `_log_request()` / `_log_response()` | Log HTTP request/response details to console (brief) and debug file (full). |
| `_classify_and_raise(resp, label)` | Inspect a non-2xx response and raise a typed exception (auth, entitlement, or service error). |
| `_backoff_wait(attempt)` | Sleep with exponential backoff between retries. |
| `_get_poll_interval(vin, default)` | Determine the polling interval from `polling_config` + current vehicle/charging state. |
| `_record_failure(vin, error)` | Record a poll failure in `collector_status`, incrementing the consecutive failure counter. |

---

### `units.py` — Unit Conversion

Ford transmits all telemetry in SI/metric (km, km/h, °C, kPa, meters, kWh, volts, amps) regardless of the vehicle's display setting. This module converts values at display time based on the user's unit preference.

| Function | Description |
|---|---|
| `km_to_mi()` / `mi_to_km()` | Convert between kilometers and miles. |
| `kmh_to_mph()` / `mph_to_kmh()` | Convert between km/h and mph. |
| `c_to_f()` / `f_to_c()` | Convert between Celsius and Fahrenheit. |
| `kpa_to_psi()` / `psi_to_kpa()` | Convert between kilopascals and PSI. |
| `m_to_ft()` / `ft_to_m()` | Convert between meters and feet. |
| `nm_to_lbft()` | Convert Newton-meters to lb-ft. |
| `convert(val, from_unit, to_unit)` | Generic converter using the dispatch table. |
| `unit_label(category, system)` | Return the display label for a measurement category (e.g., "mph" or "km/h"). |
| `convert_for_display(val, field_name, system)` | Auto-convert a DB value based on its column name and the selected unit system. This is the function injected into Jinja templates. |

---

### `app.py` — Flask Web Application

The main application factory. Creates the Flask app, initializes logging and database, registers all routes and Jinja context processors for unit conversion.

#### Routes

| Route | Method | Description |
|---|---|---|
| `/` | GET | **Dashboard** — vehicle overview, battery summary, poller status. |
| `/vehicle` | GET | **Vehicle State** — detailed view of all state tables with unit-converted values. |
| `/telemetry` | GET | **Telemetry** — poll count, latest timestamp, recent poll history. |
| `/oauth` | GET, POST | **OAuth Config** — enter/update Ford API credentials. Validates by attempting a token refresh, then triggers initial data poll. |
| `/poller` | GET, POST | **Poller Control** — start/stop the background poller, view collector status. |
| `/settings` | GET, POST | **Settings** — toggle metric/imperial display, configure polling intervals. |
| `/manage` | GET | **Manage Vehicles** — list all VINs with per-table row counts, delete VINs, re-poll. |
| `/manage/delete-vin` | POST | Delete a VIN and all cascaded data. |
| `/manage/repoll` | POST | Re-run the initial setup poll for the active VIN. |
| `/reset` | GET, POST | **Factory Reset** — delete all data for the active VIN, return to setup. |
| `/db` | GET | **Database Browser** — list all tables with row counts. |
| `/db/<table>` | GET | View table contents with secret masking and delete buttons. |
| `/db/<table>/delete` | POST | Delete a specific row from a table. |
| `/db/<table>/<id>` | GET | Row detail with expanded JSON for JSONB columns. |

---

### `schema.sql` — PostgreSQL Schema

18 tables covering vehicle metadata, telemetry logs, per-domain state tables, OAuth credentials, polling configuration, and application settings. All vehicle tables reference `garage(vin)` with `ON DELETE CASCADE`.

---

### `config.json` — Application Configuration

```json
{
  "environment": "development",
  "database": { "host", "port", "name", "user", "password" },
  "vehicle": { "vin": "1FT6W5L78RWG14285" },
  "logging": { "level": "INFO" },
  "collector": { "default_poll_interval_sec": 60, "max_consecutive_failures": 5 }
}
```

---

## Templates

| Template | Description |
|---|---|
| `base.html` | Base layout with nav bar, prototype banner, flash messages. |
| `dashboard.html` | Main dashboard with vehicle info, battery, poller status cards. |
| `vehicle_state.html` | All state tables displayed as cards with unit-converted values. |
| `telemetry.html` | Telemetry overview with poll count and recent history. |
| `oauth_config.html` | OAuth credential entry form with pre-population from DB. |
| `poller.html` | Poller start/stop controls and collector status. |
| `settings.html` | Unit system toggle and polling interval configuration. |
| `manage.html` | Vehicle management — list VINs, delete, re-poll, orphan detection. |
| `reset.html` | Factory reset confirmation page. |
| `db_browser.html` | Database table listing with row counts. |
| `db_table.html` | Table content viewer with delete buttons and secret masking. |
| `db_row_detail.html` | Single row detail with JSON expansion. |

---

## Quick Start

```bash
# 1. Start PostgreSQL (Docker)
docker volume create lightning_pgdata
docker run -d \
  --name lightning-db \
  -e POSTGRES_USER=lightning \
  -e POSTGRES_PASSWORD=lightningpass \
  -e POSTGRES_DB=lightning \
  -p 5432:5432 \
  -v lightning_pgdata:/var/lib/postgresql/data \
  postgres:16

# 2. Apply schema
docker exec -i lightning-db psql -U lightning -d lightning < schema.sql

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Run the app
python app.py
```

Open `http://localhost:5000`. You'll be redirected to the OAuth config page on first run.

---

## Ford API Notes

- **Host:** `api.vehicle.ford.com` (NOT `api.mps.ford.com`)
- **Token endpoint:** `https://api.vehicle.ford.com/dah2vb2cprod.onmicrosoft.com/oauth2/v2.0/token?p=B2C_1A_FCON_AUTHORIZE`
- **Garage:** `GET /fcon-query/v1/garage`
- **Telemetry:** `GET /fcon-query/v1/telemetry`
- Token refresh uses **multipart/form-data** (not URL-encoded)
- Ford uses `redirect_url` (not `redirect_uri`) in the token form
- Requires `Application-Id` header set to the OAuth client_id
- All numeric telemetry is in **SI/metric** regardless of the vehicle's display setting

---

## Phase 1 Scope

- [x] OAuth2 authentication with Ford's Azure AD B2C
- [x] Garage and telemetry API polling
- [x] Raw telemetry storage (JSONB) + parsed state tables
- [x] Web dashboard with unit conversion (metric/imperial)
- [x] Background poller with adaptive intervals
- [x] Database viewer and vehicle management
- [ ] GEO information integration (Phase 2)
- [ ] Charger location data (Phase 2)
- [ ] AI model training pipeline (Phase 3)

---

## License

Private prototype — not for distribution.
