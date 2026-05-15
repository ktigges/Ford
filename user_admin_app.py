"""Local user administration app for the Ford Lightning prototype.

Author: Kevin Tigges
Copyright (c) 2026 Kevin Tigges
License: Open source prototype software
Notice: Use at your own risk.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from flask import Flask, flash, redirect, render_template_string, request, session, url_for

import config
import db
import local_auth


def create_user_admin_app() -> Flask:
    app = Flask(__name__)
    config.load()
    app.secret_key = (os.environ.get("LIGHTNING_USER_ADMIN_SECRET") or "local-user-admin-dev-secret").strip()
    secure_cookie = (os.environ.get("LIGHTNING_USER_ADMIN_COOKIE_SECURE", "0") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = secure_cookie

    try:
        db.init_pool()
    except Exception:
        pass

    if db.is_available():
        local_auth.ensure_schema()

    def _safe_next_url(candidate: str | None) -> str:
        if not candidate:
            return url_for("users")
        parsed = urlparse(candidate)
        if parsed.scheme or parsed.netloc:
            return url_for("users")
        if not candidate.startswith("/"):
            return url_for("users")
        return candidate

    def _admin_from_session() -> dict | None:
        user = local_auth.validate_authenticated_session(session)
        if not user:
            return None
        if not bool(user.get("is_admin")):
            return None
        return user

    def _require_admin():
        admin = _admin_from_session()
        if admin:
            return admin
        return None

    @app.route("/", methods=["GET"])
    def index():
        if _require_admin():
            return redirect(url_for("users"))
        return redirect(url_for("login"))

    @app.route("/bootstrap", methods=["GET", "POST"])
    def bootstrap():
        if local_auth.admin_count() > 0:
            return redirect(url_for("login"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""
            if not username or not password:
                flash("Username and password are required.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            else:
                try:
                    local_auth.create_user(
                        username=username,
                        password=password,
                        email=email,
                        is_admin=True,
                        is_active=True,
                    )
                    flash("Bootstrap admin created. Set up MFA at first login.", "success")
                    return redirect(url_for("login"))
                except Exception as exc:
                    flash(f"Failed to create admin user: {exc}", "error")

        return render_template_string(
            """
            <h2>Bootstrap Local Admin</h2>
            {% for cat, msg in get_flashed_messages(with_categories=true) %}<p><b>{{cat}}</b>: {{msg}}</p>{% endfor %}
            <form method="post">
              <label>Username <input name="username" required></label><br>
              <label>Email <input name="email"></label><br>
              <label>Password <input type="password" name="password" required></label><br>
              <label>Confirm <input type="password" name="confirm_password" required></label><br>
              <button type="submit">Create Admin</button>
            </form>
            """
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if local_auth.admin_count() == 0:
            return redirect(url_for("bootstrap"))

        next_url = _safe_next_url(request.values.get("next"))
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            user = local_auth.verify_password(username, password)
            if not user or not bool(user.get("is_admin")):
                flash("Invalid credentials.", "error")
            else:
                stage = "verify" if bool(user.get("mfa_enabled")) else "setup"
                local_auth.begin_pending_login(session, int(user["id"]), next_url, stage)
                if stage == "setup":
                    session["local_pending_mfa_secret"] = local_auth.generate_mfa_secret()
                    return redirect(url_for("mfa_setup"))
                return redirect(url_for("mfa_verify"))

        return render_template_string(
            """
            <h2>User Admin Login</h2>
            {% for cat, msg in get_flashed_messages(with_categories=true) %}<p><b>{{cat}}</b>: {{msg}}</p>{% endfor %}
            <form method="post">
              <input type="hidden" name="next" value="{{ next_url }}">
              <label>Username <input name="username" required></label><br>
              <label>Password <input type="password" name="password" required></label><br>
              <button type="submit">Continue</button>
            </form>
            """,
            next_url=next_url,
        )

    @app.route("/mfa/setup", methods=["GET", "POST"])
    def mfa_setup():
        pending = local_auth.pending_login(session)
        if pending["stage"] != "setup" or not pending["user_id"]:
            return redirect(url_for("login"))

        user = local_auth.get_user_by_id(pending["user_id"])
        if not user or not bool(user.get("is_admin")):
            local_auth.clear_session(session)
            return redirect(url_for("login"))

        secret = str(session.get("local_pending_mfa_secret") or "").strip()
        if not secret:
            secret = local_auth.generate_mfa_secret()
            session["local_pending_mfa_secret"] = secret
        otp_uri = local_auth.provisioning_uri(user, secret)
        qr_data_uri = local_auth.otp_qr_data_uri(otp_uri)

        if request.method == "POST":
            code = request.form.get("code") or ""
            if not local_auth.verify_totp(secret, code):
                flash("Invalid code. Try again.", "error")
            else:
                local_auth.enable_mfa(int(user["id"]), secret)
                local_auth.issue_authenticated_session(
                    session,
                    user,
                    ip_address=request.remote_addr or "",
                    user_agent=request.headers.get("User-Agent", ""),
                )
                return redirect(pending["next_url"] or url_for("users"))

        return render_template_string(
            """
            <h2>Admin MFA Setup</h2>
            {% for cat, msg in get_flashed_messages(with_categories=true) %}<p><b>{{cat}}</b>: {{msg}}</p>{% endfor %}
            <p>Add this secret to your authenticator app:</p>
            <p><code>{{ secret }}</code></p>
            <p>Provisioning URI:</p>
            <textarea rows="4" cols="80" readonly>{{ otp_uri }}</textarea>
                        {% if qr_data_uri %}
                        <p>Scan QR Code:</p>
                        <p><img src="{{ qr_data_uri }}" alt="MFA QR Code" width="220" height="220"></p>
                        {% endif %}
            <form method="post">
              <label>6-digit code <input name="code" required></label>
              <button type="submit">Verify & Finish</button>
            </form>
            """,
            secret=secret,
            otp_uri=otp_uri,
                        qr_data_uri=qr_data_uri,
        )

    @app.route("/mfa/verify", methods=["GET", "POST"])
    def mfa_verify():
        pending = local_auth.pending_login(session)
        if pending["stage"] != "verify" or not pending["user_id"]:
            return redirect(url_for("login"))

        user = local_auth.get_user_by_id(pending["user_id"])
        if not user or not bool(user.get("is_admin")) or not bool(user.get("mfa_enabled")):
            local_auth.clear_session(session)
            return redirect(url_for("login"))

        if request.method == "POST":
            code = request.form.get("code") or ""
            if not local_auth.verify_totp(str(user.get("mfa_secret") or ""), code):
                flash("Invalid code.", "error")
            else:
                local_auth.issue_authenticated_session(
                    session,
                    user,
                    ip_address=request.remote_addr or "",
                    user_agent=request.headers.get("User-Agent", ""),
                )
                return redirect(pending["next_url"] or url_for("users"))

        return render_template_string(
            """
            <h2>Admin MFA Verification</h2>
            {% for cat, msg in get_flashed_messages(with_categories=true) %}<p><b>{{cat}}</b>: {{msg}}</p>{% endfor %}
            <form method="post">
              <label>6-digit code <input name="code" required></label>
              <button type="submit">Sign In</button>
            </form>
            """
        )

    @app.route("/logout", methods=["POST", "GET"])
    def logout():
        local_auth.revoke_session(session)
        local_auth.clear_session(session)
        return redirect(url_for("login"))

    @app.route("/users", methods=["GET"])
    def users():
        admin = _require_admin()
        if not admin:
            return redirect(url_for("login", next=request.path))

        return render_template_string(
            """
            <h2>Local Users</h2>
            <p>Signed in as <b>{{ admin.username }}</b> (<a href="{{ url_for('logout') }}">logout</a>)</p>
            {% for cat, msg in get_flashed_messages(with_categories=true) %}<p><b>{{cat}}</b>: {{msg}}</p>{% endfor %}

            <h3>Create User</h3>
            <form method="post" action="{{ url_for('users_create') }}">
              <label>Username <input name="username" required></label>
              <label>Email <input name="email"></label>
              <label>Password <input type="password" name="password" required></label>
              <label><input type="checkbox" name="is_admin" value="1"> Admin</label>
              <button type="submit">Create</button>
            </form>

            <h3>Existing Users</h3>
            <table border="1" cellpadding="6" cellspacing="0">
              <tr><th>ID</th><th>Username</th><th>Email</th><th>Active</th><th>Admin</th><th>MFA</th><th>Actions</th></tr>
              {% for u in users %}
              <tr>
                <td>{{ u.id }}</td>
                <td>{{ u.username }}</td>
                <td>{{ u.email or '-' }}</td>
                <td>{{ 'yes' if u.is_active else 'no' }}</td>
                <td>{{ 'yes' if u.is_admin else 'no' }}</td>
                <td>{{ 'enabled' if u.mfa_enabled else 'disabled' }}</td>
                <td>
                  <form style="display:inline" method="post" action="{{ url_for('users_toggle', user_id=u.id) }}"><button type="submit">Toggle Active</button></form>
                  <form style="display:inline" method="post" action="{{ url_for('users_reset_mfa', user_id=u.id) }}"><button type="submit">Reset MFA</button></form>
                </td>
              </tr>
              {% endfor %}
            </table>
            """,
            admin=admin,
            users=local_auth.list_users(),
        )

    @app.route("/users/create", methods=["POST"])
    def users_create():
        admin = _require_admin()
        if not admin:
            return redirect(url_for("login"))

        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        is_admin = (request.form.get("is_admin") or "") in {"1", "true", "on", "yes"}
        try:
            local_auth.create_user(
                username=username,
                password=password,
                email=email,
                is_admin=is_admin,
                is_active=True,
            )
            flash(f"Created user '{username}'.", "success")
        except Exception as exc:
            flash(f"Create user failed: {exc}", "error")
        return redirect(url_for("users"))

    @app.route("/users/<int:user_id>/toggle", methods=["POST"])
    def users_toggle(user_id: int):
        admin = _require_admin()
        if not admin:
            return redirect(url_for("login"))
        user = local_auth.get_user_by_id(user_id)
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("users"))
        local_auth.set_user_active(user_id, not bool(user.get("is_active")))
        flash(f"Updated active state for '{user.get('username')}'.", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:user_id>/reset-mfa", methods=["POST"])
    def users_reset_mfa(user_id: int):
        admin = _require_admin()
        if not admin:
            return redirect(url_for("login"))
        local_auth.disable_mfa(user_id)
        flash("MFA reset. User must re-enroll at next login.", "success")
        return redirect(url_for("users"))

    return app


if __name__ == "__main__":
    app = create_user_admin_app()
    port = int(os.environ.get("LIGHTNING_USER_ADMIN_PORT", "5051"))
    app.run(host="0.0.0.0", port=port, debug=True)