"""Background telemetry poller.

Runs in a daemon thread. Only one instance at a time.
Uses the reusable oauth module to obtain tokens and writes telemetry + state.
Extract metrics from Ford's API response and upserts into PostgreSQL state tables.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.1
Date:        2026-04-28
"""

import hashlib
import json
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import db
import config
import crypto
import oauth

log = logging.getLogger("poller")
api_log = logging.getLogger("ford_api")

_lock = threading.Lock()
_poller_thread: threading.Thread | None = None
_stop_event = threading.Event()

# Conservative mode: write idle records only once per this interval
_CONSERVATIVE_IDLE_INTERVAL_SEC = 3600  # 60 minutes
_last_idle_write: datetime | None = None
_last_metrics_hash: str | None = None


# ── Public control API ─────────────────────────────────────────────

def is_running() -> bool:
    """Check whether the poller thread is alive."""
    return _poller_thread is not None and _poller_thread.is_alive()


def conservative_mode() -> bool:
    """Return True if conservative polling is enabled."""
    row = db.fetch_one("SELECT value FROM app_config WHERE key = 'conservative_polling'")
    return (row and row["value"].lower() in ("on", "true", "1")) if row else False


def start() -> bool:
    """Start the poller. Returns True if started, False if already running."""
    global _poller_thread
    with _lock:
        if is_running():
            return False
        _stop_event.clear()
        _poller_thread = threading.Thread(target=_poll_loop, daemon=True, name="telemetry-poller")
        _poller_thread.start()
        log.info("Poller started")
        return True


def stop() -> bool:
    """Signal the poller to stop. Returns True if it was running."""
    global _poller_thread
    with _lock:
        if not is_running():
            return False
        _stop_event.set()
        log.info("Poller stop requested")
        return True


# ── Core loop ──────────────────────────────────────────────────────

def _poll_loop() -> None:
    """Main poller loop. Runs in a daemon thread, polling at adaptive intervals."""
    vin = db.active_vin()
    if not vin:
        log.error("No VIN in garage table — cannot start polling")
        return
    provider = "ford"
    default_interval = config.collector_config().get("default_poll_interval_sec", 60)
    max_failures = config.collector_config().get("max_consecutive_failures", 5)

    log.info("Poller loop started for VIN=%s (default_interval=%ds)", vin, default_interval)

    while not _stop_event.is_set():
        interval = _get_poll_interval(vin, default_interval)
        try:
            _do_poll(provider, vin)
        except TelemetryRateLimitError as exc:
            log.warning("Rate limited – sleeping %ds before next poll", exc.retry_after)
            _record_failure(vin, str(exc))
            _stop_event.wait(timeout=exc.retry_after)
            continue
        except TelemetryAuthError as exc:
            log.error("Auth error in poll cycle (token may be permanently invalid): %s", exc)
            _record_failure(vin, str(exc))
            status = db.fetch_one("SELECT consecutive_failures FROM collector_status WHERE vin = %s", (vin,))
            if status and status["consecutive_failures"] >= max_failures:
                log.error("Max consecutive failures (%d) reached – stopping poller", max_failures)
                break
        except Exception as exc:
            log.exception("Poll cycle failed: %s", exc)
            _record_failure(vin, str(exc))
            status = db.fetch_one("SELECT consecutive_failures FROM collector_status WHERE vin = %s", (vin,))
            if status and status["consecutive_failures"] >= max_failures:
                log.error("Max consecutive failures (%d) reached – stopping poller", max_failures)
                break

        _stop_event.wait(timeout=interval)

    log.info("Poller loop exited")


