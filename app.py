"""Flask application for Ford Lightning telemetry dashboard.

Serves the web UI for vehicle state monitoring, OAuth configuration,
poller control, database browsing, settings, and vehicle management.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.2.1
Date:        2026-04-28
"""

import logging
import os
import sys
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for, flash, send_file
from werkzeug.utils import secure_filename

import config
import db
import oauth
import poller
import units
import backup
import crypto

# ── Logging setup ──────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure logging with clean console output and detailed per-module debug files.

    Console: brief one-line summaries (configurable level)
    logs/lightning_app.log: combined log (configurable level)
    logs/debug_oauth.log: OAuth token exchange details (DEBUG)
    logs/debug_api.log: Ford API calls – URIs, headers, params, responses (DEBUG)
    logs/debug_poller.log: Poller lifecycle + state upserts (DEBUG)

    The console and app-file handler levels can be changed at runtime via
    ``set_log_level()`` (exposed through the Settings page).
    """
    cfg = config.logging_config()
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # ── Formatters ─────────────────────────────────────────────
    brief_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                  datefmt="%H:%M:%S")
    detail_fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s")

    # ── Console handler (brief) ────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(brief_fmt)
    console.setLevel(level)
    console.set_name("lightning_console")

    # ── Combined app log ───────────────────────────────────────
    app_file = logging.FileHandler(os.path.join(log_dir, "lightning_app.log"))
    app_file.setFormatter(detail_fmt)
    app_file.setLevel(level)
    app_file.set_name("lightning_app_file")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(app_file)

    # ── Per-module debug files ─────────────────────────────────
    debug_files = {
        "oauth": "debug_oauth.log",
        "ford_api": "debug_api.log",
        "poller": "debug_poller.log",
    }
    for logger_name, filename in debug_files.items():
        fh = logging.FileHandler(os.path.join(log_dir, filename))
        fh.setFormatter(detail_fmt)
        fh.setLevel(logging.DEBUG)
        logging.getLogger(logger_name).addHandler(fh)

    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def set_log_level(level_name: str) -> str:
    """Change the console and app-file log level at runtime.

    Per-module debug files always remain at DEBUG.
    Returns the level name actually applied.
    """
    level_name = level_name.upper()
    if level_name not in _VALID_LOG_LEVELS:
        level_name = "INFO"
    level = getattr(logging, level_name)

    root = logging.getLogger()
    for handler in root.handlers:
        if handler.get_name() in ("lightning_console", "lightning_app_file"):
            handler.setLevel(level)

    root_log = logging.getLogger(__name__)
    root_log.info("Log level changed to %s", level_name)
    return level_name


def get_log_level() -> str:
    """Return the current effective level of the console handler."""
    root = logging.getLogger()
    for handler in root.handlers:
        if handler.get_name() == "lightning_console":
            return logging.getLevelName(handler.level)
    return config.logging_config().get("level", "INFO").upper()


# ── App factory ────────────────────────────────────────────────────

def create_app() -> Flask:
    config.load()
    _setup_logging()

    log = logging.getLogger(__name__)
    log.info("Starting Lightning app (env=%s)", config.environment())

    # Attempt database connection – enter setup mode if unavailable
    try:
        db.init_pool()
        # Restore saved log level from app_config if available
        try:
            row = db.fetch_one("SELECT value FROM app_config WHERE key = 'log_level'")
            if row:
                set_log_level(row["value"])
        except Exception:
            pass  # table may not exist yet

        # Migrate: ensure all client_secret values are encrypted with this host's key.
        # After a backup/restore the ciphertext may be from a different host's key,
        # so we try to decrypt — if that fails, the value is either plaintext or
        # foreign ciphertext. Either way we need to flag it.
        try:
            from cryptography.fernet import InvalidToken
            creds = db.fetch_all("SELECT id, client_secret FROM oauth_credentials WHERE client_secret IS NOT NULL")
            for cred in creds:
                secret = cred["client_secret"]
                if not secret:
                    continue

                # Try decrypting with our key
                try:
                    crypto._get_fernet().decrypt(secret.encode("utf-8"))
                    # Success — already encrypted with our key, nothing to do
                except (InvalidToken, Exception):
                    # Not encrypted with our key. Could be:
                    #  a) plaintext secret → encrypt it
                    #  b) ciphertext from another host's key → unusable, clear it
                    if secret.startswith("gAAAAA"):
                        # Foreign Fernet token — can't recover the original secret
                        db.execute(
                            "UPDATE oauth_credentials SET client_secret = %s WHERE id = %s",
                            ("", cred["id"]),
                        )
                        log.warning(
                            "client_secret for cred id=%s was encrypted with a different key "
                            "and cannot be decrypted. It has been cleared — please re-enter "
                            "your client_secret via OAuth Config.", cred["id"],
                        )
                    else:
                        # Plaintext — encrypt it
                        encrypted = crypto.encrypt(secret)
                        db.execute(
                            "UPDATE oauth_credentials SET client_secret = %s WHERE id = %s",
                            (encrypted, cred["id"]),
                        )
                        log.info("Migrated client_secret to encrypted form (cred id=%s)", cred["id"])
        except Exception:
            pass  # table may not exist yet

    except Exception as exc:
        log.warning("Database unavailable at startup: %s", exc)
        log.info("Entering setup mode – configure the database via the web UI")

    app = Flask(__name__)
    app.secret_key = os.urandom(32)
    app.config["APP_VERSION"] = "0.2.1"

    # ── Settings helper (reads from app_config table) ──────────────

    _SETTINGS_DEFAULTS = {
        "units": "imperial",
        "poll_interval_off": "120",
        "poll_interval_on": "60",
        "poll_interval_moving": "15",
        "poll_interval_charging": "60",
        "conservative_polling": "off",
    }

    # Safety limits for polling intervals (seconds)
    _POLL_LIMITS = {
        "poll_interval_off":      {"min": 60, "max": 3600},
        "poll_interval_on":       {"min": 60, "max": 3600},
        "poll_interval_moving":   {"min": 15, "max": 3600},
        "poll_interval_charging": {"min": 60, "max": 3600},
    }

    def _clamp_interval(key: str, raw_value: str) -> int:
        """Clamp a polling interval to its configured min/max range."""
        limits = _POLL_LIMITS.get(key, {"min": 15, "max": 3600})
        try:
            val = int(raw_value)
        except (ValueError, TypeError):
            val = int(_SETTINGS_DEFAULTS.get(key, "60"))
        return max(limits["min"], min(limits["max"], val))

    def _get_setting(key: str) -> str:
        """Read a single app setting from the app_config table, with fallback defaults."""
        row = db.fetch_one("SELECT value FROM app_config WHERE key = %s", (key,))
        return row["value"] if row else _SETTINGS_DEFAULTS.get(key, "")

    def _set_setting(key: str, value: str, description: str = "") -> None:
        """Write a single app setting to the app_config table (upsert)."""
        db.execute(
            """
            INSERT INTO app_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (key, value, description),
        )

    # ── Register Jinja globals for unit conversion ─────────────────

    @app.context_processor
    def _inject_units():
        system = _get_setting("units") if db.is_available() else "imperial"

        def _ulabel_for_field(field_name):
            """Return the unit label for a DB field name, or empty string."""
            cat = units.FIELD_CATEGORIES.get(field_name)
            if cat:
                return units.unit_label(cat, system)
            return ""

        return {
            "unit_system": system,
            "convert": lambda val, field: units.convert_for_display(val, field, system),
            "ulabel": lambda cat: units.unit_label(cat, system),
            "ulabel_for_field": _ulabel_for_field,
        }

    # ── Startup checks ─────────────────────────────────────────────

    def _active_vin() -> str | None:
        """Return the current active VIN from the garage table (may be None)."""
        if not db.is_available():
            return None
        return db.active_vin()

    def _needs_setup() -> bool:
        """Check whether the system still needs OAuth / garage configuration."""
        if not db.is_available():
            return True
        vin = _active_vin()
        if not vin:
            return True
        creds = db.fetch_one(
            "SELECT id FROM oauth_credentials WHERE vin = %s AND enabled = TRUE", (vin,)
        )
        return creds is None

    # ── Request hook: force HTTPS when SSL is active ─────────────

    @app.before_request
    def _force_https():
        """Redirect HTTP requests to HTTPS when SSL is enabled."""
        if app.config.get("SSL_ACTIVE") and not request.is_secure:
            url = request.url.replace("http://", "https://", 1)
            return redirect(url, code=301)

    # ── Request hook: redirect to setup if not configured ──────────

    _SETUP_SAFE_ENDPOINTS = frozenset({
        "db_setup", "db_setup_test", "db_setup_create",
        "db_setup_restore", "db_setup_upload",
        "oauth_config", "reset", "manage", "manage_delete_vin",
        "manage_repoll", "db_browser", "db_table", "db_delete_row",
        "settings", "backup_page", "backup_create", "backup_restore",
        "backup_download", "backup_delete", "backup_upload", "static",
    })

    @app.before_request
    def _check_setup():
        # If DB is not connected, only allow the database setup pages
        if not db.is_available():
            if request.endpoint not in ("db_setup", "db_setup_test",
                                         "db_setup_create", "db_setup_restore",
                                         "db_setup_upload", "static"):
                return redirect(url_for("db_setup"))
            return

        if _needs_setup() and request.endpoint not in _SETUP_SAFE_ENDPOINTS:
            return redirect(url_for("oauth_config"))

    # ── Database setup routes (no-DB mode) ─────────────────────────

    def _setup_backup_list() -> list[dict]:
        """List backup files with formatted sizes (works without DB)."""
        backups_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        if not os.path.isdir(backups_dir):
            return []
        files = []
        for name in sorted(os.listdir(backups_dir), reverse=True):
            if name.endswith((".sql", ".json")):
                full = os.path.join(backups_dir, name)
                size = os.path.getsize(full)
                files.append({"name": name, "size": size, "size_fmt": backup._format_size(size)})
        return files

    @app.route("/setup", methods=["GET", "POST"])
    def db_setup():
        """Database setup page — shown when PostgreSQL is unreachable."""
        db_cfg = config.database()
        if request.method == "POST":
            new_cfg = {
                "host": request.form.get("host", "localhost").strip(),
                "port": int(request.form.get("port", 5432)),
                "name": request.form.get("name", "lightning").strip(),
                "user": request.form.get("user", "lightning").strip(),
                "password": request.form.get("password", "").strip(),
                "connect_timeout": int(request.form.get("connect_timeout", 10)),
            }
            config.save_database(new_cfg)
            db_cfg = new_cfg

            # Attempt to connect with the new settings
            try:
                db.init_pool()
                flash("Database connected successfully!", "success")
                return redirect(url_for("db_setup"))
            except Exception as exc:
                flash(f"Connection failed: {exc}", "error")

        return render_template("db_setup.html", db=db_cfg, connected=db.is_available(),
                               backup_files=_setup_backup_list())

    @app.route("/setup/test", methods=["POST"])
    def db_setup_test():
        """Test a database connection without saving."""
        ok, msg = db.test_connection(
            host=request.form.get("host", "localhost").strip(),
            port=int(request.form.get("port", 5432)),
            name=request.form.get("name", "lightning").strip(),
            user=request.form.get("user", "lightning").strip(),
            password=request.form.get("password", "").strip(),
            timeout=5,
        )
        flash(msg, "success" if ok else "error")
        return redirect(url_for("db_setup"))

    @app.route("/setup/create-schema", methods=["POST"])
    def db_setup_create():
        """Apply schema.sql to create all tables."""
        if not db.is_available():
            flash("Connect to the database first.", "error")
            return redirect(url_for("db_setup"))

        ok, msg = db.apply_schema()
        flash(msg, "success" if ok else "error")
        return redirect(url_for("db_setup"))

    @app.route("/setup/restore", methods=["POST"])
    def db_setup_restore():
        """Restore a backup during setup."""
        if not db.is_available():
            flash("Connect to the database first.", "error")
            return redirect(url_for("db_setup"))

        filename = request.form.get("filename", "")
        if not filename:
            flash("No backup file selected.", "error")
            return redirect(url_for("db_setup"))

        safe_name = os.path.basename(filename)
        filepath = os.path.join(backup.BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            flash("Backup file not found.", "error")
            return redirect(url_for("db_setup"))

        try:
            # Apply schema first to ensure tables exist
            db.apply_schema()
            if safe_name.endswith(".sql"):
                backup.restore_sql(filepath)
                flash(f"SQL restore complete: {safe_name}", "success")
            elif safe_name.endswith(".json"):
                summary = backup.restore_json(filepath)
                total = sum(summary.values())
                flash(f"JSON restore complete: {total} rows from {safe_name}", "success")
            else:
                flash("Unknown backup format.", "error")
        except Exception as exc:
            log.error("Setup restore failed: %s", exc)
            flash(f"Restore failed: {exc}", "error")
        return redirect(url_for("db_setup"))

    @app.route("/setup/upload", methods=["POST"])
    def db_setup_upload():
        """Upload a backup file during setup (DB may not be connected yet)."""
        file = request.files.get("backup_file")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("db_setup"))

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ("sql", "json"):
            flash("Only .sql and .json backup files are accepted.", "error")
            return redirect(url_for("db_setup"))

        safe_name = secure_filename(file.filename)
        save_path = os.path.join(backup.BACKUP_DIR, safe_name)
        os.makedirs(backup.BACKUP_DIR, exist_ok=True)
        file.save(save_path)
        flash(f"Uploaded: {safe_name}", "success")
        return redirect(url_for("db_setup"))

    # ── Routes ─────────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        """Main dashboard showing vehicle overview, battery, poller status."""
        vin = _active_vin()
        garage = db.fetch_one("SELECT * FROM garage WHERE vin = %s", (vin,)) if vin else None
        status = db.fetch_one("SELECT * FROM collector_status WHERE vin = %s", (vin,)) if vin else None
        battery = db.fetch_one("SELECT * FROM battery_state WHERE vin = %s", (vin,)) if vin else None
        vehicle = db.fetch_one("SELECT * FROM vehicle_state WHERE vin = %s", (vin,)) if vin else None
        charging = db.fetch_one("SELECT * FROM charging_state WHERE vin = %s", (vin,)) if vin else None
        tires = db.fetch_all("SELECT * FROM tire_state WHERE vin = %s ORDER BY wheel_position", (vin,)) if vin else []

        # Determine vehicle image filename
        vehicle_img = _get_setting("vehicle_image") or "vehicle.png"

        return render_template(
            "dashboard.html",
            vin=vin,
            garage=garage,
            status=status,
            battery=battery,
            vehicle=vehicle,
            charging=charging,
            tires=tires,
            vehicle_img=vehicle_img,
            poller_running=poller.is_running(),
        )

    @app.route("/vehicle")
    def vehicle_state():
        """Detailed view of all state tables for the active VIN."""
        vin = _active_vin()
        states = {}
        tables = [
            "vehicle_state", "battery_state", "charging_state", "location_state",
            "brake_state", "security_state", "environment_state",
        ]
        for t in tables:
            states[t] = db.fetch_one(f"SELECT * FROM {t} WHERE vin = %s", (vin,)) if vin else None

        # Composite-key tables
        states["tire_state"] = db.fetch_all("SELECT * FROM tire_state WHERE vin = %s", (vin,)) if vin else []
        states["door_state"] = db.fetch_all("SELECT * FROM door_state WHERE vin = %s", (vin,)) if vin else []
        states["window_state"] = db.fetch_all("SELECT * FROM window_state WHERE vin = %s", (vin,)) if vin else []

        return render_template("vehicle_state.html", vin=vin, states=states)

    @app.route("/telemetry")
    def telemetry_overview():
        """Show telemetry count, latest poll time, and recent poll history."""
        vin = _active_vin()
        count_row = db.fetch_one("SELECT count(*) AS cnt FROM telemetry WHERE vin = %s", (vin,)) if vin else None
        count = count_row["cnt"] if count_row else 0
        latest = db.fetch_one(
            "SELECT polled_at FROM telemetry WHERE vin = %s ORDER BY polled_at DESC LIMIT 1", (vin,)
        ) if vin else None
        recent = db.fetch_all(
            "SELECT id, polled_at, created_at FROM telemetry WHERE vin = %s ORDER BY polled_at DESC LIMIT 20",
            (vin,),
        ) if vin else []
        return render_template(
            "telemetry.html", vin=vin, count=count,
            latest=latest, recent=recent,
        )

    @app.route("/drives")
    def drives_list():
        """List all drive sessions for the active VIN."""
        vin = _active_vin()
        drives = db.fetch_all(
            """SELECT d.*,
                      (SELECT count(*) FROM drive_points WHERE drive_id = d.id) AS point_count
               FROM drives d
               WHERE d.vin = %s
               ORDER BY d.started_at DESC LIMIT 50""",
            (vin,),
        ) if vin else []
        return render_template("drives.html", vin=vin, drives=drives)

    @app.route("/drives/<int:drive_id>")
    def drive_detail(drive_id):
        """Show a single drive with all its data points."""
        drive = db.fetch_one("SELECT * FROM drives WHERE id = %s", (drive_id,))
        if not drive:
            flash("Drive not found.", "error")
            return redirect(url_for("drives_list"))
        points = db.fetch_all(
            "SELECT * FROM drive_points WHERE drive_id = %s ORDER BY recorded_at ASC",
            (drive_id,),
        )
        return render_template("drive_detail.html", drive=drive, points=points)

    @app.route("/oauth", methods=["GET", "POST"])
    def oauth_config():
        """OAuth configuration form. Validates credentials and kicks off initial data poll."""

        if request.method == "POST":
            form = {
                "provider": request.form.get("provider", "ford").strip(),
                "client_id": request.form.get("client_id", "").strip(),
                "client_secret": request.form.get("client_secret", "").strip(),
                "scope": request.form.get("scope", "").strip(),
                "redirect_uri": request.form.get("redirect_uri", "").strip(),
                "refresh_token": request.form.get("refresh_token", "").strip(),
                "token_endpoint": request.form.get("token_endpoint", "").strip(),
            }

            vin = _active_vin()

            # Basic presence validation
            missing = [k for k in ("client_id", "client_secret", "refresh_token", "token_endpoint") if not form[k]]
            if missing:
                flash(f"Missing required fields: {', '.join(missing)}", "error")
                return render_template("oauth_config.html", vin=vin, form=form)

            # Validate by attempting a token refresh
            log.info("OAuth form submitted – validating credentials...")
            token_data, err = oauth.validate_credentials(form)
            if err:
                log.warning("OAuth validation FAILED: %s", err)
                flash(err, "error")
                return render_template("oauth_config.html", vin=vin, form=form)
            log.info("OAuth validation SUCCEEDED – token received")

            # Save credentials (VIN may be None on first setup — will be
            # updated after garage discovery in initial_setup_poll)
            provider = form["provider"]
            oauth.save_credentials(provider, vin, form, token_data)
            log.info("OAuth credentials saved to database (vin=%s)", vin)

            # Initial setup: discover VIN from garage, then fetch telemetry
            try:
                discovered_vin = poller.initial_setup_poll(provider, vin)
                log.info("Initial setup discovered VIN=%s", discovered_vin)

                # Initialize collector_status for the discovered VIN
                db.execute(
                    """
                    INSERT INTO collector_status (vin, consecutive_failures)
                    VALUES (%s, 0)
                    ON CONFLICT (vin) DO NOTHING
                    """,
                    (discovered_vin,),
                )

                flash("Configuration saved. Garage and telemetry data loaded.", "success")
            except Exception as exc:
                log.error("Initial setup poll failed: %s", exc)
                flash(f"Credentials saved but initial poll failed: {exc}", "warning")

            return redirect(url_for("dashboard"))

        # GET – pre-populate from DB if available
        vin = _active_vin()
        existing = db.fetch_one(
            "SELECT * FROM oauth_credentials WHERE provider = 'ford' ORDER BY id DESC LIMIT 1"
        )
        # Decrypt client_secret for display in the form
        secret_raw = (existing or {}).get("client_secret", "")
        if secret_raw:
            secret_raw = crypto.decrypt(secret_raw)
        form = {
            "provider": (existing or {}).get("provider", "ford"),
            "client_id": (existing or {}).get("client_id", ""),
            "client_secret": secret_raw,
            "scope": (existing or {}).get("scope", ""),
            "redirect_uri": (existing or {}).get("redirect_uri", ""),
            "refresh_token": (existing or {}).get("refresh_token", ""),
            "token_endpoint": (existing or {}).get("token_endpoint", ""),
        }
        return render_template("oauth_config.html", vin=vin, form=form)

    @app.route("/poller", methods=["GET", "POST"])
    def poller_control():
        """Start/stop the background telemetry poller and view its status."""
        if request.method == "POST":
            action = request.form.get("action")
            if action == "start":
                if poller.start():
                    flash("Poller started.", "success")
                else:
                    flash("Poller is already running.", "warning")
            elif action == "stop":
                if poller.stop():
                    flash("Poller stop requested.", "success")
                else:
                    flash("Poller is not running.", "warning")
            return redirect(url_for("poller_control"))

        status = db.fetch_one("SELECT * FROM collector_status WHERE vin = %s", (_active_vin(),))
        polling_cfg = db.fetch_one(
            "SELECT * FROM polling_config WHERE vin = %s AND enabled = TRUE ORDER BY id DESC LIMIT 1",
            (_active_vin(),),
        )
        return render_template(
            "poller.html", vin=_active_vin(),
            status=status, polling_cfg=polling_cfg,
            running=poller.is_running(),
            conservative=poller.conservative_mode(),
        )

    @app.route("/reset", methods=["GET", "POST"])
    def reset():
        """Factory reset: delete all data for the active VIN and return to setup mode."""
        vin = _active_vin()
        if request.method == "POST":
            confirm = request.form.get("confirm")
            if confirm != "RESET":
                flash("Type RESET to confirm.", "error")
                return render_template("reset.html", vin=vin)

            # Stop poller if running
            if poller.is_running():
                poller.stop()
                log.info("Poller stopped for reset")

            if vin:
                # Delete all data for this VIN (cascade handles FK references)
                tables_to_clear = [
                    "telemetry", "vehicle_state", "battery_state", "charging_state",
                    "location_state", "tire_state", "door_state", "window_state",
                    "brake_state", "security_state", "environment_state",
                    "collector_status", "polling_config", "oauth_credentials",
                    "vehicle_configuration", "departure_schedule",
                ]
                # drive_points cascade-deletes when drives rows are removed
                db.execute("DELETE FROM drives WHERE vin = %s", (vin,))
                for t in tables_to_clear:
                    db.execute(f"DELETE FROM {t} WHERE vin = %s", (vin,))

                db.execute("DELETE FROM garage WHERE vin = %s", (vin,))
                log.info("All data cleared for VIN=%s", vin)

            # Also clear any orphan credentials (NULL VIN)
            db.execute("DELETE FROM oauth_credentials WHERE vin IS NULL")

            flash("All data and OAuth configuration cleared. Please re-configure.", "success")
            return redirect(url_for("oauth_config"))

        return render_template("reset.html", vin=vin)

    # ── Settings ───────────────────────────────────────────────────────

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        """Application settings: display units, polling intervals, log level."""
        if request.method == "POST":
            _set_setting("units", request.form.get("units", "imperial"), "Display unit system")

            # Runtime log level switching
            new_level = request.form.get("log_level", "INFO").upper()
            applied = set_log_level(new_level)
            _set_setting("log_level", applied, "Console / app-file log level")
            log.info("Settings: log level set to %s", applied)

            # Conservative polling toggle
            cons = "on" if request.form.get("conservative_polling") == "on" else "off"
            _set_setting("conservative_polling", cons, "Conservative idle polling (write once per hour when idle)")

            # Clamp all polling intervals to safe limits
            iv_off      = _clamp_interval("poll_interval_off",      request.form.get("poll_interval_off", "120"))
            iv_on       = _clamp_interval("poll_interval_on",       request.form.get("poll_interval_on", "60"))
            iv_moving   = _clamp_interval("poll_interval_moving",   request.form.get("poll_interval_moving", "15"))
            iv_charging = _clamp_interval("poll_interval_charging", request.form.get("poll_interval_charging", "60"))

            _set_setting("poll_interval_off",      str(iv_off),      "Ignition-off poll interval (sec)")
            _set_setting("poll_interval_on",       str(iv_on),       "Ignition-on poll interval (sec)")
            _set_setting("poll_interval_moving",   str(iv_moving),   "Moving poll interval (sec)")
            _set_setting("poll_interval_charging", str(iv_charging), "Charging poll interval (sec)")

            # Sync poller intervals to polling_config table for the active VIN
            active_vin = _active_vin()
            db.execute(
                """
                INSERT INTO polling_config (vin, ignition_off_interval_sec, ignition_on_interval_sec,
                    moving_interval_sec, charging_interval_sec, enabled)
                VALUES (%s, %s, %s, %s, %s, TRUE)
                ON CONFLICT ON CONSTRAINT polling_config_pkey DO NOTHING
                """,
                (active_vin, iv_off, iv_on, iv_moving, iv_charging),
            )
            # Also update the latest row for this VIN if one exists
            existing = db.fetch_one(
                "SELECT id FROM polling_config WHERE vin = %s ORDER BY id DESC LIMIT 1",
                (active_vin,),
            )
            if existing:
                db.execute(
                    """
                    UPDATE polling_config SET
                        ignition_off_interval_sec = %s,
                        ignition_on_interval_sec = %s,
                        moving_interval_sec = %s,
                        charging_interval_sec = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (iv_off, iv_on, iv_moving, iv_charging, existing["id"]),
                )

            flash("Settings saved.", "success")
            return redirect(url_for("settings"))

        current = {
            "units": _get_setting("units"),
            "log_level": get_log_level(),
            "poll_interval_off": _get_setting("poll_interval_off"),
            "poll_interval_on": _get_setting("poll_interval_on"),
            "poll_interval_moving": _get_setting("poll_interval_moving"),
            "poll_interval_charging": _get_setting("poll_interval_charging"),
            "conservative_polling": _get_setting("conservative_polling"),
        }
        ssl_cfg = config.ssl_config()
        ssl_status = {
            "active": app.config.get("SSL_ACTIVE", False),
            "recovery": app.config.get("SSL_RECOVERY", False),
        }
        # Validate current user certs if they exist
        if ssl_cfg.get("cert") and ssl_cfg.get("key"):
            if os.path.isfile(ssl_cfg["cert"]) and os.path.isfile(ssl_cfg["key"]):
                valid, msg = crypto.validate_ssl_files(ssl_cfg["cert"], ssl_cfg["key"])
                ssl_status["cert_valid"] = valid
                ssl_status["cert_message"] = msg
            else:
                ssl_status["cert_valid"] = False
                ssl_status["cert_message"] = "Certificate or key file not found on disk"
        return render_template("settings.html", settings=current, ssl=ssl_cfg,
                               ssl_status=ssl_status)

    @app.route("/settings/upload-image", methods=["POST"])
    def upload_vehicle_image():
        """Handle vehicle image upload from the settings page."""
        file = request.files.get("vehicle_image")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("settings"))

        ALLOWED = {"png", "jpg", "jpeg", "gif", "webp"}
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED:
            flash(f"Invalid file type. Allowed: {', '.join(ALLOWED)}", "error")
            return redirect(url_for("settings"))

        filename = secure_filename(f"vehicle.{ext}")
        save_path = os.path.join(app.static_folder, filename)
        file.save(save_path)
        _set_setting("vehicle_image", filename, "Custom vehicle image filename")
        flash("Vehicle image updated.", "success")
        return redirect(url_for("settings"))

    @app.route("/settings/ssl", methods=["POST"])
    def ssl_settings():
        """Upload SSL cert/key files and toggle SSL on/off.

        Files are saved to a `certs/` directory next to the project.
        Paths and enabled flag are persisted to config.json.
        A restart is required for SSL changes to take effect.
        """
        certs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
        os.makedirs(certs_dir, exist_ok=True)

        ssl_cfg = config.ssl_config()

        # Handle cert file upload
        cert_file = request.files.get("ssl_cert")
        if cert_file and cert_file.filename:
            cert_ext = cert_file.filename.rsplit(".", 1)[-1].lower() if "." in cert_file.filename else ""
            if cert_ext not in ("pem", "crt", "cer"):
                flash("Certificate must be .pem, .crt, or .cer", "error")
                return redirect(url_for("settings"))
            cert_path = os.path.join(certs_dir, "server.crt")
            cert_file.save(cert_path)
            ssl_cfg["cert"] = cert_path
            log.info("SSL certificate uploaded: %s", cert_path)

        # Handle key file upload
        key_file = request.files.get("ssl_key")
        if key_file and key_file.filename:
            key_ext = key_file.filename.rsplit(".", 1)[-1].lower() if "." in key_file.filename else ""
            if key_ext not in ("pem", "key"):
                flash("Key must be .pem or .key", "error")
                return redirect(url_for("settings"))
            key_path = os.path.join(certs_dir, "server.key")
            key_file.save(key_path)
            # Restrict permissions on the private key
            os.chmod(key_path, 0o600)
            ssl_cfg["key"] = key_path
            log.info("SSL private key uploaded: %s", key_path)

        # Toggle enabled
        ssl_cfg["enabled"] = request.form.get("ssl_enabled") == "on"

        config.save_ssl(ssl_cfg)
        flash("SSL settings saved. Restart the application for changes to take effect.", "success")
        return redirect(url_for("settings"))

    # ── Manage Vehicles ──────────────────────────────────────────────

    _VIN_TABLES = [
        "garage", "telemetry", "vehicle_state", "battery_state",
        "charging_state", "location_state", "tire_state", "door_state",
        "window_state", "brake_state", "security_state", "environment_state",
        "vehicle_configuration", "departure_schedule",
        "polling_config", "collector_status", "oauth_credentials",
    ]

    @app.route("/manage")
    def manage():
        """Show all VINs in the system with per-table row counts."""
        active_vin = _active_vin()
        garage_rows = db.fetch_all("SELECT * FROM garage ORDER BY updated_at DESC")

        # Build per-VIN stats
        vin_stats = []
        for g in garage_rows:
            v = g["vin"]
            counts = {}
            total = 0
            for t in _VIN_TABLES:
                if t == "garage":
                    continue
                # composite-PK tables use vin column too
                row = db.fetch_one(f"SELECT count(*) AS cnt FROM {t} WHERE vin = %s", (v,))
                c = row["cnt"] if row else 0
                if c > 0:
                    counts[t] = c
                    total += c
            vin_stats.append({
                "vin": v,
                "nickname": g.get("nickname"),
                "make": g.get("make"),
                "model_name": g.get("model_name"),
                "model_year": g.get("model_year"),
                "is_active": (v == active_vin),
                "counts": counts,
                "total_rows": total,
            })

        # Also check for oauth_credentials without a matching garage row
        orphan_creds = db.fetch_all(
            "SELECT id, provider, vin FROM oauth_credentials "
            "WHERE vin NOT IN (SELECT vin FROM garage) OR vin IS NULL"
        )

        return render_template(
            "manage.html",
            active_vin=active_vin,
            vin_stats=vin_stats,
            orphan_creds=orphan_creds,
        )

    @app.route("/manage/delete-vin", methods=["POST"])
    def manage_delete_vin():
        """Delete a VIN and all its data (ON DELETE CASCADE handles child tables)."""
        target_vin = request.form.get("vin", "").strip()
        if not target_vin:
            flash("No VIN specified.", "error")
            return redirect(url_for("manage"))

        # Safety: stop poller if deleting the active VIN
        active_vin = _active_vin()
        if target_vin == active_vin and poller.is_running():
            poller.stop()
            log.info("Poller stopped – deleting active VIN %s", target_vin)

        # CASCADE deletes all child rows
        db.execute("DELETE FROM garage WHERE vin = %s", (target_vin,))
        log.info("Deleted VIN=%s and all associated data (cascade)", target_vin)
        flash(f"Deleted VIN {target_vin} and all associated records.", "success")

        return redirect(url_for("manage"))

    @app.route("/manage/repoll", methods=["POST"])
    def manage_repoll():
        """Re-run initial setup poll for the active VIN."""
        active_vin = _active_vin()
        if not active_vin:
            flash("No active VIN in garage. Configure OAuth first.", "error")
            return redirect(url_for("manage"))

        creds = db.fetch_one(
            "SELECT provider FROM oauth_credentials WHERE vin = %s AND enabled = TRUE",
            (active_vin,),
        )
        if creds is None:
            flash("No enabled OAuth credentials for the active VIN.", "error")
            return redirect(url_for("manage"))

        try:
            poller.initial_setup_poll(creds["provider"], active_vin)
            flash(f"Re-polled data for VIN {active_vin} successfully.", "success")
        except Exception as exc:
            log.error("Re-poll failed: %s", exc)
            flash(f"Re-poll failed: {exc}", "error")

        return redirect(url_for("manage"))

    # ── Database viewer ────────────────────────────────────────────

    # Whitelist of tables the viewer can display
    _VIEWABLE_TABLES = [
        "garage", "telemetry",
        "vehicle_state", "battery_state", "charging_state", "location_state",
        "tire_state", "door_state", "window_state",
        "brake_state", "security_state", "environment_state",
        "vehicle_configuration", "departure_schedule",
        "polling_config", "collector_status", "app_config", "oauth_credentials",
    ]

    @app.route("/db")
    def db_browser():
        """Show all tables with row counts."""
        table_info = []
        for t in _VIEWABLE_TABLES:
            row = db.fetch_one(f"SELECT count(*) AS cnt FROM {t}")
            table_info.append({"name": t, "count": row["cnt"] if row else 0})
        return render_template("db_browser.html", tables=table_info)

    @app.route("/db/<table_name>")
    def db_table(table_name):
        """Show contents of a single table."""
        if table_name not in _VIEWABLE_TABLES:
            flash(f"Table '{table_name}' is not viewable.", "error")
            return redirect(url_for("db_browser"))

        limit = request.args.get("limit", 100, type=int)
        limit = min(max(limit, 1), 1000)

        # Get column info
        cols_rows = db.fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table_name,),
        )
        columns = [r["column_name"] for r in cols_rows]
        col_types = {r["column_name"]: r["data_type"] for r in cols_rows}

        # Fetch rows – order by first column (usually PK)
        rows = db.fetch_all(
            f"SELECT * FROM {table_name} ORDER BY 1 DESC LIMIT %s",
            (limit,),
        )

        # Mask secrets in oauth_credentials display
        if table_name == "oauth_credentials":
            for row in rows:
                for secret_col in ("client_secret", "access_token", "refresh_token"):
                    if secret_col in row and row[secret_col]:
                        val = str(row[secret_col])
                        row[secret_col] = val[:6] + "…" if len(val) > 10 else "***"
                # Truncate other long fields for readability
                for long_col in ("token_endpoint", "redirect_uri", "scope", "client_id"):
                    if long_col in row and row[long_col] and len(str(row[long_col])) > 50:
                        row[long_col] = str(row[long_col])[:50] + "…"

        # Determine primary key column for delete buttons
        _PK_MAP = {
            "garage": "vin",
            "telemetry": "id",
            "polling_config": "id",
            "oauth_credentials": "id",
            "app_config": "key",
        }
        # Most state tables use 'vin' as PK
        pk_col = _PK_MAP.get(table_name, "vin")

        # Tables that render better as vertical cards instead of wide horizontal tables
        _CARD_LAYOUT_TABLES = {"oauth_credentials"}

        return render_template(
            "db_table.html",
            table_name=table_name,
            columns=columns,
            col_types=col_types,
            rows=rows,
            limit=limit,
            row_count=len(rows),
            pk_col=pk_col,
            card_layout=(table_name in _CARD_LAYOUT_TABLES),
        )

    @app.route("/db/<table_name>/delete", methods=["POST"])
    def db_delete_row(table_name):
        """Delete a specific row from a table."""
        if table_name not in _VIEWABLE_TABLES:
            flash(f"Table '{table_name}' is not viewable.", "error")
            return redirect(url_for("db_browser"))

        pk_col = request.form.get("pk_col", "").strip()
        pk_val = request.form.get("pk_val", "").strip()
        if not pk_col or not pk_val:
            flash("Missing primary key info for deletion.", "error")
            return redirect(url_for("db_table", table_name=table_name))

        # Validate pk_col is an actual column in the table to prevent injection
        valid_cols = db.fetch_all(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table_name,),
        )
        valid_col_names = {r["column_name"] for r in valid_cols}
        if pk_col not in valid_col_names:
            flash(f"Invalid column '{pk_col}'.", "error")
            return redirect(url_for("db_table", table_name=table_name))

        db.execute(f"DELETE FROM {table_name} WHERE {pk_col} = %s", (pk_val,))
        log.info("Deleted row from %s WHERE %s = %s", table_name, pk_col, pk_val)
        flash(f"Deleted row from {table_name} where {pk_col} = {pk_val}.", "success")
        return redirect(url_for("db_table", table_name=table_name))

    @app.route("/db/<table_name>/<int:row_id>")
    def db_row_detail(table_name, row_id):
        """Show a single row with full JSON expansion (useful for telemetry raw_metrics)."""
        if table_name not in _VIEWABLE_TABLES:
            flash(f"Table '{table_name}' is not viewable.", "error")
            return redirect(url_for("db_browser"))

        # Try common PK column names
        pk_col = "id"
        if table_name == "garage":
            pk_col = "vin"
        elif table_name == "app_config":
            pk_col = "key"

        row = db.fetch_one(f"SELECT * FROM {table_name} WHERE {pk_col} = %s", (row_id,))
        if row is None:
            flash(f"Row {row_id} not found in {table_name}.", "error")
            return redirect(url_for("db_table", table_name=table_name))

        import json as json_mod
        formatted = {}
        for k, v in row.items():
            if isinstance(v, (dict, list)):
                formatted[k] = json_mod.dumps(v, indent=2, default=str)
            else:
                formatted[k] = v

        return render_template(
            "db_row_detail.html",
            table_name=table_name,
            row_id=row_id,
            row=formatted,
        )

    # ── Backup & Restore ───────────────────────────────────────────

    @app.route("/backup")
    def backup_page():
        """List available backups and provide create/restore/download/delete controls."""
        backups = backup.list_backups()
        for b in backups:
            b["size_fmt"] = backup._format_size(b["size"])
        return render_template("backup.html", backups=backups)

    @app.route("/backup/create", methods=["POST"])
    def backup_create():
        """Create a new backup (SQL or JSON)."""
        fmt = request.form.get("format", "json")
        label = secure_filename(request.form.get("label", "").strip()[:40])
        try:
            if fmt == "sql":
                path = backup.backup_sql(label=label)
            else:
                path = backup.backup_json(label=label)
            flash(f"Backup created: {os.path.basename(path)}", "success")
        except Exception as exc:
            log.error("Backup failed: %s", exc)
            flash(f"Backup failed: {exc}", "error")
        return redirect(url_for("backup_page"))

    @app.route("/backup/restore", methods=["POST"])
    def backup_restore():
        """Restore from an existing backup file."""
        filename = request.form.get("filename", "")
        if not filename:
            flash("No backup file specified.", "error")
            return redirect(url_for("backup_page"))

        safe_name = os.path.basename(filename)
        filepath = os.path.join(backup.BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            flash("Backup file not found.", "error")
            return redirect(url_for("backup_page"))

        try:
            if safe_name.endswith(".sql"):
                backup.restore_sql(filepath)
                flash(f"SQL restore complete: {safe_name}", "success")
            elif safe_name.endswith(".json"):
                summary = backup.restore_json(filepath)
                total = sum(summary.values())
                flash(f"JSON restore complete: {total} rows from {safe_name}", "success")
            else:
                flash("Unknown backup format.", "error")
        except Exception as exc:
            log.error("Restore failed: %s", exc)
            flash(f"Restore failed: {exc}", "error")
        return redirect(url_for("backup_page"))

    @app.route("/backup/download/<filename>")
    def backup_download(filename):
        """Download a backup file."""
        safe_name = os.path.basename(filename)
        filepath = os.path.join(backup.BACKUP_DIR, safe_name)
        if not os.path.isfile(filepath):
            flash("Backup file not found.", "error")
            return redirect(url_for("backup_page"))
        return send_file(filepath, as_attachment=True, download_name=safe_name)

    @app.route("/backup/delete", methods=["POST"])
    def backup_delete():
        """Delete a backup file."""
        filename = request.form.get("filename", "")
        if backup.delete_backup(filename):
            flash(f"Deleted: {os.path.basename(filename)}", "success")
        else:
            flash("File not found.", "error")
        return redirect(url_for("backup_page"))

    @app.route("/backup/upload", methods=["POST"])
    def backup_upload():
        """Upload a backup file to the backups directory."""
        file = request.files.get("backup_file")
        if not file or file.filename == "":
            flash("No file selected.", "error")
            return redirect(url_for("backup_page"))

        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ("sql", "json"):
            flash("Only .sql and .json backup files are accepted.", "error")
            return redirect(url_for("backup_page"))

        safe_name = secure_filename(file.filename)
        save_path = os.path.join(backup.BACKUP_DIR, safe_name)
        os.makedirs(backup.BACKUP_DIR, exist_ok=True)
        file.save(save_path)
        flash(f"Uploaded: {safe_name}", "success")
        return redirect(url_for("backup_page"))

    @app.teardown_appcontext
    def _shutdown(exc):
        """Application teardown hook. Pool cleanup handled by atexit."""
        pass  # pool cleanup handled by atexit / signal

    return app


# ── Entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    app = create_app()
    _log = logging.getLogger(__name__)

    # SSL/TLS support – try user certs, fall back to self-signed recovery
    ssl_cfg = config.ssl_config()
    ssl_context = None
    ssl_recovery = False

    if ssl_cfg.get("enabled"):
        cert_path = ssl_cfg.get("cert", "")
        key_path = ssl_cfg.get("key", "")

        # Try the user-provided certificates first
        if cert_path and key_path and os.path.isfile(cert_path) and os.path.isfile(key_path):
            valid, msg = crypto.validate_ssl_files(cert_path, key_path)
            if valid:
                ssl_context = (cert_path, key_path)
                _log.info("SSL enabled with user certificates: cert=%s key=%s", cert_path, key_path)
            else:
                _log.warning("User SSL certificates invalid: %s – falling back to recovery certs", msg)

        # Fall back to self-signed recovery certificates
        if ssl_context is None:
            _log.warning("Generating self-signed recovery certificate for HTTPS access")
            try:
                recovery_cert, recovery_key = crypto.generate_self_signed_cert()
                ssl_context = (recovery_cert, recovery_key)
                ssl_recovery = True
                _log.warning(
                    "*** RECOVERY MODE: Using self-signed certificate. "
                    "Upload valid certs in Settings → SSL to clear this warning. ***"
                )
            except Exception as exc:
                _log.error("Failed to generate recovery certificate: %s – starting without SSL", exc)

    # Store SSL state on the app so templates/hooks can see it
    app.config["SSL_ACTIVE"] = ssl_context is not None
    app.config["SSL_RECOVERY"] = ssl_recovery

    app.run(
        host="0.0.0.0",
        port=config.flask_port(),
        debug=(config.environment() == "development"),
        ssl_context=ssl_context,
    )
