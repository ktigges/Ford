# ⚡ Ford Lightning EV Tool — Prototype (Phase 1)

**Author:** Kevin Tigges  
**Version:** 0.3.2  
**Date:** 2026-05-02

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
         ┌───────┬───────┼───────┬───────────┐
         │       │       │       │           │
    ┌────┴────┐ ┌┴──────┐│ ┌────┴─────┐ ┌───┴──────┐
    │ oauth.py│ │backup ││ │ units.py │ │crypto.py │
    │  tokens │ │  .py  ││ │ unit conv│ │encryption│
    └────┬────┘ └───┬───┘│ └──────────┘ └──────────┘
         │          │    │
         └──────────┼────┘
               ┌────┴────┐
               │ poller.py│  ← background daemon thread
               └────┬─────┘
               ┌────┴────┐
               │  db.py  │  ← psycopg2 connection pool
               └────┬────┘
               ┌────┴─────┐
               │ PostgreSQL│  ← 21 tables (schema.sql)
               └──────────┘
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
| `flask_port()` | Return the Flask listening port from config (default 5000). |
| `ssl_config()` | Return SSL/TLS settings (enabled, cert path, key path). |
| `save_database(db_settings)` | Update the database section in `config.json` and reload the cached config. Used by the DB setup page. |
| `save_ssl(ssl_settings)` | Update the ssl section in `config.json` and reload. Used by the SSL settings UI. |

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
| `is_available()` | Return `True` if the connection pool is initialised and the database is reachable. Used by setup mode. |
| `test_connection(host, port, ...)` | Test a database connection without affecting the pool. Returns `(success, message)`. |
| `apply_schema()` | Apply `schema.sql` to the connected database to create all tables. Returns `(success, message)`. |

---

### `oauth.py` — OAuth2 Token Management

Self-contained OAuth module for Ford's Azure AD B2C endpoint. Handles token refresh via `multipart/form-data` (Ford's requirement), rotation-safe refresh-token storage, and JWT claim diagnostics for debugging.