def _do_poll(provider: str, vin: str) -> None:
    """Execute a single poll cycle: fetch telemetry, insert row, upsert state.

    If the token expires mid-poll (401/403), automatically refreshes and retries
    up to _AUTH_RETRY_LIMIT times before failing.
    """
    log.info("── POLL CYCLE START (VIN=%s) ──", vin)

    # Step 1: Get access token
    log.info("[STEP 1/3] Obtaining access token...")
    creds = oauth.get_credentials(provider, vin)
    if creds is None:
        raise RuntimeError("No OAuth credentials found in database")

    token = oauth.get_valid_access_token(provider, vin)
    if token is None:
        raise RuntimeError("STEP 1 FAILED: Unable to obtain a valid access token (refresh may have failed – check logs above)")
    log.info("[STEP 1/3] Access token obtained successfully")

    application_id = creds["client_id"]

    # Step 2: Fetch telemetry with auth-retry logic
    log.info("[STEP 2/3] Fetching telemetry from Ford API...")
    raw = None
    auth_retries = 0
    while True:
        try:
            raw = fetch_telemetry(token, application_id)
            break
        except TelemetryAuthError:
            auth_retries += 1
            if auth_retries > _AUTH_RETRY_LIMIT:
                raise
            log.warning("[STEP 2/3] Token rejected (auth retry %d/%d) – forcing refresh...",
                        auth_retries, _AUTH_RETRY_LIMIT)
            # Force a token refresh (the token may have expired between step 1 and the API call)
            refreshed = oauth.refresh_access_token(creds)
            if not refreshed:
                raise RuntimeError("Token refresh failed during auth retry")
            token = refreshed["access_token"]
            log.info("[STEP 2/3] Token refreshed – retrying request")

    log.info("[STEP 2/3] Telemetry fetched successfully")
    now = datetime.now(timezone.utc)

    # Ford wraps all metrics under a "metrics" key
    metrics = raw.get("metrics", raw)

    # ── Conservative mode: skip write if vehicle is idle and state unchanged ──
    if conservative_mode() and not _vehicle_is_active(vin, metrics):
        global _last_idle_write, _last_metrics_hash
        metrics_hash = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        time_since_last = (now - _last_idle_write).total_seconds() if _last_idle_write else None
        state_changed = (_last_metrics_hash is not None and metrics_hash != _last_metrics_hash)

        if _last_idle_write and not state_changed and time_since_last < _CONSERVATIVE_IDLE_INTERVAL_SEC:
            # Update collector_status so the UI knows we're still polling, but skip the DB write
            db.execute(
                """
                INSERT INTO collector_status (vin, last_poll, last_success, consecutive_failures)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (vin) DO UPDATE SET
                    last_poll = EXCLUDED.last_poll,
                    last_success = EXCLUDED.last_success,
                    last_error = NULL,
                    consecutive_failures = 0
                """,
                (vin, now, now),
            )
            _last_metrics_hash = metrics_hash
            log.info("[CONSERVATIVE] Vehicle idle, state unchanged – skipping write (last write %ds ago)",
                     int(time_since_last))
            log.info("── POLL CYCLE COMPLETE (VIN=%s) [skipped write] ──", vin)
            return

        if state_changed:
            log.info("[CONSERVATIVE] Vehicle idle but state changed – writing record")
        else:
            log.info("[CONSERVATIVE] Vehicle idle – writing hourly idle record")
        _last_idle_write = now
        _last_metrics_hash = metrics_hash
    else:
        # Active vehicle or conservative mode off – always write, reset idle tracking
        _last_idle_write = None
        _last_metrics_hash = None

    # Step 3: Store data
    log.info("[STEP 3/3] Storing telemetry and updating state tables...")
    db.execute(
        "INSERT INTO telemetry (vin, polled_at, raw_metrics) VALUES (%s, %s, %s)",
        (vin, now, json.dumps(raw)),
    )

    _upsert_vehicle_state(vin, now, metrics)
    _upsert_battery_state(vin, now, metrics)
    _upsert_charging_state(vin, now, metrics)
    _upsert_location_state(vin, now, metrics)
    _upsert_tire_state(vin, now, metrics)
    _upsert_door_state(vin, now, metrics)
    _upsert_window_state(vin, now, metrics)
    _upsert_brake_state(vin, now, metrics)
    _upsert_security_state(vin, now, metrics)
    _upsert_environment_state(vin, now, metrics)
    _upsert_vehicle_configuration(vin, now, metrics)
    _upsert_departure_schedules(vin, metrics)

    db.execute(
        """
        INSERT INTO collector_status (vin, last_poll, last_success, consecutive_failures)
        VALUES (%s, %s, %s, 0)
        ON CONFLICT (vin) DO UPDATE SET
            last_poll = EXCLUDED.last_poll,
            last_success = EXCLUDED.last_success,
            last_error = NULL,
            consecutive_failures = 0
        """,
        (vin, now, now),
    )
    log.info("[STEP 3/3] Data stored successfully")
    log.info("── POLL CYCLE COMPLETE (VIN=%s) ──", vin)


def poll_once(provider: str, vin: str) -> None:
    """Run a single telemetry poll (called during poller loop)."""
    _do_poll(provider, vin)


def initial_setup_poll(provider: str, vin: str | None = None) -> str:
    """Run the initial setup sequence: garage first, then telemetry.

    Called after OAuth credentials are validated and saved.
    The VIN parameter is optional — if not provided, the garage API response
    supplies it. Returns the discovered VIN.
    """
    log.info("═══ INITIAL SETUP POLL START ═══")

    # Step 1: Get access token (use VIN if known, otherwise find any enabled creds)
    log.info("[SETUP STEP 1/4] Obtaining access token...")
    if vin:
        creds = oauth.get_credentials(provider, vin)
    else:
        creds = db.fetch_one(
            "SELECT * FROM oauth_credentials WHERE provider = %s AND enabled = TRUE ORDER BY id DESC LIMIT 1",
            (provider,),
        )
        # Decrypt client_secret — db.fetch_one returns the raw encrypted value
        if creds and creds.get("client_secret"):
            creds["client_secret"] = crypto.decrypt(creds["client_secret"])
    if creds is None:
        raise RuntimeError("SETUP STEP 1 FAILED: No OAuth credentials found in database")

    if vin:
        token = oauth.get_valid_access_token(provider, vin)
    else:
        # Refresh using the credential row directly
        result = oauth.refresh_access_token(creds)
        token = result["access_token"] if result else None
    if token is None:
        raise RuntimeError("SETUP STEP 1 FAILED: Unable to obtain access token (refresh failed – check logs above)")
    log.info("[SETUP STEP 1/4] Access token obtained successfully")

    application_id = creds["client_id"]

    # Step 2: Fetch garage info — discover VIN from Ford's response
    log.info("[SETUP STEP 2/4] Fetching garage info from Ford API...")
    garage_data = fetch_garage(token, application_id)
    log.info("[SETUP STEP 2/4] Garage data received – storing to database")
    discovered_vin = _store_garage_data(garage_data)

    if not discovered_vin:
        raise RuntimeError("SETUP STEP 2 FAILED: No VIN found in garage response")

    # If we now have a VIN and the credential row had NULL, update it
    if not creds.get("vin"):
        db.execute(
            "UPDATE oauth_credentials SET vin = %s, updated_at = now() WHERE id = %s",
            (discovered_vin, creds["id"]),
        )
        log.info("[SETUP] Updated OAuth credentials with discovered VIN=%s", discovered_vin)

    vin = discovered_vin

    # Step 3: Fetch telemetry
    log.info("[SETUP STEP 3/4] Fetching telemetry from Ford API...")
    raw = fetch_telemetry(token, application_id)
    log.info("[SETUP STEP 3/4] Telemetry data received")

    # Step 4: Store telemetry + state
    log.info("[SETUP STEP 4/4] Storing telemetry and populating state tables...")
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO telemetry (vin, polled_at, raw_metrics) VALUES (%s, %s, %s)",
        (vin, now, json.dumps(raw)),
    )

    # Ford wraps all metrics under a "metrics" key
    metrics = raw.get("metrics", raw)

    _upsert_vehicle_state(vin, now, metrics)
    _upsert_battery_state(vin, now, metrics)
    _upsert_charging_state(vin, now, metrics)
    _upsert_location_state(vin, now, metrics)
    _upsert_tire_state(vin, now, metrics)
    _upsert_door_state(vin, now, metrics)
    _upsert_window_state(vin, now, metrics)
    _upsert_brake_state(vin, now, metrics)
    _upsert_security_state(vin, now, metrics)
    _upsert_environment_state(vin, now, metrics)
    _upsert_vehicle_configuration(vin, now, metrics)
    _upsert_departure_schedules(vin, metrics)

    db.execute(
        """
        INSERT INTO collector_status (vin, last_poll, last_success, consecutive_failures)
        VALUES (%s, %s, %s, 0)
        ON CONFLICT (vin) DO UPDATE SET
            last_poll = EXCLUDED.last_poll,
            last_success = EXCLUDED.last_success,
            last_error = NULL,
            consecutive_failures = 0
        """,
        (vin, now, now),
    )
    log.info("[SETUP STEP 4/4] All data stored successfully")
    log.info("═══ INITIAL SETUP POLL COMPLETE (VIN=%s) ═══", vin)
    return vin


