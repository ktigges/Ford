"""Flask application for Ford Lightning telemetry dashboard.

Serves the web UI for vehicle state monitoring, OAuth configuration,
poller control, database browsing, settings, and vehicle management.

Author:      Kevin Tigges
Description: Ford Lightning EV Tool Prototype
Version:     0.1.0
Date:        2026-04-26
"""

import logging
import os
import sys
from datetime import datetime, timezone

from flask import Flask, redirect, render_template, request, url_for, flash
from werkzeug.utils import secure_filename

import config
import db
import oauth
import poller
import units

# ── Logging setup ──────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure logging with clean console output and detailed per-module debug files.

    Console: brief one-line summaries (INFO level)
    logs/lightning_app.log: combined log (INFO)
    logs/debug_oauth.log: OAuth token exchange details (DEBUG)
    logs/debug_api.log: Ford API calls – URIs, headers, params, responses (DEBUG)
    logs/debug_poller.log: Poller lifecycle + state upserts (DEBUG)
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

    # ── Combined app log ───────────────────────────────────────
    app_file = logging.FileHandler(os.path.join(log_dir, "lightning_app.log"))
    app_file.setFormatter(detail_fmt)
    app_file.setLevel(level)

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


# ── App factory ────────────────────────────────────────────────────

def create_app() -> Flask:
    config.load()
    _setup_logging()

    log = logging.getLogger(__name__)
    log.info("Starting Lightning app (env=%s)", config.environment())

    db.init_pool()

    app = Flask(__name__)
    app.secret_key = os.urandom(32)

    # ── Settings helper (reads from app_config table) ──────────────

    _SETTINGS_DEFAULTS = {
        "units": "imperial",
        "poll_interval_off": "120",
        "poll_interval_on": "60",
        "poll_interval_moving": "15",
        "poll_interval_charging": "60",
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
        system = _get_setting("units")

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
        return db.active_vin()

    def _needs_setup() -> bool:
        """Check whether the system still needs OAuth / garage configuration."""
        vin = _active_vin()
        if not vin:
            return True
        creds = db.fetch_one(
            "SELECT id FROM oauth_credentials WHERE vin = %s AND enabled = TRUE", (vin,)
        )
        return creds is None

    # ── Request hook: redirect to setup if not configured ──────────

    @app.before_request
    def _check_setup():
        if _needs_setup() and request.endpoint not in ("oauth_config", "reset", "manage", "manage_delete_vin", "manage_repoll", "db_browser", "db_table", "db_delete_row", "settings", "static"):
            return redirect(url_for("oauth_config"))

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
        form = {
            "provider": (existing or {}).get("provider", "ford"),
            "client_id": (existing or {}).get("client_id", ""),
            "client_secret": (existing or {}).get("client_secret", ""),
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
        """Application settings: display units (metric/imperial), polling intervals."""
        if request.method == "POST":
            _set_setting("units", request.form.get("units", "imperial"), "Display unit system")

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
            "poll_interval_off": _get_setting("poll_interval_off"),
            "poll_interval_on": _get_setting("poll_interval_on"),
            "poll_interval_moving": _get_setting("poll_interval_moving"),
            "poll_interval_charging": _get_setting("poll_interval_charging"),
        }
        return render_template("settings.html", settings=current)

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
                        row[secret_col] = val[:8] + "..." + val[-4:] if len(val) > 16 else "***"

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

        return render_template(
            "db_table.html",
            table_name=table_name,
            columns=columns,
            col_types=col_types,
            rows=rows,
            limit=limit,
            row_count=len(rows),
            pk_col=pk_col,
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

    @app.teardown_appcontext
    def _shutdown(exc):
        """Application teardown hook. Pool cleanup handled by atexit."""
        pass  # pool cleanup handled by atexit / signal

    return app


# ── Entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=(config.environment() == "development"))
