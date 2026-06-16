"""Optional dashboard authentication — a single admin login gating the
read-only dashboard and its data API.

Stdlib-only (no new dependency): PBKDF2-SHA256 password verification and an
HMAC-SHA256-signed session cookie. The plaintext password NEVER lives in code
or .env — only a salted PBKDF2 hash (config DASHBOARD_AUTH_PASSWORD_HASH).
/health is intentionally NOT gated (compose healthcheck / external watchdog).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE = "bp_session"
_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 floor for PBKDF2-SHA256


class AuthRequired(Exception):
    """Raised by the dependency when a protected route lacks a valid session.
    The installed handler redirects HTML requests to /login, else returns 401."""


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(dk, expected)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(username: str, secret: str, ttl_seconds: int, *, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    body = _b64e(f"{username}|{issued + ttl_seconds}".encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_session(token: str, secret: str, *, now: int | None = None) -> str | None:
    parts = token.split(".")
    if len(parts) != 2:
        return None
    body, sig_b64 = parts
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    try:
        got = _b64d(sig_b64)
    except (ValueError, binascii.Error):
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        username, exp_s = _b64d(body).decode().split("|")
        exp = int(exp_s)
    except (ValueError, UnicodeDecodeError):
        return None
    current = int(time.time()) if now is None else now
    if current >= exp:
        return None
    return username


def is_authenticated(request: Request) -> bool:
    from app.config import get_settings

    settings = get_settings()
    if not settings.dashboard_auth_enabled:
        return True
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    return verify_session(token, settings.dashboard_session_secret) is not None


def require_dashboard_auth(request: Request) -> None:
    """FastAPI dependency for protected routes. Raises AuthRequired when auth
    is enabled and the request carries no valid session."""
    if not is_authenticated(request):
        raise AuthRequired


def authenticate(username: str, password: str) -> bool:
    from app.config import get_settings

    settings = get_settings()
    user_ok = hmac.compare_digest(username, settings.dashboard_auth_username)
    pass_ok = verify_password(password, settings.dashboard_auth_password_hash)
    return user_ok and pass_ok


def install_auth(app: FastAPI) -> None:
    """Register the AuthRequired handler: redirect browsers to /login, 401 API."""

    @app.exception_handler(AuthRequired)
    async def _handle(request: Request, exc: AuthRequired) -> RedirectResponse | JSONResponse:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": "authentication required"}, status_code=401)