def _store_garage_data(garage_data: dict) -> str | None:
    """Parse the garage API response and upsert into the garage table.

    The garage response may contain a list of vehicles or a single vehicle.
    Returns the VIN of the first vehicle found (used to discover VIN on initial setup).
    """
    # The response could be a list of vehicles or a wrapper
    vehicles = []
    if isinstance(garage_data, list):
        vehicles = garage_data
    elif isinstance(garage_data, dict):
        # Could be {"vehicles": [...]} or a single vehicle dict
        if "vehicles" in garage_data:
            vehicles = garage_data["vehicles"]
        elif "vehicle" in garage_data:
            vehicles = [garage_data["vehicle"]]
        else:
            # Might be the vehicle itself
            vehicles = [garage_data]

    log.info("[GARAGE] Found %d vehicle(s) in garage response", len(vehicles))

    for v in vehicles:
        if not isinstance(v, dict):
            continue

        v_vin = v.get("vin") or v.get("VIN")
        if not v_vin:
            log.warning("[GARAGE] Skipping vehicle with no VIN: %s", v)
            continue
        log.info("[GARAGE] Vehicle: vin=%s nickname=%s make=%s model=%s year=%s",
                 v_vin, v.get("nickName"), v.get("make"), v.get("modelName"), v.get("modelYear"))

        # Ford returns booleans as integers (1/0) – cast to Python bool for PostgreSQL
        def _bool(val):
            if val is None:
                return None
            return bool(val)

        db.execute(
            """
            INSERT INTO garage (vin, vehicle_id, nickname, make, model_name, model_code,
                model_year, vehicle_type, color, engine_type, tcu_enabled,
                ng_sdn_managed, vehicle_authorization_indicator, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), now())
            ON CONFLICT (vin) DO UPDATE SET
                vehicle_id = COALESCE(EXCLUDED.vehicle_id, garage.vehicle_id),
                nickname = COALESCE(EXCLUDED.nickname, garage.nickname),
                make = COALESCE(EXCLUDED.make, garage.make),
                model_name = COALESCE(EXCLUDED.model_name, garage.model_name),
                model_code = COALESCE(EXCLUDED.model_code, garage.model_code),
                model_year = COALESCE(EXCLUDED.model_year, garage.model_year),
                vehicle_type = COALESCE(EXCLUDED.vehicle_type, garage.vehicle_type),
                color = COALESCE(EXCLUDED.color, garage.color),
                engine_type = COALESCE(EXCLUDED.engine_type, garage.engine_type),
                tcu_enabled = COALESCE(EXCLUDED.tcu_enabled, garage.tcu_enabled),
                ng_sdn_managed = COALESCE(EXCLUDED.ng_sdn_managed, garage.ng_sdn_managed),
                vehicle_authorization_indicator = COALESCE(EXCLUDED.vehicle_authorization_indicator, garage.vehicle_authorization_indicator),
                updated_at = now()
            """,
            (
                v_vin,
                v.get("vehicleId") or v.get("vehicle_id"),
                v.get("nickName") or v.get("nickname"),
                v.get("make"),
                v.get("modelName") or v.get("model_name"),
                v.get("modelCode") or v.get("model_code"),
                v.get("modelYear") or v.get("model_year"),
                v.get("vehicleType") or v.get("vehicle_type"),
                v.get("color"),
                v.get("engineType") or v.get("engine_type"),
                _bool(v.get("tcuEnabled")),
                _bool(v.get("ngSdnManaged")),
                _bool(v.get("vehicleAuthorizationIndicator")),
            ),
        )

    # Return the first VIN we stored (used for initial setup discovery)
    first_vin = None
    if vehicles:
        for v in vehicles:
            if isinstance(v, dict) and (v.get("vin") or v.get("VIN")):
                first_vin = v.get("vin") or v.get("VIN")
                break
    return first_vin


