"""Optional dashboard authentication — a single admin login gating the
read-only dashboard and its data API.

Stdlib-only (no new dependency): PBKDF2-SHA256 password verification and an
HMAC-SHA256-signed session cookie. The plaintext password NEVER lives in code
or .env — only a salted PBKDF2 hash.

Credentials come from ONE of two places, DB first:
  1. the ``dashboard_credentials`` row created by the first-run /setup screen
     (the source of truth once set; loaded into memory at startup), or
  2. the .env trio (DASHBOARD_AUTH_*) — back-compat for hand-provisioned hosts.
If neither exists while auth is enabled, the app is UNCONFIGURED: every gated
route redirects to /setup until a password is set (SetupRequired).

/health is intentionally NOT gated (compose healthcheck / external watchdog).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

SESSION_COOKIE = "bp_session"
_PBKDF2_ITERATIONS = 600_000  # OWASP 2023 floor for PBKDF2-SHA256
_PBKDF2_MIN_ITERATIONS = 100_000
_PBKDF2_MAX_ITERATIONS = 2_000_000
MAX_USERNAME_BYTES = 128
MAX_PASSWORD_BYTES = 1024
MAX_SESSION_SECRET_BYTES = 512
MIN_SESSION_SECRET_BYTES = 32
MAX_SESSION_TOKEN_BYTES = 4096
MAX_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def _bounded_utf8(value: str, maximum: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


class AuthRequired(Exception):
    """Raised by the dependency when a protected route lacks a valid session.
    The installed handler redirects HTML requests to /login, else returns 401."""


class SetupRequired(Exception):
    """Raised when auth is ENABLED but no admin credential exists yet (no DB
    row, no .env hash). The installed handler redirects HTML requests to /setup
    so the operator can create the first password; API requests get 503."""


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    if not password or not _bounded_utf8(password, MAX_PASSWORD_BYTES):
        raise ValueError("password length is outside supported bounds")
    if not _PBKDF2_MIN_ITERATIONS <= iterations <= _PBKDF2_MAX_ITERATIONS:
        raise ValueError("PBKDF2 iteration count is outside supported bounds")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not password or not _bounded_utf8(password, MAX_PASSWORD_BYTES) or len(stored) > 1024:
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    # Never let an attacker-controlled/legacy hash trigger an effectively
    # unbounded CPU job or weaken verification below the accepted floor.
    if not _PBKDF2_MIN_ITERATIONS <= iterations <= _PBKDF2_MAX_ITERATIONS:
        return False
    if not 16 <= len(salt) <= 64 or len(expected) != hashlib.sha256().digest_size:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(dk, expected)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_session(username: str, secret: str, ttl_seconds: int, *, now: int | None = None) -> str:
    if not username or not _bounded_utf8(username, MAX_USERNAME_BYTES):
        raise ValueError("username length is outside supported bounds")
    if not MIN_SESSION_SECRET_BYTES <= len(secret.encode("utf-8")) <= MAX_SESSION_SECRET_BYTES:
        raise ValueError("session secret length is outside supported bounds")
    if not 1 <= ttl_seconds <= MAX_SESSION_TTL_SECONDS:
        raise ValueError("session TTL is outside supported bounds")
    issued = int(time.time()) if now is None else now
    # Structured payload removes delimiter ambiguity (a username containing
    # '|' previously produced a cookie that sign_session emitted but
    # verify_session could never parse).
    payload = {"u": username, "iat": issued, "exp": issued + ttl_seconds}
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_session(token: str, secret: str, *, now: int | None = None) -> str | None:
    if len(token) > MAX_SESSION_TOKEN_BYTES:
        return None
    if not MIN_SESSION_SECRET_BYTES <= len(secret.encode("utf-8")) <= MAX_SESSION_SECRET_BYTES:
        return None
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
        decoded = json.loads(_b64d(body).decode())
        username = decoded["u"]
        issued = decoded["iat"]
        exp = decoded["exp"]
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return None
    if (
        not isinstance(username, str)
        or not username
        or not _bounded_utf8(username, MAX_USERNAME_BYTES)
        or not isinstance(issued, int)
        or isinstance(issued, bool)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
        or exp <= issued
        or exp - issued > MAX_SESSION_TTL_SECONDS
    ):
        return None
    current = int(time.time()) if now is None else now
    if issued > current + 60 or current >= exp:
        return None
    return username


# --- credential source: DB-loaded (preferred) -> .env (fallback) ------------


@dataclass(frozen=True)
class DashboardCredentials:
    username: str
    password_hash: str
    session_secret: str


_active: DashboardCredentials | None = None


def set_active_credentials(username: str, password_hash: str, session_secret: str) -> None:
    """Install the live admin credential. Called at startup from the DB row and
    again right after first-run /setup writes one. Takes precedence over .env."""
    if not username or not _bounded_utf8(username, MAX_USERNAME_BYTES):
        raise ValueError("username length is outside supported bounds")
    if len(password_hash) > 1024:
        raise ValueError("password hash length is outside supported bounds")
    if (
        not MIN_SESSION_SECRET_BYTES
        <= len(session_secret.encode("utf-8"))
        <= MAX_SESSION_SECRET_BYTES
    ):
        raise ValueError("session secret length is outside supported bounds")
    global _active
    _active = DashboardCredentials(username, password_hash, session_secret)


def reset_active_credentials() -> None:
    """Drop the in-memory credential (tests; returns to the .env fallback)."""
    global _active
    _active = None


def _current_credentials() -> DashboardCredentials | None:
    """The credential in force: the DB-loaded one if set, else the .env trio,
    else None (unconfigured — first-run /setup pending)."""
    if _active is not None:
        return _active
    from app.config import get_settings

    s = get_settings()
    hash_ = s.dashboard_auth_password_hash.get_secret_value()
    secret = s.dashboard_session_secret.get_secret_value()
    if hash_ and secret:
        return DashboardCredentials(s.dashboard_auth_username, hash_, secret)
    return None


def auth_is_configured() -> bool:
    """True once an admin credential exists (DB or .env)."""
    return _current_credentials() is not None


def current_credentials() -> DashboardCredentials | None:
    """The credential in force (DB-loaded or .env), or None if unconfigured.
    The login/setup routes sign the session cookie with this credential's secret
    — the SAME secret is_authenticated verifies against, never the (possibly
    blank) .env one."""
    return _current_credentials()


def is_authenticated(request: Request) -> bool:
    from app.config import get_settings

    if not get_settings().dashboard_auth_enabled:
        return True
    creds = _current_credentials()
    if creds is None:
        return False  # unconfigured: nobody is authenticated until /setup runs
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    return verify_session(token, creds.session_secret) is not None


def require_dashboard_auth(request: Request) -> None:
    """FastAPI dependency for protected routes. When auth is enabled: raises
    SetupRequired if no credential exists yet (-> /setup), else AuthRequired if
    the request carries no valid session (-> /login)."""
    from app.config import get_settings

    if not get_settings().dashboard_auth_enabled:
        return
    if not auth_is_configured():
        raise SetupRequired
    if not is_authenticated(request):
        raise AuthRequired


def authenticate(username: str, password: str) -> bool:
    creds = _current_credentials()
    if creds is None:
        return False
    if not _bounded_utf8(username, MAX_USERNAME_BYTES) or not _bounded_utf8(
        password, MAX_PASSWORD_BYTES
    ):
        return False
    # ``compare_digest(str, str)`` rejects non-ASCII text with TypeError. Both
    # setup/config intentionally allow UTF-8 usernames, so compare their bytes
    # instead and keep Unicode login attempts fail-closed rather than 500ing.
    user_ok = hmac.compare_digest(username.encode("utf-8"), creds.username.encode("utf-8"))
    pass_ok = verify_password(password, creds.password_hash)
    return user_ok and pass_ok


def install_auth(app: FastAPI) -> None:
    """Register the AuthRequired/SetupRequired handlers: redirect browsers to
    /login or /setup respectively, else 401/503 for API clients."""

    @app.exception_handler(AuthRequired)
    async def _handle_auth(request: Request, exc: AuthRequired) -> RedirectResponse | JSONResponse:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": "authentication required"}, status_code=401)

    @app.exception_handler(SetupRequired)
    async def _handle_setup(
        request: Request, exc: SetupRequired
    ) -> RedirectResponse | JSONResponse:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/setup", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": "setup required"}, status_code=503)
