"""Entra External ID authentication helpers for Flask app routes.

Implements OIDC authorization-code login for server-side sessions.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import jwt
import requests

log = logging.getLogger("entra_external_id")

_OIDC_CACHE: dict[str, dict[str, Any]] = {}
_JWKS_CLIENTS: dict[str, jwt.PyJWKClient] = {}


def is_enabled(cfg: dict) -> bool:
    """Return True when External ID auth is enabled."""
    return bool(cfg and cfg.get("enabled"))


def _metadata_url(cfg: dict) -> str:
    """Resolve metadata endpoint from explicit URL or authority."""
    if cfg.get("metadata_url"):
        return str(cfg["metadata_url"]).strip()

    authority = str(cfg.get("authority", "")).rstrip("/")
    if not authority:
        raise ValueError("External ID config missing 'authority' or 'metadata_url'")
    return f"{authority}/.well-known/openid-configuration"


def oidc_config(cfg: dict) -> dict:
    """Fetch and cache OIDC discovery metadata."""
    meta_url = _metadata_url(cfg)
    cached = _OIDC_CACHE.get(meta_url)
    if cached:
        return cached

    resp = requests.get(meta_url, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    _OIDC_CACHE[meta_url] = data
    return data


def begin_auth_session(session_obj: dict, next_url: str | None = None) -> tuple[str, str]:
    """Create state/nonce and store in Flask session."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    session_obj["entra_auth_state"] = state
    session_obj["entra_auth_nonce"] = nonce
    if next_url:
        session_obj["entra_auth_next"] = next_url
    return state, nonce


def build_authorize_url(cfg: dict, session_obj: dict, next_url: str | None = None) -> str:
    """Build authorization endpoint URL and prime anti-CSRF state."""
    oidc = oidc_config(cfg)
    state, nonce = begin_auth_session(session_obj, next_url=next_url)

    scope = str(cfg.get("scope", "openid profile email")).strip()
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "response_mode": "query",
        "scope": scope,
        "state": state,
        "nonce": nonce,
    }
    return f"{oidc['authorization_endpoint']}?{urlencode(params)}"


def exchange_code_for_tokens(cfg: dict, code: str) -> dict:
    """Exchange auth code for token set using confidential client secret."""
    oidc = oidc_config(cfg)
    payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": cfg["redirect_uri"],
        "code": code,
        "scope": str(cfg.get("scope", "openid profile email")).strip(),
    }
    try:
        resp = requests.post(oidc["token_endpoint"], data=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        from datetime import datetime as _dt, timezone as _tz
        
        status = getattr(getattr(exc, "response", None), "status_code", "unknown")
        body = ""
        headers = {}
        if getattr(exc, "response", None) is not None:
            body = (exc.response.text or "")
            # Extract correlation IDs and diagnostic headers from Entra response
            resp_obj = exc.response
            for key in ("x-ms-request-id", "x-ms-diagnostics-id", "x-correlation-id", "request-id"):
                if key in resp_obj.headers:
                    headers[key] = resp_obj.headers[key]
        
        # Log full diagnostic details to debug log (server-side only)
        log.error(
            "[TOKEN_EXCHANGE_FAILED] status=%s endpoint=%s redirect_uri=%s client_id=%s",
            status,
            oidc.get("token_endpoint", ""),
            cfg.get("redirect_uri", ""),
            cfg.get("client_id", ""),
        )
        log.error(
            "[TOKEN_EXCHANGE_FAILED] Entra response headers: %s",
            headers,
        )
        log.error(
            "[TOKEN_EXCHANGE_FAILED] Entra response body (full): %s",
            body,
        )
        log.error(
            "[TOKEN_EXCHANGE_FAILED] Exception: %s",
            str(exc),
        )
        raise


def _jwks_client(cfg: dict) -> jwt.PyJWKClient:
    """Return cached JWKS client for the authority."""
    oidc = oidc_config(cfg)
    jwks_uri = oidc["jwks_uri"]
    client = _JWKS_CLIENTS.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(jwks_uri)
        _JWKS_CLIENTS[jwks_uri] = client
    return client


def validate_id_token(cfg: dict, id_token: str, expected_nonce: str | None = None) -> dict:
    """Validate signature + issuer + audience + expiry for ID token."""
    oidc = oidc_config(cfg)
    signing_key = _jwks_client(cfg).get_signing_key_from_jwt(id_token)

    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=cfg["client_id"],
        issuer=oidc["issuer"],
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )

    if expected_nonce and claims.get("nonce") != expected_nonce:
        raise ValueError("ID token nonce mismatch")

    if _to_list(cfg.get("allowed_tenants")) and not str(claims.get("tid") or "").strip():
        raise ValueError("ID token missing tenant claim 'tid'")

    return claims


def extract_user(claims: dict) -> dict:
    """Map token claims to stable app session user object."""
    return {
        "oid": claims.get("oid") or claims.get("sub"),
        "name": claims.get("name") or claims.get("given_name") or "Unknown",
        "username": claims.get("preferred_username") or claims.get("email") or "",
        "tenant": claims.get("tid", ""),
        "groups": claims.get("groups", []),
        "roles": claims.get("roles", []),
    }


def _to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        raw = [v.strip() for v in value.split(",")]
        return [v for v in raw if v]
    return [str(value).strip()]


def user_allowed_by_group(cfg: dict, claims: dict) -> bool:
    """Check role allow-list from External ID config (uses allowed_group_names for role GUIDs)."""
    allowed_group_ids = set(_to_list(cfg.get("allowed_groups")))
    allowed_role_ids = set(_to_list(cfg.get("allowed_group_names")))

    token_groups = set(_to_list(claims.get("groups")))
    token_roles = set(_to_list(claims.get("roles")))

    # If no allow-list configured, allow all authenticated users
    if not allowed_group_ids and not allowed_role_ids:
        return True

    # Check against group IDs (legacy support)
    if allowed_group_ids and token_groups.intersection(allowed_group_ids):
        return True

    # Check against role IDs (primary method)
    if allowed_role_ids and token_roles.intersection(allowed_role_ids):
        return True

    return False


def user_allowed_by_tenant(cfg: dict, claims: dict) -> bool:
    """Check tenant allow-list from External ID config."""
    allowed_tenants = set(_to_list(cfg.get("allowed_tenants")))
    if not allowed_tenants:
        return True
    tid = str(claims.get("tid") or "").strip()
    return bool(tid and tid in allowed_tenants)


def build_logout_url(cfg: dict, post_logout_redirect_uri: str | None = None) -> str | None:
    """Build provider logout URL when available."""
    try:
        oidc = oidc_config(cfg)
    except Exception:
        return None

    end_session = oidc.get("end_session_endpoint")
    if not end_session:
        return None

    params = {}
    if post_logout_redirect_uri:
        params["post_logout_redirect_uri"] = post_logout_redirect_uri
    if params:
        return f"{end_session}?{urlencode(params)}"
    return end_session