# ── Ford API interaction ───────────────────────────────────────────

FORD_API_BASE = "https://api.vehicle.ford.com/fcon-query/v1"
FORD_GARAGE_URL = f"{FORD_API_BASE}/garage"
FORD_TELEMETRY_URL = f"{FORD_API_BASE}/telemetry"

# FordPass-style User-Agent – required by some Ford API gateway rules
_USER_AGENT = "FordPass/1.0 CFNetwork/1494.0.7 Darwin/23.4.0"

# Retry configuration for 5xx responses
_MAX_RETRIES = 3
_BACKOFF_BASE = 2        # seconds – base for exponential backoff
_BACKOFF_MAX = 120       # seconds – cap for any single backoff sleep
_BACKOFF_JITTER = 0.25   # ±25% random jitter on each backoff delay
_REQUEST_TIMEOUT = 30    # seconds – HTTP request timeout

# Azure B2C / Ford rate-limit defaults
_429_DEFAULT_RETRY_AFTER = 30   # seconds – fallback when Retry-After header is absent
_429_MAX_RETRIES = 2            # extra retries specifically for 429 (on top of normal retries)
_AUTH_RETRY_LIMIT = 1           # how many times to re-auth on 401/403 before giving up


class TelemetryAuthError(RuntimeError):
    """Token was rejected (401/403). Caller should re-authenticate."""


class TelemetryRateLimitError(RuntimeError):
    """Rate limited (429). Caller should respect Retry-After and back off."""
    def __init__(self, message: str, retry_after: int = _429_DEFAULT_RETRY_AFTER):
        super().__init__(message)
        self.retry_after = retry_after


class TelemetryEntitlementError(RuntimeError):
    """Vehicle not entitled or not provisioned for connected services."""


class TelemetryServiceError(RuntimeError):
    """Upstream service returned a 5xx error after retries exhausted."""


# ── Common HTTP helpers ────────────────────────────────────────────