| Function | Description |
|---|---|
| `_decode_jwt_claims(token)` | Decode JWT payload without signature verification — for diagnostic logging only, never auth decisions. |
| `log_token_diagnostics(token, context)` | Log non-sensitive JWT claims (aud, scope, exp, iss) to help debug 401/403 errors. |
| `get_credentials(provider, vin)` | Look up the enabled `oauth_credentials` row for a provider/VIN pair. The `client_secret` is transparently decrypted before being returned. |
| `get_valid_access_token(provider, vin)` | Return a valid access token, refreshing automatically if expired. Returns `None` if credentials are missing or refresh fails. |
| `_build_token_fields(creds)` | Build multipart form fields for a token refresh request. Uses `redirect_url` (Ford's non-standard field name). |
| `refresh_access_token(creds)` | Perform a refresh-token grant against Ford's token endpoint. Persists the new tokens and handles rotation-safe refresh-token updates. |
| `validate_credentials(form_data)` | Test OAuth credentials by attempting a token refresh. Used by the setup UI to verify before saving. Returns `(token_data, None)` or `(None, error_message)`. |
| `exchange_authorization_code(form_data, auth_code)` | Exchange an OAuth authorization code for `access_token` + `refresh_token`. Returns `(token_data, None)` or `(None, error_message)`. |
| `save_credentials(provider, vin, form_data, token_data)` | Insert or upsert OAuth credentials with validated token data. The `client_secret` is encrypted before storage. |
| `_persist_tokens(cred_id, ...)` | Internal helper to update access_token, expiry, and refresh_token on an existing credential row. |

---

### `poller.py` — Background Telemetry Poller

Daemon thread that periodically calls Ford's telemetry API and stores the results. Handles retry with exponential backoff, classified error responses, and adaptive polling intervals.

Charging history is also recorded while the vehicle is plugged in or actively charging.

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
| `/charging` | GET | **Charging** — current charging state plus recent charging history samples. |
| `/oauth` | GET, POST | **OAuth Config** — manual paste-code flow. Enter OAuth settings, paste authorization code (or refresh token), exchange/validate, save credentials, then trigger initial data poll. |
| `/poller` | GET, POST | **Poller Control** — start/stop the background poller, view collector status. |
| `/settings` | GET, POST | **Settings** — toggle metric/imperial display, configure polling intervals, change runtime log level. |
| `/settings/ssl` | POST | Upload SSL cert/key files and enable/disable HTTPS. Requires restart. |
| `/settings/upload-image` | POST | Upload a custom vehicle image for the dashboard. |
| `/manage` | GET | **Manage Vehicles** — list all VINs with per-table row counts, delete VINs, re-poll. |
| `/manage/delete-vin` | POST | Delete a VIN and all cascaded data. |
| `/manage/repoll` | POST | Re-run the initial setup poll for the active VIN. |
| `/reset` | GET, POST | **Factory Reset** — delete all data for the active VIN, return to setup. |
| `/db` | GET | **Database Browser** — list all tables with row counts. |
| `/db/<table>` | GET | View table contents with secret masking and delete buttons. |
| `/db/<table>/delete` | POST | Delete a specific row from a table. |
| `/db/<table>/<id>` | GET | Row detail with expanded JSON for JSONB columns. |
| `/backup` | GET | **Backup & Restore** — list backups, create, restore, download, delete, upload. |
| `/backup/create` | POST | Create a new backup (SQL or JSON format). |
| `/backup/restore` | POST | Restore from an existing backup file. |
| `/backup/download/<file>` | GET | Download a backup file. |
| `/backup/delete` | POST | Delete a backup file. |
| `/backup/upload` | POST | Upload a backup file (.sql or .json). |
| `/setup` | GET, POST | **Database Setup** — shown when PostgreSQL is unreachable. Configure connection, save to config.json, and connect. |
| `/setup/test` | POST | Test a database connection without saving settings. |
| `/setup/create-schema` | POST | Apply `schema.sql` to create all required tables. |
| `/setup/restore` | POST | Apply schema and restore from a backup file during setup. |
| `/setup/upload` | POST | Upload a backup file during setup (works without database). |

#### Startup Behaviour

If PostgreSQL is unreachable at startup, the app enters **setup mode** instead of crashing. All routes redirect to `/setup` until a database connection is established. Once connected, the user can create the schema and/or restore from a backup, then proceed to OAuth configuration.

#### Runtime Log Level Switching

The Settings page includes a **Console / App Log Level** dropdown (DEBUG, INFO, WARNING, ERROR). Changing the level takes effect immediately — the console handler and combined app-file handler are updated at runtime. Per-module debug files (`logs/debug_*.log`) always capture DEBUG regardless of this setting. The selected level is persisted in the `app_config` database table and restored on startup.

---

### `crypto.py` — Credential Encryption

Symmetric encryption for sensitive fields using Fernet (AES-128-CBC with HMAC-SHA256) from the `cryptography` library. The encryption key is auto-generated on first use and stored in `secret.key` (file permissions: 0600, excluded from Git via `.gitignore`).

| Function | Description |
|---|---|
| `encrypt(plaintext)` | Encrypt a string and return a URL-safe base64 token. |
| `decrypt(ciphertext)` | Decrypt a Fernet token back to plaintext. Gracefully falls back to returning the original value if decryption fails (supports pre-encryption data migration). |

> **Important:** The `secret.key` file must be kept secure and backed up separately. If lost, encrypted `client_secret` values in the database cannot be recovered.

---

### `schema.sql` — PostgreSQL Schema

21 tables covering vehicle metadata, telemetry logs, per-domain state tables, charging history, drive tracking, OAuth credentials, polling configuration, and application settings. All vehicle tables reference `garage(vin)` with `ON DELETE CASCADE`.

---

### `config.json` — Application Configuration

```json
{
  "environment": "development",
  "port": 5000,
  "database": { "host": "localhost", "port": 5432, "name": "lightning",
                "user": "lightning", "password": "lightningpass" },
  "logging": { "level": "INFO" },
  "collector": { "default_poll_interval_sec": 60, "max_consecutive_failures": 5 },
  "ssl": { "enabled": false, "cert": "", "key": "" }
}
```

| Section | Description |
|---|---|
| `environment` | `development` or `production`. Controls Flask debug mode. |
| `port` | Flask listening port (default `5000`). |
| `database` | PostgreSQL connection settings. Editable via the DB setup page. |
| `logging` | Default log level and SQL logging flag. Runtime level is controlled from Settings. |
| `collector` | Poller defaults (interval, max consecutive failures). |
| `ssl` | SSL/TLS configuration. Set `enabled: true` and provide paths to `cert` and `key` PEM files to serve over HTTPS. Can be configured via the Settings page UI. |

The VIN is **not** stored in config — it is discovered automatically from Ford’s garage API during initial setup.

---

### `backup.py` — Backup & Restore

Provides two backup strategies for the complete database and all configuration.

| Function | Description |
|---|---|
| `backup_sql(label)` | Full database dump using `pg_dump`. Requires PostgreSQL client tools on the server. |
| `restore_sql(filepath)` | Restore from a SQL dump using `psql`. |
| `backup_json(label)` | Export all application tables to a portable JSON file. No external tools required. |
| `restore_json(filepath)` | Restore from a JSON backup. Uses `INSERT ... ON CONFLICT DO NOTHING` — existing rows are never overwritten. |
| `list_backups()` | List all `.sql` and `.json` files in the `backups/` directory. |
| `delete_backup(filename)` | Delete a backup file (path-traversal safe). |

Backups are stored in the `backups/` directory at the project root.

---

## Templates

| Template | Description |
|---|---|
| `base.html` | Base layout with nav bar, prototype banner, flash messages. |
| `dashboard.html` | Main dashboard with vehicle info, battery, poller status cards. |
| `vehicle_state.html` | All state tables displayed as cards with unit-converted values. |
| `telemetry.html` | Telemetry overview with poll count and recent history. |
| `oauth_config.html` | OAuth credential entry form with pre-population from DB. |
| `charging.html` | Current charging state plus charging history table. |
| `poller.html` | Poller start/stop controls and collector status. |
| `settings.html` | Unit system toggle and polling interval configuration. |
| `manage.html` | Vehicle management — list VINs, delete, re-poll, orphan detection. |
| `reset.html` | Factory reset confirmation page. |
| `db_browser.html` | Database table listing with row counts. |
| `db_table.html` | Table content viewer with delete buttons and secret masking. |
| `db_row_detail.html` | Single row detail with JSON expansion. |
| `backup.html` | Backup & restore UI — create, upload, download, restore, delete. |
| `db_setup.html` | Database setup — connection form, test, schema creation, backup restore. |

---

## Database Setup (Docker)

The application uses PostgreSQL 16. The recommended approach is Docker with a **named volume** so data persists across container restarts and upgrades.

### First-time setup

```bash
# 1. Create a persistent volume (only once — survives container removal)
docker volume create lightning_pgdata

# 2. Start PostgreSQL
docker run -d \
  --name lightning-db \
  -e POSTGRES_USER=lightning \
  -e POSTGRES_PASSWORD=lightningpass \
  -e POSTGRES_DB=lightning \
  -p 5432:5432 \
  -v lightning_pgdata:/var/lib/postgresql/data \
  postgres:16

# 3. Wait a few seconds for startup, then apply the schema
docker exec -i lightning-db psql -U lightning -d lightning < schema.sql
```

### Restarting (preserves all data)

```bash
# If the container is stopped:
docker start lightning-db

# If the container was removed but the volume still exists:
docker run -d \
  --name lightning-db \
  -e POSTGRES_USER=lightning \
  -e POSTGRES_PASSWORD=lightningpass \
  -e POSTGRES_DB=lightning \
  -p 5432:5432 \
  -v lightning_pgdata:/var/lib/postgresql/data \
  postgres:16
# Data is intact — do NOT re-run schema.sql (it would fail on existing tables)
```

### Upgrading PostgreSQL

```bash
# 1. Create a backup first (from the web UI or CLI)
docker exec lightning-db pg_dump -U lightning -d lightning > backup_before_upgrade.sql

# 2. Stop and remove the old container (volume is preserved)
docker stop lightning-db && docker rm lightning-db

# 3. Start with the new image version
docker run -d --name lightning-db \
  -e POSTGRES_USER=lightning \
  -e POSTGRES_PASSWORD=lightningpass \
  -e POSTGRES_DB=lightning \
  -p 5432:5432 \
  -v lightning_pgdata:/var/lib/postgresql/data \
  postgres:17   # <-- new version
```

> **Key point:** The named volume `lightning_pgdata` holds all data. As long as you don’t delete the volume (`docker volume rm lightning_pgdata`), your database survives container stops, removals, and image upgrades.

### Checking the volume

```bash
docker volume inspect lightning_pgdata
```

---

## Quick Start

```bash
# 1. Set up PostgreSQL (see "Database Setup" section above)

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

Open `http://localhost:5000` (or whichever port you set in `config.json`). If the database is unreachable you'll see the Database Setup page; otherwise you'll be redirected to the OAuth config page on first run.

### Running in the Background

By default `python app.py` runs in the foreground and stops when you close the terminal. Use one of these approaches to keep it running:

#### Option 1: nohup (simplest)

```bash
nohup python app.py > logs/stdout.log 2>&1 &
echo $!  # prints the PID — save this to stop later
```

To stop:

```bash
kill <PID>
```

#### Option 2: systemd service (Linux, recommended for servers)

Create `/etc/systemd/system/lightning.service`:

```ini
[Unit]
Description=Lightning EV Telemetry
After=network.target postgresql.service

[Service]
Type=simple
User=kevin
WorkingDirectory=/Users/kevin/dev/Ford
ExecStart=/usr/bin/python3 /Users/kevin/dev/Ford/app.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable lightning   # start on boot
sudo systemctl start lightning    # start now
sudo systemctl status lightning   # check status
journalctl -u lightning -f        # follow logs
```

#### Option 3: launchd plist (macOS)

Create `~/Library/LaunchAgents/com.lightning.ev.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.lightning.ev</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/kevin/dev/Ford/app.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/kevin/dev/Ford</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/kevin/dev/Ford/logs/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/kevin/dev/Ford/logs/stderr.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.lightning.ev.plist     # start
launchctl unload ~/Library/LaunchAgents/com.lightning.ev.plist   # stop
```

#### Option 4: screen or tmux

```bash
screen -dmS lightning python app.py   # detached screen session
screen -r lightning                    # reattach to view output
# Ctrl-A D to detach again
```

### SSL/TLS (HTTPS)

To enable HTTPS, use the **SSL / TLS** section on the Settings page to upload your certificate and private key files, then check "Enable SSL". Files are saved to the `certs/` directory. Alternatively, edit `config.json` directly:

```json
"ssl": {
  "enabled": true,
  "cert": "/path/to/cert.pem",
  "key": "/path/to/key.pem"
}
```

The app will start with `ssl_context=(cert, key)`. If the cert/key files are missing, it falls back to plain HTTP with a warning.

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

## Manual OAuth Authorization-Code Flow

Current setup is manual by design.

1. In the app, open `/oauth` and fill in provider, client_id, client_secret, scope, authorize URL, redirect URI, and token endpoint.
2. Build and open your authorize URL in a browser using:
   `authorize_endpoint?response_type=code&client_id=...&redirect_uri=...&scope=...`
3. Authenticate with Ford and copy the `code` value returned to your redirect URI.
4. Paste the code into the **Authorization Code** field.
5. Click **Exchange Code / Validate Refresh Token & Save**.
6. The app exchanges the code at the token endpoint, stores both `access_token` and `refresh_token`, then runs initial garage + telemetry setup.

If your provider policy does not allow localhost redirect URIs, use a registered public HTTPS redirect URI.

---

## Reset Database from schema.sql

Use the standalone script below when schema changes require a clean rebuild:

`scripts/reset_db_from_schema.sh`

This script drops the target DB, recreates it, and reapplies `schema.sql`.

```bash
# From project root
./scripts/reset_db_from_schema.sh

# Non-interactive
./scripts/reset_db_from_schema.sh --yes

# Override connection settings
DB_HOST=localhost DB_PORT=5432 DB_NAME=lightning DB_USER=lightning DB_PASSWORD=lightningpass \
  ./scripts/reset_db_from_schema.sh --yes
```

Requires PostgreSQL client tools: `dropdb`, `createdb`, and `psql`.

---

## Changelog

### v0.3.2 — 2026-05-02
- Drive detection no longer leaves stale `In Progress` drives visible while the truck is parked and charging.
- Added `charging_history` for sampled charging sessions, including charge rate, voltage/current, SOC, and temperatures.
- Added a dedicated Charging page and made dashboard charging state much more prominent.
- Drives list now shows only active drives while they are actually happening.

### v0.3.1 — 2026-05-02
- OAuth setup updated for manual authorization-code paste flow.
- Added authorization-code exchange path to obtain and persist both access and refresh tokens.
- Added `scripts/reset_db_from_schema.sh` to rebuild the database from `schema.sql`.

### v0.2.1 — 2026-04-28
- **Conservative polling mode** — when enabled, idle vehicles are still polled at normal intervals but telemetry records are only written to the DB when the vehicle state changes or once every 60 minutes. Active states (ignition on/run/start, gear in drive/reverse, speed > 0, charging) always write normally.
- Broadened active-vehicle detection: added `gearLeverPosition` (drive/reverse) and `ignitionStatus` value `start` to both `_vehicle_is_active()` and `_get_poll_interval()`.
- Fixed encrypted `client_secret` being sent to Ford during initial setup (VIN-less `db.fetch_one` path was missing decryption).

### v0.2.0 — 2026-04-26
- Database setup mode, SSL/TLS with recovery certs, client secret encryption, runtime log level switching, configurable Flask port, backup/restore with cross-host encryption portability.

---

## Phase 1 Scope

- [x] OAuth2 authentication with Ford's Azure AD B2C
- [x] Garage and telemetry API polling
- [x] Raw telemetry storage (JSONB) + parsed state tables
- [x] Web dashboard with unit conversion (metric/imperial)
- [x] Background poller with adaptive intervals
- [x] Database viewer and vehicle management
- [x] Backup and restore (SQL dump + portable JSON)
- [x] Database setup mode (no-DB startup, web-based configuration)
- [x] SSL/TLS support (cert + key PEM files via config.json)
- [x] Client secret encryption (Fernet AES-128-CBC)
- [x] Runtime log level switching (DEBUG/INFO/WARNING/ERROR)
- [x] Conservative polling mode (idle writes once per hour)
- [ ] GEO information integration (Phase 2)
- [ ] Charger location data (Phase 2)
- [ ] AI model training pipeline (Phase 3)

---

## License

Private prototype — not for distribution.