def _build_headers(token: str, application_id: str) -> dict:
    """Build the HTTP headers required by Ford's telemetry API."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "Application-Id": application_id,
        "api-version": "2020-06-01",
    }


def _mask_token(token: str) -> str:
    """Truncate a token for safe log output."""
    if len(token) > 20:
        return f"{token[:12]}...{token[-6:]}"
    return "<short-token>"


def _log_request(method: str, url: str, headers: dict, token: str, label: str) -> None:
    """Log request to console (brief) and debug file (full detail)."""
    log.info("[%s] %s %s", label, method, url)
    safe = {k: v for k, v in headers.items()}
    safe["Authorization"] = f"Bearer {_mask_token(token)}"
    api_log.debug("[%s] %s %s", label, method, url)
    api_log.debug("[%s] Request headers: %s", label, json.dumps(safe, indent=2))


def _log_response(resp: requests.Response, label: str, attempt: int = 1, max_attempts: int = 1) -> None:
    """Log response to console (brief) and debug file (full detail)."""
    log.info("[%s] → HTTP %d (attempt %d/%d)", label, resp.status_code, attempt, max_attempts)
    api_log.debug("[%s] Response: HTTP %d (attempt %d/%d)", label, resp.status_code, attempt, max_attempts)
    api_log.debug("[%s] Response headers: %s", label, json.dumps(dict(resp.headers), indent=2))
    body_preview = ""
    try:
        body_preview = resp.text[:2000]
    except Exception:
        pass
    api_log.debug("[%s] Response body: %s", label, body_preview)


def _classify_and_raise(resp: requests.Response, label: str) -> None:
    """Inspect an error response and raise the appropriate typed exception."""
    status = resp.status_code
    body_text = ""
    try:
        body_text = resp.text[:500]
    except Exception:
        pass

    if status in (401, 403):
        log.error("[%s] AUTH ERROR %d", label, status)
        api_log.error("[%s] AUTH ERROR %d – %s", label, status, body_text)
        raise TelemetryAuthError(
            f"HTTP {status}: token rejected. "
            "Check token audience (aud), scopes, and Application-Id header."
        )

    if status == 429:
        retry_after = _parse_retry_after(resp)
        log.warning("[%s] RATE LIMITED 429 – Retry-After: %ds", label, retry_after)
        api_log.warning("[%s] RATE LIMITED 429 – Retry-After: %ds – %s", label, retry_after, body_text)
        raise TelemetryRateLimitError(
            f"HTTP 429: rate limited by Azure B2C / Ford API gateway. "
            f"Retry after {retry_after}s.",
            retry_after=retry_after,
        )

    if status in (402, 404, 405):
        log.error("[%s] ENTITLEMENT/ROUTING ERROR %d", label, status)
        api_log.error("[%s] ENTITLEMENT/ROUTING ERROR %d – %s", label, status, body_text)
        raise TelemetryEntitlementError(
            f"HTTP {status}: vehicle may not be enrolled in FordPass Connected Services, "
            "or the endpoint path is incorrect."
        )

    if 500 <= status < 600:
        log.error("[%s] SERVICE ERROR %d", label, status)
        api_log.error("[%s] SERVICE ERROR %d – %s", label, status, body_text)
        raise TelemetryServiceError(
            f"HTTP {status}: Ford upstream service error. "
            "This is often transient. If persistent, verify: "
            "(1) correct API host/path, (2) required headers, (3) vehicle TCU is online."
        )

    log.error("[%s] UNEXPECTED HTTP %d", label, status)
    api_log.error("[%s] UNEXPECTED HTTP %d – %s", label, status, body_text)
    raise RuntimeError(f"HTTP {status}: {body_text}")


def _parse_retry_after(resp: requests.Response) -> int:
    """Extract the Retry-After value from a 429 response.

    Azure B2C sends Retry-After as an integer (seconds).
    Falls back to a safe default if the header is missing or unparseable.
    """
    raw = resp.headers.get("Retry-After", "")
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        return _429_DEFAULT_RETRY_AFTER


def _ford_get(url: str, token: str, application_id: str, label: str) -> dict:
    """Make a GET request to a Ford API endpoint with retry + diagnostics.

    Retry strategy (Azure B2C compatible):
    - Network errors / timeouts: exponential backoff with jitter, up to _MAX_RETRIES
    - 5xx server errors: exponential backoff with jitter, up to _MAX_RETRIES
    - 429 rate-limit: respect Retry-After header, up to _429_MAX_RETRIES extra attempts
    - 401/403 auth errors: bubble up as TelemetryAuthError (caller handles re-auth)
    - Other 4xx: fail immediately
    """
    headers = _build_headers(token, application_id)

    oauth.log_token_diagnostics(token, context=label)
    _log_request("GET", url, headers, token, label)

    last_exc: Exception | None = None
    rate_limit_retries = 0

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        except requests.ConnectionError as exc:
            log.warning("[%s] Network error (attempt %d/%d)", label, attempt + 1, _MAX_RETRIES)
            api_log.warning("[%s] Network error (attempt %d/%d): %s", label, attempt + 1, _MAX_RETRIES, exc)
            last_exc = RuntimeError(f"Connection error: {exc}")
            _backoff_wait(attempt)
            continue
        except requests.Timeout as exc:
            log.warning("[%s] Timeout (attempt %d/%d)", label, attempt + 1, _MAX_RETRIES)
            api_log.warning("[%s] Timeout (attempt %d/%d): %s", label, attempt + 1, _MAX_RETRIES, exc)
            last_exc = RuntimeError(f"Request timeout: {exc}")
            _backoff_wait(attempt)
            continue

        _log_response(resp, label, attempt + 1, _MAX_RETRIES)

        if resp.ok:
            data = resp.json()
            keys_info = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            log.info("[%s] SUCCESS – keys: %s", label, keys_info)
            api_log.info("[%s] SUCCESS – full response written above", label)
            return data

        # 429 – rate limited: respect Retry-After header
        if resp.status_code == 429:
            retry_after = _parse_retry_after(resp)
            rate_limit_retries += 1
            if rate_limit_retries <= _429_MAX_RETRIES:
                log.warning("[%s] Rate limited (429) – waiting %ds (rate-retry %d/%d)",
                            label, retry_after, rate_limit_retries, _429_MAX_RETRIES)
                time.sleep(retry_after)
                continue
            # Exhausted rate-limit retries
            _classify_and_raise(resp, label)

        # 5xx → retry with backoff
        if 500 <= resp.status_code < 600:
            will = "retry" if attempt < _MAX_RETRIES - 1 else "give up"
            log.warning("[%s] Server error %d (attempt %d/%d) – %s",
                        label, resp.status_code, attempt + 1, _MAX_RETRIES, will)
            last_exc = TelemetryServiceError(f"HTTP {resp.status_code}")
            if attempt < _MAX_RETRIES - 1:
                _backoff_wait(attempt)
                continue
            _classify_and_raise(resp, label)

        # Non-retryable error – fail immediately (401, 403, 404, etc.)
        _classify_and_raise(resp, label)

    raise last_exc or RuntimeError(f"[{label}] Request failed after retries")


def _backoff_wait(attempt: int) -> None:
    """Exponential backoff with jitter, capped at _BACKOFF_MAX.

    Azure B2C recommended pattern: base * 2^attempt ± jitter.
    """
    base_delay = _BACKOFF_BASE * (2 ** attempt)
    jitter = base_delay * _BACKOFF_JITTER * (2 * random.random() - 1)  # ±25%
    delay = min(base_delay + jitter, _BACKOFF_MAX)
    delay = max(0.5, delay)  # never sleep less than 0.5s
    log.debug("Backoff: sleeping %.1fs before retry (attempt %d)", delay, attempt + 1)
    time.sleep(delay)


# ── Garage fetch ───────────────────────────────────────────────────

def fetch_garage(token: str, application_id: str) -> dict:
    """Fetch garage info from Ford API. Returns raw JSON dict."""
    return _ford_get(FORD_GARAGE_URL, token, application_id, label="GARAGE")


# ── Telemetry fetch ────────────────────────────────────────────────

def fetch_telemetry(token: str, application_id: str) -> dict:
    """Fetch telemetry from Ford API. Returns raw JSON dict."""
    return _ford_get(FORD_TELEMETRY_URL, token, application_id, label="TELEMETRY")


# ── Polling interval logic ─────────────────────────────────────────

# Ignition values that indicate the vehicle is "on"
_IGNITION_ACTIVE = {"run", "on", "start"}
# Gear positions that indicate the vehicle is driving
_GEAR_ACTIVE = {"drive", "reverse"}


def _vehicle_is_active(vin: str, metrics: dict) -> bool:
    """Return True if the vehicle appears to be on, moving, or charging.

    Checks both the fresh metrics from the API and the stored state as a fallback.
    Detects: ignition on/run/start, gear in drive/reverse, speed > 0, charging.
    """
    # Check fresh metrics first
    ign = _v(metrics, "ignitionStatus", "value")
    speed = _v(metrics, "speed", "value")
    plug = _v(metrics, "plugStatus", "value")
    gear = _v(metrics, "gearLeverPosition", "value")

    if speed is not None and float(speed) > 0:
        return True
    if ign and str(ign).lower() in _IGNITION_ACTIVE:
        return True
    if gear and str(gear).lower() in _GEAR_ACTIVE:
        return True
    if plug and str(plug).lower() not in ("unplugged", "unknown", ""):
        return True

    # Fallback to stored DB state
    vs = db.fetch_one("SELECT ignition_status, speed_mph, gear_position FROM vehicle_state WHERE vin = %s", (vin,))
    cs = db.fetch_one("SELECT plug_status FROM charging_state WHERE vin = %s", (vin,))
    if vs and vs.get("speed_mph") and vs["speed_mph"] > 0:
        return True
    if vs and vs.get("ignition_status") and vs["ignition_status"].lower() in _IGNITION_ACTIVE:
        return True
    if vs and vs.get("gear_position") and vs["gear_position"].lower() in _GEAR_ACTIVE:
        return True
    if cs and cs.get("plug_status") and cs["plug_status"].lower() not in ("unplugged", "unknown"):
        return True

    return False


def _get_poll_interval(vin: str, default: int) -> int:
    """Determine the appropriate polling interval from polling_config + vehicle state.

    All returned values are clamped to safety limits:
    - Moving: minimum 15 seconds
    - All other modes: minimum 60 seconds
    - Maximum for all modes: 3600 seconds (60 minutes)
    """
    _MIN_MOVING = 15
    _MIN_GENERAL = 60
    _MAX_INTERVAL = 3600

    pc = db.fetch_one(
        "SELECT * FROM polling_config WHERE vin = %s AND enabled = TRUE ORDER BY id DESC LIMIT 1",
        (vin,),
    )
    if pc is None:
        return max(_MIN_GENERAL, min(_MAX_INTERVAL, default))

    vs = db.fetch_one("SELECT ignition_status, speed_mph, gear_position FROM vehicle_state WHERE vin = %s", (vin,))
    cs = db.fetch_one("SELECT plug_status FROM charging_state WHERE vin = %s", (vin,))

    if vs and vs.get("speed_mph") and vs["speed_mph"] > 0:
        return max(_MIN_MOVING, min(_MAX_INTERVAL, pc["moving_interval_sec"]))
    if cs and cs.get("plug_status") and cs["plug_status"].lower() not in ("unplugged", "unknown"):
        return max(_MIN_GENERAL, min(_MAX_INTERVAL, pc["charging_interval_sec"]))
    if vs and vs.get("ignition_status") and vs["ignition_status"].lower() in _IGNITION_ACTIVE:
        return max(_MIN_GENERAL, min(_MAX_INTERVAL, pc["ignition_on_interval_sec"]))
    if vs and vs.get("gear_position") and vs["gear_position"].lower() in _GEAR_ACTIVE:
        return max(_MIN_GENERAL, min(_MAX_INTERVAL, pc["ignition_on_interval_sec"]))

    return max(_MIN_GENERAL, min(_MAX_INTERVAL, pc["ignition_off_interval_sec"]))


# ── Failure recording ──────────────────────────────────────────────

def _record_failure(vin: str, error: str) -> None:
    """Record a poll failure in collector_status, incrementing the failure counter."""
    db.execute(
        """
        INSERT INTO collector_status (vin, last_poll, last_error, consecutive_failures)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (vin) DO UPDATE SET
            last_poll = EXCLUDED.last_poll,
            last_error = EXCLUDED.last_error,
            consecutive_failures = collector_status.consecutive_failures + 1
        """,
        (vin, datetime.now(timezone.utc), error),
    )


# ── State upsert helpers ──────────────────────────────────────────
# Each helper extracts relevant fields from the raw API response and
# upserts a single row. Missing data is safely handled with .get().

def _v(raw: dict, *keys, default=None):
    """Safely traverse nested dicts."""
    node = raw
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
    return node


def _upsert_vehicle_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford metrics to vehicle_state. All values stored in raw metric units."""
    db.execute(
        """
        INSERT INTO vehicle_state (vin, last_update, ignition_status, speed_mph, gear_position, odometer_miles, lifecycle_mode)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update = EXCLUDED.last_update,
            ignition_status = EXCLUDED.ignition_status,
            speed_mph = EXCLUDED.speed_mph,
            gear_position = EXCLUDED.gear_position,
            odometer_miles = EXCLUDED.odometer_miles,
            lifecycle_mode = EXCLUDED.lifecycle_mode
        """,
        (vin, ts,
         _v(m, "ignitionStatus", "value"),
         _v(m, "speed", "value"),                        # km/h from Ford
         _v(m, "gearLeverPosition", "value"),
         _v(m, "odometer", "value"),                     # km from Ford
         _v(m, "vehicleLifeCycleMode", "value")),
    )


def _upsert_battery_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford xev* battery metrics to battery_state. Raw metric units."""
    db.execute(
        """
        INSERT INTO battery_state (vin, last_update, soc_percent, actual_soc_percent,
            energy_remaining_kwh, capacity_kwh, voltage, current, temperature_c,
            performance_status, load_status, range_miles)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update, soc_percent=EXCLUDED.soc_percent,
            actual_soc_percent=EXCLUDED.actual_soc_percent,
            energy_remaining_kwh=EXCLUDED.energy_remaining_kwh,
            capacity_kwh=EXCLUDED.capacity_kwh, voltage=EXCLUDED.voltage,
            current=EXCLUDED.current, temperature_c=EXCLUDED.temperature_c,
            performance_status=EXCLUDED.performance_status,
            load_status=EXCLUDED.load_status, range_miles=EXCLUDED.range_miles
        """,
        (vin, ts,
         _v(m, "xevBatteryStateOfCharge", "value"),
         _v(m, "xevBatteryActualStateOfCharge", "value"),
         _v(m, "xevBatteryEnergyRemaining", "value"),
         _v(m, "xevBatteryCapacity", "value"),
         _v(m, "xevBatteryVoltage", "value"),
         _v(m, "xevBatteryIoCurrent", "value"),
         _v(m, "xevBatteryTemperature", "value"),
         _v(m, "xevBatteryPerformanceStatus", "value"),
         _v(m, "batteryLoadStatus", "value"),
         _v(m, "xevBatteryRange", "value")),            # km from Ford
    )


def _upsert_charging_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford xev charging metrics to charging_state."""
    db.execute(
        """
        INSERT INTO charging_state (vin, last_update, plug_status, charger_power_type,
            communication_status, time_to_full_min, charger_current, charger_voltage, evse_dc_current)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update, plug_status=EXCLUDED.plug_status,
            charger_power_type=EXCLUDED.charger_power_type,
            communication_status=EXCLUDED.communication_status,
            time_to_full_min=EXCLUDED.time_to_full_min,
            charger_current=EXCLUDED.charger_current,
            charger_voltage=EXCLUDED.charger_voltage,
            evse_dc_current=EXCLUDED.evse_dc_current
        """,
        (vin, ts,
         _v(m, "xevPlugChargerStatus", "value"),
         _v(m, "xevChargeStationPowerType", "value"),
         _v(m, "xevChargeStationCommunicationStatus", "value"),
         _v(m, "xevBatteryTimeToFullCharge", "value"),
         _v(m, "xevBatteryChargerCurrentOutput", "value"),
         _v(m, "xevBatteryChargerVoltageOutput", "value"),
         _v(m, "xevEvseBatteryDcCurrentOutput", "value")),
    )


def _upsert_location_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford position/heading/compass metrics to location_state."""
    db.execute(
        """
        INSERT INTO location_state (vin, last_update, latitude, longitude, altitude_m, heading_deg, compass_direction)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update, latitude=EXCLUDED.latitude,
            longitude=EXCLUDED.longitude, altitude_m=EXCLUDED.altitude_m,
            heading_deg=EXCLUDED.heading_deg, compass_direction=EXCLUDED.compass_direction
        """,
        (vin, ts,
         _v(m, "position", "value", "location", "lat"),
         _v(m, "position", "value", "location", "lon"),
         _v(m, "position", "value", "location", "alt"),
         _v(m, "heading", "value", "heading"),
         _v(m, "compassDirection", "value")),
    )


def _upsert_tire_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford tirePressure array + tirePressureStatus array to tire_state."""
    tires = m.get("tirePressure", [])
    if not isinstance(tires, list):
        return
    # Build status lookup from tirePressureStatus array
    statuses = {}
    for s in (m.get("tirePressureStatus", []) or []):
        if isinstance(s, dict):
            statuses[s.get("vehicleWheel", "")] = s.get("value")

    for tire in tires:
        if not isinstance(tire, dict):
            continue
        wheel = tire.get("vehicleWheel")
        if not wheel:
            continue
        # placard can be in wheelPlacardFront or wheelPlacardRear
        placard = tire.get("wheelPlacardFront") or tire.get("wheelPlacardRear")
        db.execute(
            """
            INSERT INTO tire_state (vin, wheel_position, pressure_kpa, status, placard_kpa, last_update)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vin, wheel_position) DO UPDATE SET
                pressure_kpa=EXCLUDED.pressure_kpa, status=EXCLUDED.status,
                placard_kpa=EXCLUDED.placard_kpa, last_update=EXCLUDED.last_update
            """,
            (vin, wheel, tire.get("value"), statuses.get(wheel), placard, ts),
        )


def _upsert_door_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford doorStatus, doorLockStatus, doorPresenceStatus arrays to door_state.

    Ford sends separate arrays for status, lock, and presence. We merge them
    by (vehicleDoor + vehicleSide/vehicleOccupantRole) into a composite key.
    """
    # Build a dict keyed by a door identifier
    doors: dict[str, dict] = {}

    for entry in (m.get("doorStatus", []) or []):
        if not isinstance(entry, dict):
            continue
        key = _door_key(entry)
        doors.setdefault(key, {})["status"] = entry.get("value")

    for entry in (m.get("doorLockStatus", []) or []):
        if not isinstance(entry, dict):
            continue
        key = _door_key(entry)
        doors.setdefault(key, {})["lock_status"] = entry.get("value")

    for entry in (m.get("doorPresenceStatus", []) or []):
        if not isinstance(entry, dict):
            continue
        key = _door_key(entry)
        doors.setdefault(key, {})["presence_status"] = entry.get("value")

    for door_name, data in doors.items():
        db.execute(
            """
            INSERT INTO door_state (vin, door, status, lock_status, presence_status, last_update)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (vin, door) DO UPDATE SET
                status=EXCLUDED.status, lock_status=EXCLUDED.lock_status,
                presence_status=EXCLUDED.presence_status, last_update=EXCLUDED.last_update
            """,
            (vin, door_name,
             data.get("status"), data.get("lock_status"),
             data.get("presence_status"), ts),
        )


def _door_key(entry: dict) -> str:
    """Build a human-readable door identifier from Ford's multi-field keys."""
    door = entry.get("vehicleDoor", "UNKNOWN")
    side = entry.get("vehicleSide", "")
    role = entry.get("vehicleOccupantRole", "")
    parts = [door]
    if side and side != "UNKNOWN":
        parts.append(side)
    elif role and role not in ("PASSENGER", ""):
        parts.append(role)
    return "_".join(parts)


def _upsert_window_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford windowStatus array to window_state."""
    windows = m.get("windowStatus", [])
    if not isinstance(windows, list):
        return
    for w in windows:
        if not isinstance(w, dict):
            continue
        # Build a position key from vehicleWindow + vehicleSide
        win_type = w.get("vehicleWindow", "UNKNOWN")
        side = w.get("vehicleSide", "")
        pos = f"{win_type}_{side}" if side else win_type

        # value is {"doubleRange": {"lowerBound": ..., "upperBound": ...}}
        val = w.get("value", {})
        dr = val.get("doubleRange", {}) if isinstance(val, dict) else {}
        db.execute(
            """
            INSERT INTO window_state (vin, window_position, lower_bound, upper_bound, last_update)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (vin, window_position) DO UPDATE SET
                lower_bound=EXCLUDED.lower_bound, upper_bound=EXCLUDED.upper_bound,
                last_update=EXCLUDED.last_update
            """,
            (vin, pos, dr.get("lowerBound"), dr.get("upperBound"), ts),
        )


def _upsert_brake_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford brake/torque metrics."""
    db.execute(
        """
        INSERT INTO brake_state (vin, last_update, brake_pedal_status, brake_torque,
            parking_brake_status, wheel_torque_status, transmission_torque)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update, brake_pedal_status=EXCLUDED.brake_pedal_status,
            brake_torque=EXCLUDED.brake_torque, parking_brake_status=EXCLUDED.parking_brake_status,
            wheel_torque_status=EXCLUDED.wheel_torque_status,
            transmission_torque=EXCLUDED.transmission_torque
        """,
        (vin, ts,
         _v(m, "brakePedalStatus", "value"),
         _v(m, "brakeTorque", "value"),
         _v(m, "parkingBrakeStatus", "value"),
         _v(m, "wheelTorqueStatus", "value"),
         _v(m, "torqueAtTransmission", "value")),
    )


def _upsert_security_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford alarm/security metrics."""
    db.execute(
        """
        INSERT INTO security_state (vin, last_update, alarm_status, panic_alarm_status, remote_start_countdown)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update, alarm_status=EXCLUDED.alarm_status,
            panic_alarm_status=EXCLUDED.panic_alarm_status,
            remote_start_countdown=EXCLUDED.remote_start_countdown
        """,
        (vin, ts,
         _v(m, "alarmStatus", "value"),
         _v(m, "panicAlarmStatus", "value"),
         _v(m, "remoteStartCountdownTimer", "value")),
    )


def _upsert_environment_state(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford temperature metrics."""
    db.execute(
        """
        INSERT INTO environment_state (vin, last_update, ambient_temp_c, outside_temp_c)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update, ambient_temp_c=EXCLUDED.ambient_temp_c,
            outside_temp_c=EXCLUDED.outside_temp_c
        """,
        (vin, ts,
         _v(m, "ambientTemp", "value"),
         _v(m, "outsideTemperature", "value")),
    )


def _upsert_vehicle_configuration(vin: str, ts: datetime, m: dict) -> None:
    """Map Ford configurations block to vehicle_configuration."""
    cfg = m.get("configurations", {})
    if not isinstance(cfg, dict):
        return

    sw_schedule = _v(cfg, "automaticSoftwareUpdateScheduleSetting", "value")
    batt_target = _v(cfg, "xevBatteryTargetRangeSetting", "value")

    db.execute(
        """
        INSERT INTO vehicle_configuration (vin, last_update, remote_start_duration_sec,
            software_update_opt_in, software_update_schedule, battery_target_range_setting)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (vin) DO UPDATE SET
            last_update=EXCLUDED.last_update,
            remote_start_duration_sec=EXCLUDED.remote_start_duration_sec,
            software_update_opt_in=EXCLUDED.software_update_opt_in,
            software_update_schedule=EXCLUDED.software_update_schedule,
            battery_target_range_setting=EXCLUDED.battery_target_range_setting
        """,
        (vin, ts,
         _v(cfg, "remoteStartRunDurationSetting", "value"),
         _v(cfg, "automaticSoftwareUpdateOptInSetting", "value"),
         json.dumps(sw_schedule) if sw_schedule else None,
         json.dumps(batt_target) if batt_target else None),
    )


def _upsert_departure_schedules(vin: str, m: dict) -> None:
    """Map Ford xevDepartureSchedulesSetting to departure_schedule rows."""
    cfg = m.get("configurations", {})
    if not isinstance(cfg, dict):
        return
    dep = _v(cfg, "xevDepartureSchedulesSetting", "value")
    if not isinstance(dep, dict):
        return

    locations = dep.get("departureLocations", [])
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        for sched in loc.get("departureSchedules", []):
            if not isinstance(sched, dict):
                continue
            sid = sched.get("scheduleId")
            if sid is None:
                continue
            db.execute(
                """
                INSERT INTO departure_schedule (vin, schedule_id, status, schedule,
                    desired_temperature, oem_data)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (vin, schedule_id) DO UPDATE SET
                    status=EXCLUDED.status, schedule=EXCLUDED.schedule,
                    desired_temperature=EXCLUDED.desired_temperature,
                    oem_data=EXCLUDED.oem_data
                """,
                (vin, str(sid),
                 sched.get("scheduleStatus"),
                 json.dumps(sched.get("schedule")) if sched.get("schedule") else None,
                 json.dumps(sched.get("desiredCabinTemperatureSetting")) if sched.get("desiredCabinTemperatureSetting") else None,
                 json.dumps(sched.get("oemData")) if sched.get("oemData") else None),
            )
