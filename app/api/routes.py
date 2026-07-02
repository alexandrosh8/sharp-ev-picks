"""API routes: latest picks, manual result tracking, health.

POST /picks/{id}/result is the MANUAL result-tracking entrypoint — the user
records what THEY did (bet placed or not, stake, outcome). Nothing here can
place a bet.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    SESSION_COOKIE,
    auth_is_configured,
    authenticate,
    current_credentials,
    hash_password,
    is_authenticated,
    require_dashboard_auth,
    set_active_credentials,
    sign_session,
)
from app.api.deps import get_session
from app.backtesting.calibration import bet_band_reliability
from app.backtesting.live_evidence import live_evidence_report
from app.edge.confidence import confidence_rating
from app.resolution.shadow import summarize_anchor_coverage, summarize_match_rate
from app.schemas.events import EventResultIn, ResultIn
from app.settlement.engine import settle_event_picks
from app.settlement.outcomes import pick_pnl, pick_roi
from app.storage.models import Event, ManualBetLog, Pick, ResultTracking
from app.storage.repositories import (
    bet_band_observations,
    betfair_archive_capture_by_sport,
    betfair_inline_capture_by_sport,
    create_dashboard_credentials,
    latest_available_games_with_events,
    latest_picks_with_events,
    live_evidence_rows,
    performance_report,
    pinnacle_archive_capture_by_sport,
    shadow_match_rate_outcomes,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Self-contained dashboard page (no build step, no CDN — works offline and
# identically on the Ubuntu VPS). Data is fetched from /picks client-side.
_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

# Self-contained dark login page (no CDN/JS libs). Posts JSON to /login; on
# success redirects to /. No credential is ever embedded here, and the error
# message is set via textContent (never innerHTML) so a server string can't
# inject markup.
_LOGIN_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>TAPE — sign in</title>
    <style>
      :root {
        --bg: #100d09;
        --surface-1: #16120c;
        --surface-2: #1e1810;
        --line: #2d2417;
        --text: #ece2cf;
        --dim: #b4a78f;
        --faint: #8a7e67;
        --pos: #4fc78d;
        --neg: #e2554a;
        --info: #d3a02f;
        --radius: 3px;
        --radius-sm: 3px;
        --font-display:
          ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas,
          monospace;
        --mono:
          ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Menlo,
          Consolas, monospace;
      }
      * { box-sizing: border-box; margin: 0; }
      html { background: var(--bg); }
      body {
        color: var(--text);
        font: 13px/1.5 var(--mono);
        font-variant-numeric: tabular-nums;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        background:
          radial-gradient(820px 360px at 50% -10%,
            rgba(79, 199, 141, 0.07), transparent 60%),
          repeating-linear-gradient(0deg, transparent 0 23px, rgba(45, 36, 23, 0.28) 23px 24px),
          repeating-linear-gradient(90deg, transparent 0 23px, rgba(45, 36, 23, 0.16) 23px 24px),
          var(--bg);
      }
      .card {
        width: 100%;
        max-width: 360px;
        border: 1px solid var(--line);
        border-radius: var(--radius);
        background: linear-gradient(180deg, var(--surface-2), var(--surface-1));
        padding: 26px 24px 22px;
        box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
      }
      .brand {
        display: flex;
        align-items: baseline;
        font-family: var(--font-display);
        font-size: 20px;
        font-weight: 700;
        letter-spacing: 0.22em;
        color: var(--text);
      }
      .brand .mark { color: var(--pos); letter-spacing: 0; margin-right: 7px; }
      .brand .tick { color: var(--pos); }
      .sub {
        color: var(--faint);
        font-size: 10px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        margin: 7px 0 20px;
      }
      label {
        display: block;
        color: var(--dim);
        font-family: var(--font-display);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin: 14px 0 6px;
      }
      input {
        width: 100%;
        background: var(--surface-1);
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
        padding: 10px 12px;
        font: 13px var(--mono);
        letter-spacing: 0.02em;
      }
      input:hover { border-color: var(--faint); }
      input:focus-visible { outline: 2px solid var(--pos); outline-offset: 2px; }
      button {
        width: 100%;
        margin-top: 22px;
        cursor: pointer;
        background: rgba(79, 199, 141, 0.10);
        color: var(--pos);
        border: 1px solid var(--pos);
        border-radius: var(--radius-sm);
        padding: 11px 12px;
        font: 600 11px var(--mono);
        letter-spacing: 0.16em;
        text-transform: uppercase;
        transition: background-color 120ms, box-shadow 120ms;
      }
      button:hover { background: rgba(79, 199, 141, 0.18); box-shadow: 0 0 0 1px var(--pos); }
      button:focus-visible { outline: 2px solid var(--pos); outline-offset: 2px; }
      .err {
        color: var(--neg);
        font-size: 11px;
        min-height: 16px;
        margin-top: 13px;
        letter-spacing: 0.02em;
      }
      @media (prefers-reduced-motion: reduce) {
        * { transition: none !important; animation: none !important; }
      }
    </style>
  </head>
  <body>
    <form class="card" id="login-form" autocomplete="off">
      <div class="brand"><span class="mark">▌</span>TAPE<span class="tick">.</span></div>
      <div class="sub">picks terminal · sign in</div>
      <label for="u">Username</label>
      <input id="u" name="username" type="text" autocomplete="username" autofocus />
      <label for="p">Password</label>
      <input id="p" name="password" type="password" autocomplete="current-password" />
      <button type="submit">Sign in</button>
      <div class="err" id="err" role="alert"></div>
    </form>
    <script>
      "use strict";
      const form = document.getElementById("login-form");
      const errEl = document.getElementById("err");
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        errEl.textContent = "";
        const username = document.getElementById("u").value;
        const password = document.getElementById("p").value;
        try {
          const res = await fetch("/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
          });
          if (res.ok) {
            window.location = "/";
            return;
          }
          errEl.textContent =
            res.status === 401
              ? "Invalid username or password"
              : "Sign-in failed (HTTP " + res.status + ")";
        } catch (e) {
          errEl.textContent = "Sign-in failed — could not reach the server";
        }
      });
    </script>
  </body>
</html>
"""


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_auth)],
)
async def dashboard(response: Response) -> str:
    # Never browser-cache the HTML shell: a deploy ships new structure (panels,
    # badges, banner) but the page only reloads on a full refresh — the 60s
    # auto-refresh re-fetches DATA, not the page. A cached shell would mask the
    # update behind a stale tab (and caching auth-gated HTML is undesirable).
    response.headers["Cache-Control"] = "no-store"
    return _DASHBOARD_HTML


# --- Installable-PWA assets (PUBLIC, no auth) -------------------------------
# The manifest declares the standalone app (home-screen install, own window);
# the service worker enables install. The SW is a deliberate network PASS-
# THROUGH: it never caches the auth-gated shell or any data (mirrors the /
# no-store note above). Both are tiny inline strings, like /login and /setup —
# no build step, no CDN. Icons are inline SVG data URIs (the ring-and-dot mark).
_PWA_MANIFEST = (
    '{"name":"Picks Terminal","short_name":"Picks",'
    '"description":"+EV picks decision-support. You review and place every bet yourself.",'
    '"start_url":"/","scope":"/","display":"standalone",'
    '"orientation":"portrait-primary","background_color":"#0a0c10","theme_color":"#0a0c10",'
    '"icons":['
    "{\"src\":\"data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20192%20192'%3E%3Crect%20width='192'%20height='192'%20rx='42'%20fill='%230a0c10'/%3E%3Ccircle%20cx='96'%20cy='96'%20r='54'%20fill='none'%20stroke='%2338bdf8'%20stroke-width='11'/%3E%3Ccircle%20cx='96'%20cy='96'%20r='17'%20fill='%2334d399'/%3E%3C/svg%3E\",\"sizes\":\"192x192\",\"type\":\"image/svg+xml\",\"purpose\":\"any\"},"
    "{\"src\":\"data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20512%20512'%3E%3Crect%20width='512'%20height='512'%20fill='%230a0c10'/%3E%3Ccircle%20cx='256'%20cy='256'%20r='118'%20fill='none'%20stroke='%2338bdf8'%20stroke-width='26'/%3E%3Ccircle%20cx='256'%20cy='256'%20r='40'%20fill='%2334d399'/%3E%3C/svg%3E\",\"sizes\":\"512x512\",\"type\":\"image/svg+xml\",\"purpose\":\"maskable\"}"
    "]}"
)
_SERVICE_WORKER = (
    "self.addEventListener('install',function(){self.skipWaiting();});"
    "self.addEventListener('activate',function(e){e.waitUntil(self.clients.claim());});"
    # No 'fetch' handler: the app caches nothing, and a NO-OP fetch handler is
    # flagged by Chrome as needless navigation overhead ("recognized as no-op").
    # Modern browsers keep the PWA installable from the manifest + this SW alone.
)


@router.get("/manifest.webmanifest", include_in_schema=False)
async def web_manifest() -> Response:
    return Response(
        _PWA_MANIFEST,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    return Response(
        _SERVICE_WORKER,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


class _LoginIn(BaseModel):
    username: str
    password: str


# --- /login throttle (WP7 fix 4) --------------------------------------------
# Each /login attempt burns a 600k-iteration PBKDF2 hash; unthrottled, a bot
# gets free brute-force AND a cheap CPU-DoS on a 2-CPU box. Simple in-process
# fixed window per source IP: after LOGIN_MAX_FAILURES failures inside
# LOGIN_WINDOW_SECONDS the endpoint answers 429 BEFORE hashing. In-memory by
# design (single-process app; a restart forgiving the window is acceptable).
# Named constants rather than env — one-line promotion to Settings if needed.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300.0
_LOGIN_THROTTLE_MAX_KEYS = 1024  # bounded memory even under spoofed-IP spray

#: ip -> (window_start monotonic seconds, failures in window)
_login_failures: dict[str, tuple[float, int]] = {}


def reset_login_throttle() -> None:
    """Clear all throttle state (tests)."""
    _login_failures.clear()


def _login_retry_after(ip: str, now: float | None = None) -> int | None:
    """Whole seconds until `ip` may try again, or None when not throttled."""
    now = time.monotonic() if now is None else now
    entry = _login_failures.get(ip)
    if entry is None:
        return None
    window_start, failures = entry
    if now - window_start >= LOGIN_WINDOW_SECONDS or failures < LOGIN_MAX_FAILURES:
        return None
    return max(1, int(window_start + LOGIN_WINDOW_SECONDS - now) + 1)


def _login_record_failure(ip: str, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    entry = _login_failures.get(ip)
    if entry is None or now - entry[0] >= LOGIN_WINDOW_SECONDS:
        if len(_login_failures) >= _LOGIN_THROTTLE_MAX_KEYS:
            # drop expired windows first; if a spray keeps it full, drop oldest
            expired = [
                k
                for k, (start, _) in _login_failures.items()
                if now - start >= LOGIN_WINDOW_SECONDS
            ]
            for key in expired:
                _login_failures.pop(key, None)
            while len(_login_failures) >= _LOGIN_THROTTLE_MAX_KEYS:
                oldest = min(_login_failures, key=lambda k: _login_failures[k][0])
                _login_failures.pop(oldest, None)
        _login_failures[ip] = (now, 1)
        return
    _login_failures[ip] = (entry[0], entry[1] + 1)


def _login_record_success(ip: str) -> None:
    _login_failures.pop(ip, None)


def _client_ip(request: Request) -> str:
    """Throttle key: the DIRECT peer address only — X-Forwarded-For is
    attacker-controlled and must never widen or reset someone else's window.
    Behind a reverse proxy all requests share the proxy's address, which only
    makes the guard STRICTER (fine for a single-operator dashboard)."""
    return request.client.host if request.client is not None else "unknown"


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request) -> Response:
    from app.config import get_settings

    # An enabled-but-unconfigured app has no password yet: send the operator to
    # the first-run /setup screen rather than an unusable login form.
    if get_settings().dashboard_auth_enabled and not auth_is_configured():
        return RedirectResponse("/setup", status_code=303)
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_LOGIN_HTML)


def _session_response(
    username: str, session_secret: str, ttl_seconds: int, *, secure: bool
) -> JSONResponse:
    """Issue the signed-session cookie. Signed with the ACTIVE credential's
    secret (DB-loaded or .env) — the same secret auth verifies against, never
    the possibly-blank .env value."""
    token = sign_session(username, session_secret, ttl_seconds)
    resp = JSONResponse({"status": "ok"})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return resp


@router.post("/login", include_in_schema=False)
async def login_submit(payload: _LoginIn, request: Request) -> Response:
    from app.config import get_settings

    settings = get_settings()
    # WP7 fix 4: answer 429 BEFORE the expensive hash once this source address
    # has exhausted its failure window (brute-force + PBKDF2-CPU-DoS guard).
    ip = _client_ip(request)
    retry_after = _login_retry_after(ip)
    if retry_after is not None:
        return JSONResponse(
            {"detail": "too many failed attempts — try again later"},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    # authenticate() runs a 600k-iteration PBKDF2 hash — offload it to a worker
    # thread so a burst of login attempts can't block the event loop (and with
    # it every other request + the scheduler) until the hashes finish.
    if not await asyncio.to_thread(authenticate, payload.username, payload.password):
        _login_record_failure(ip)
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    _login_record_success(ip)
    creds = current_credentials()
    if creds is None:  # unconfigured (race): nothing to sign with
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    return _session_response(
        creds.username,
        creds.session_secret,
        settings.dashboard_session_ttl_seconds,
        secure=(settings.app_env != "local"),
    )


@router.post("/logout", include_in_schema=False)
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# First-run setup page — shown ONLY while auth is enabled and no admin
# credential exists yet. Same PICKS TERMINAL skin as /login; posts JSON to
# /setup; on success the credential is stored in the DB and the operator is
# signed in. Plaintext never leaves the form; errors render via textContent.
_SETUP_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>TAPE — first-run setup</title>
    <style>
      :root {
        --bg: #100d09;
        --surface-1: #16120c;
        --surface-2: #1e1810;
        --line: #2d2417;
        --text: #ece2cf;
        --dim: #b4a78f;
        --faint: #8a7e67;
        --pos: #4fc78d;
        --neg: #e2554a;
        --info: #d3a02f;
        --radius: 3px;
        --radius-sm: 3px;
        --font-display:
          ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas,
          monospace;
        --mono:
          ui-monospace, "SF Mono", "JetBrains Mono", "Cascadia Code", Menlo,
          Consolas, monospace;
      }
      * { box-sizing: border-box; margin: 0; }
      html { background: var(--bg); }
      body {
        color: var(--text);
        font: 13px/1.5 var(--mono);
        font-variant-numeric: tabular-nums;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        background:
          radial-gradient(820px 360px at 50% -10%,
            rgba(79, 199, 141, 0.07), transparent 60%),
          repeating-linear-gradient(0deg, transparent 0 23px, rgba(45, 36, 23, 0.28) 23px 24px),
          repeating-linear-gradient(90deg, transparent 0 23px, rgba(45, 36, 23, 0.16) 23px 24px),
          var(--bg);
      }
      .card {
        width: 100%;
        max-width: 360px;
        border: 1px solid var(--line);
        border-radius: var(--radius);
        background: linear-gradient(180deg, var(--surface-2), var(--surface-1));
        padding: 26px 24px 22px;
        box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
      }
      .brand {
        display: flex;
        align-items: baseline;
        font-family: var(--font-display);
        font-size: 20px;
        font-weight: 700;
        letter-spacing: 0.22em;
        color: var(--text);
      }
      .brand .mark { color: var(--pos); letter-spacing: 0; margin-right: 7px; }
      .brand .tick { color: var(--pos); }
      .sub {
        color: var(--faint);
        font-size: 10px;
        letter-spacing: 0.18em;
        text-transform: uppercase;
        margin: 7px 0 20px;
      }
      label {
        display: block;
        color: var(--dim);
        font-family: var(--font-display);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        margin: 14px 0 6px;
      }
      input {
        width: 100%;
        background: var(--surface-1);
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: var(--radius-sm);
        padding: 10px 12px;
        font: 13px var(--mono);
        letter-spacing: 0.02em;
      }
      input:hover { border-color: var(--faint); }
      input:focus-visible { outline: 2px solid var(--pos); outline-offset: 2px; }
      button {
        width: 100%;
        margin-top: 22px;
        cursor: pointer;
        background: rgba(79, 199, 141, 0.10);
        color: var(--pos);
        border: 1px solid var(--pos);
        border-radius: var(--radius-sm);
        padding: 11px 12px;
        font: 600 11px var(--mono);
        letter-spacing: 0.16em;
        text-transform: uppercase;
        transition: background-color 120ms, box-shadow 120ms;
      }
      button:hover { background: rgba(79, 199, 141, 0.18); box-shadow: 0 0 0 1px var(--pos); }
      button:focus-visible { outline: 2px solid var(--pos); outline-offset: 2px; }
      .hint { color: var(--faint); font-size: 10px; margin-top: 6px; letter-spacing: 0.02em; }
      .err {
        color: var(--neg);
        font-size: 11px;
        min-height: 16px;
        margin-top: 13px;
        letter-spacing: 0.02em;
      }
      @media (prefers-reduced-motion: reduce) {
        * { transition: none !important; animation: none !important; }
      }
    </style>
  </head>
  <body>
    <form class="card" id="setup-form" autocomplete="off">
      <div class="brand"><span class="mark">▌</span>TAPE<span class="tick">.</span></div>
      <div class="sub">first run · create your admin password</div>
      <label for="u">Username</label>
      <input id="u" name="username" type="text" autocomplete="username" value="admin" />
      <label for="p">Password</label>
      <input id="p" name="password" type="password" autocomplete="new-password" autofocus />
      <div class="hint">At least 8 characters. Stored only as a salted hash.</div>
      <label for="c">Confirm password</label>
      <input id="c" name="confirm" type="password" autocomplete="new-password" />
      <button type="submit">Create &amp; sign in</button>
      <div class="err" id="err" role="alert"></div>
    </form>
    <script>
      "use strict";
      const form = document.getElementById("setup-form");
      const errEl = document.getElementById("err");
      const MIN = 8;
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        errEl.textContent = "";
        const username = document.getElementById("u").value.trim() || "admin";
        const password = document.getElementById("p").value;
        const confirm = document.getElementById("c").value;
        if (password.length < MIN) {
          errEl.textContent = "Password must be at least " + MIN + " characters";
          return;
        }
        if (password !== confirm) {
          errEl.textContent = "Passwords do not match";
          return;
        }
        try {
          const res = await fetch("/setup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
          });
          if (res.ok) {
            window.location = "/";
            return;
          }
          let detail = "Setup failed (HTTP " + res.status + ")";
          try {
            const j = await res.json();
            if (j && j.detail) detail = j.detail;
          } catch (e) {}
          errEl.textContent = detail;
        } catch (e) {
          errEl.textContent = "Setup failed — could not reach the server";
        }
      });
    </script>
  </body>
</html>
"""

_MIN_PASSWORD_LEN = 8


class _SetupIn(BaseModel):
    username: str
    password: str


# WP7 fix 5: headers whose PRESENCE proves the request came through a proxy.
# They are never trusted for their VALUE (trivially spoofable) — only as
# evidence that the peer is not the operator's own direct loopback connection.
_PROXY_EVIDENCE_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


def _setup_request_is_local(request: Request) -> bool:
    """True only for a DIRECT loopback connection with no proxy evidence.

    The config-time /setup guard keys off APP_HOST_BIND, which a reverse proxy
    (Traefik) bypasses: the proxy dials 127.0.0.1 so the peer address LOOKS
    local while the real client is the public internet. Per-request defence:
    any Forwarded-style header ⇒ proxied ⇒ denied, and a non-loopback peer ⇒
    denied. X-Forwarded-For is never read for its value — a spoofed
    'X-Forwarded-For: 127.0.0.1' cannot grant access, only deny it."""
    if any(header in request.headers for header in _PROXY_EVIDENCE_HEADERS):
        return False
    if request.client is None:  # in-process test app; real servers set the peer
        return True
    host = request.client.host.strip().strip("[]").lower()
    return host in ("localhost", "::1", "::ffff:127.0.0.1") or host.startswith("127.")


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_form(request: Request) -> Response:
    from app.config import get_settings

    # WP7 fix 5: first-run credential claim only over direct loopback — a
    # reverse-proxied (public) visitor must never even learn /setup exists.
    if not _setup_request_is_local(request):
        return JSONResponse({"detail": "not found"}, status_code=404)
    # /setup exists ONLY while auth is enabled and no credential is set yet.
    # Once configured it disappears — changing the password later must go
    # through an authenticated path, never this unauthenticated endpoint.
    if not get_settings().dashboard_auth_enabled or auth_is_configured():
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_SETUP_HTML)


@router.post("/setup", include_in_schema=False)
async def setup_submit(
    payload: _SetupIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    from app.config import get_settings

    settings = get_settings()
    # WP7 fix 5: same direct-loopback gate as the GET — the POST is the part
    # that actually claims the admin credential.
    if not _setup_request_is_local(request):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not settings.dashboard_auth_enabled:
        return JSONResponse({"detail": "auth is disabled"}, status_code=404)
    if auth_is_configured():
        return JSONResponse({"detail": "already configured"}, status_code=409)
    username = payload.username.strip() or "admin"
    if len(payload.password) < _MIN_PASSWORD_LEN:
        return JSONResponse(
            {"detail": f"password must be at least {_MIN_PASSWORD_LEN} characters"},
            status_code=400,
        )
    # 600k-iteration PBKDF2 — offload off the event loop, like /login.
    password_hash = await asyncio.to_thread(hash_password, payload.password)
    session_secret = secrets.token_urlsafe(48)
    created = await create_dashboard_credentials(
        session,
        username=username,
        password_hash=password_hash,
        session_secret=session_secret,
    )
    if not created:  # raced another first-run request
        return JSONResponse({"detail": "already configured"}, status_code=409)
    set_active_credentials(username, password_hash, session_secret)
    return _session_response(
        username,
        session_secret,
        settings.dashboard_session_ttl_seconds,
        secure=(settings.app_env != "local"),
    )


#: P0-3 /health liveness ceiling: the newest recorded poll must have FINISHED
#: within HEALTH_MAX_POLL_AGE_MULTIPLIER x poll_interval_seconds, else the engine
#: is judged starved/dead (HTTP 503). Named here rather than env (config.py is
#: owned elsewhere) — a one-line promotion to Settings if it ever needs tuning.
HEALTH_MAX_POLL_AGE_MULTIPLIER = 3


def _newest_poll_finish(polls: Mapping[str, Mapping[str, Any]]) -> datetime | None:
    """Most-recent `finished_at` across all recorded poll cycles (None if none).

    Parses the ISO-8601 UTC string each cycle writes to LAST_POLL; a missing or
    unparseable value is skipped rather than treated as a death signal."""
    newest: datetime | None = None
    for poll in polls.values():
        raw = poll.get("finished_at")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if newest is None or parsed > newest:
            newest = parsed
    return newest


def _poll_health(
    polls: Mapping[str, Mapping[str, Any]],
    now: datetime,
    poll_interval_seconds: int,
    max_age_multiplier: int = HEALTH_MAX_POLL_AGE_MULTIPLIER,
) -> tuple[str, int, float | None]:
    """Liveness from poll FRESHNESS, not pick count — a quiet slate that still
    completes cycles is healthy; a stale newest-cycle means a starved/dead engine.

    Returns (status, http_status, newest_poll_age_seconds):
    - No recorded cycle at all -> ok/200 (cold start / router-only test app /
      no engine — there is no evidence of death yet, so never 503 here).
    - Newest cycle finished within max_age_multiplier x poll_interval -> ok/200.
    - Older than that ceiling -> degraded/503."""
    newest = _newest_poll_finish(polls)
    if newest is None:
        return "ok", 200, None
    age = (now - newest).total_seconds()
    if age > max_age_multiplier * poll_interval_seconds:
        return "degraded", 503, age
    return "ok", 200, age


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    from app.config import get_settings
    from app.maintenance.upstream_watch import LAST_CHECK
    from app.pipeline import LAST_POLL

    settings = get_settings()
    # P0-3: real liveness — a process that is up but whose poll cycles stopped
    # finishing (starved/dead scraper) now reads degraded/503 instead of a
    # hardcoded "ok"/200. Based on poll freshness, never pick count.
    status, http_status, newest_age = _poll_health(
        LAST_POLL, datetime.now(tz=UTC), settings.poll_interval_seconds
    )
    response.status_code = http_status
    # WP7 fix 5: /health stays public for liveness (compose healthcheck /
    # external watchdog: status + HTTP code), but the DETAIL — dependency
    # versions in `upstream`, poll internals, strategy edge floors — is for
    # the authenticated dashboard only. is_authenticated() is True when
    # dashboard auth is disabled (local dev keeps the full payload) and False
    # for anonymous visitors once auth is enabled (public Traefik exposure).
    if not is_authenticated(request):
        return {"status": status, "mode": "picks-only"}
    return {
        "status": status,
        "mode": "picks-only",
        "upstream": LAST_CHECK,
        "polls": LAST_POLL,
        # Newest cycle's age + the staleness ceiling the dead-engine check uses
        # (N x poll_interval). status flips to "degraded" (503) when the age
        # exceeds it. None age == no cycle recorded yet (still "ok").
        "newest_poll_age_seconds": newest_age,
        "poll_max_age_seconds": settings.poll_interval_seconds * HEALTH_MAX_POLL_AGE_MULTIPLIER,
        # The dashboard derives its "verified within" window from the value
        # freshness window (MAX_ODDS_AGE_SECONDS): a pick whose last re-price is
        # older than this has a STALE price and must read UNVERIFIED, not show a
        # current "now" (audit 2026-06-26). poll_interval is the cadence fallback.
        "poll_interval_seconds": get_settings().poll_interval_seconds,
        "max_odds_age_seconds": get_settings().max_odds_age_seconds,
        # Tier edge floors so the dashboard colours edges/verdicts against the
        # floor the pick was actually held to (premium vs volume), not a
        # hardcoded 3% (dash-2 / EEV-1). The per-pick payload also carries a
        # tier-resolved `edge_floor`; these are the global fallback.
        "value_min_edge": get_settings().value_min_edge,
        "value_volume_min_edge": get_settings().value_volume_min_edge,
    }


def _coerce_float(value: Any) -> float | None:
    """Best-effort str/Decimal -> float for repo rows (every numeric is a
    serialized string). None/blank/unparseable -> None so the caller can fall
    back to a stated neutral input."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attach_confidence(
    rows: list[dict[str, Any]], threshold: float, volume_threshold: float
) -> list[dict[str, Any]]:
    """Add a `confidence_rating` block to each /picks row from existing fields.

    The star rating is the dashboard headline that replaces the recommended
    stake (the stake moves to a hover tooltip). It rates confidence in the
    EDGE (sharp-vs-soft line value), NOT a win probability — see
    app/edge/confidence. Computed here at the route (composition root) so the
    repository layer and the pure rating module both stay clean: the repo only
    serializes DB rows, the rating module only does arithmetic.

    `threshold` is Settings.value_min_edge (the premium edge floor); the live
    edge is preferred over alert-time edge when present, mirroring the
    dashboard's own `current_edge ?? edge` choice. There is no per-pick
    book-count field today, so book_count is None.
    """
    for row in rows:
        edge = _coerce_float(row.get("current_edge"))
        if edge is None:
            edge = _coerce_float(row.get("edge")) or 0.0
        # rate against the floor the pick was held to (volume vs premium) — audit #2
        thr = volume_threshold if row.get("tier") == "volume" else threshold
        rating = confidence_rating(
            edge=edge,
            threshold=thr,
            value_filter_score=_coerce_float(row.get("value_filter_score")),
            anchor_type=row.get("anchor_type"),
            book_count=None,
        )
        row["confidence_rating"] = {
            "level": rating.level,
            "label": rating.label,
            "reasons": list(rating.reasons),
        }
    return rows


@router.get("/picks", dependencies=[Depends(require_dashboard_auth)])
async def latest_picks(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    tier: Annotated[str | None, Query(pattern="^(premium|volume)$")] = None,
) -> list[dict[str, Any]]:
    """Latest picks, newest first. `tier` scopes the window server-side —
    the volume shadow tier runs ~6x premium volume, so an unscoped
    latest-200 window would fill with volume rows and hide open premium
    picks entirely (the dashboard fetches each tier separately).
    None = both tiers (legacy feed).

    Each row carries a `confidence_rating` (1..5 star edge-quality headline);
    the recommended stake stays on the row but is surfaced only in a hover
    tooltip on the dashboard (informational, never advice).

    `min_acceptable_odds` per row is the execution helper: the minimum
    displayed odds at which the pick still retains the premium edge floor
    ("still +EV down to X.XX" on the dashboard)."""
    from app.config import get_settings

    settings = get_settings()
    threshold = settings.value_min_edge
    volume_threshold = settings.value_volume_min_edge
    rows = await latest_picks_with_events(
        session, limit, tier=tier, min_edge=threshold, volume_min_edge=volume_threshold
    )
    return _attach_confidence(rows, threshold, volume_threshold)


async def _warehouse_available_games(
    request: Request,
    limit: int,
    sport: str | None,
) -> list[dict[str, Any]]:
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        return []
    try:
        async with session_factory() as session:
            return await latest_available_games_with_events(session, limit=limit, sport=sport)
    except Exception as exc:
        logger.warning("available games warehouse fallback failed: %s", type(exc).__name__)
        return []


@router.get("/games", dependencies=[Depends(require_dashboard_auth)])
async def available_games(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
    sport: Annotated[str | None, Query(pattern="^(soccer|basketball|tennis)$")] = None,
) -> list[dict[str, Any]]:
    """Latest unrestricted football/NBA fixture list from odds ingestion.

    This is a read-only visibility feed. It does not apply edge, odds-age,
    exposure, tier, or pick-status gates; those remain exclusive to /picks.
    """
    from app.pipeline import AVAILABLE_GAMES

    rows: list[dict[str, Any]] = []
    for sport_key in sorted(AVAILABLE_GAMES):
        if sport is not None and sport_key != sport and not sport_key.startswith(f"{sport}_"):
            continue
        rows.extend(AVAILABLE_GAMES[sport_key])
    if not rows:
        rows = await _warehouse_available_games(request, limit=limit, sport=sport)
    rows.sort(key=lambda row: (row["starts_at"] is None, row["starts_at"] or "", row["event"]))
    return rows[:limit]


@lru_cache(maxsize=1)
def _ml_operating_point() -> float | None:
    """The configured value-filter manifest's frozen q* (None = no artifact).

    Cached for the process lifetime: artifacts only change at deploy, and a
    per-request disk read would be blocking IO in the event loop. Reports
    accept ANY manifest verdict — stratifying shadow scores is annotation,
    never enforcement (demotion keeps ValueFilterModel.load's ADOPT gate).
    """
    from app.config import get_settings
    from app.models.value_filter import manifest_operating_point

    settings = get_settings()
    return manifest_operating_point(
        Path(settings.value_ml_model_dir), settings.value_ml_manifest_filename
    )


@router.get("/performance", dependencies=[Depends(require_dashboard_auth)])
async def performance(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """ROI + stake-weighted log-CLV over settled picks (phase 4 report).

    Headline fields are PREMIUM-tier scoped ("tier_scope": "premium"); the
    volume shadow tier's aggregates ride under "volume" so its many small
    edges can never distort the alerted strategy's numbers.

    "live_evidence" stratifies the settled picks by ML score bucket
    (q* from the configured manifest), tier, and — once the column lands —
    anchor type: the accumulating live instrument for the VALUE_ML_FILTER
    flip decision. Every stratum carries its n; strata under min_n are
    flagged insufficient and the dashboard shows the state, not estimates.
    """
    report = await performance_report(session)
    rows = await live_evidence_rows(session)
    report["live_evidence"] = live_evidence_report(rows, ml_threshold=_ml_operating_point())
    # P1-1 claimed-fair RELIABILITY MONITOR (report-only — NOT a release gate,
    # NOT a recalibration haircut): does model_probability match the realized
    # win-rate in the odds band actually bet? Surfaced beside ROI/CLV so a
    # calibration drift is visible; its own insufficient-n honesty gate applies.
    band_obs = await bet_band_observations(session)
    report["calibration"] = bet_band_reliability(band_obs)
    return report


@router.get("/resolution/match-rate", dependencies=[Depends(require_dashboard_auth)])
async def resolution_match_rate(
    session: Annotated[AsyncSession, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> dict[str, Any]:
    """Strict SHADOW Pinnacle-archive match rate over picks with a known kickoff
    — the instrument ADR-0014 asks be checked BEFORE CLV_USE_PINNACLE_ARCHIVE is
    enabled. Read-only: no close is attached and nothing is written. ``days``
    scopes the population to kickoffs within the last N days.

    A low rate is diagnosable, never guessed: ``no_archive_candidates`` is a
    COVERAGE gap (capture more / enable ARCADIA_ENABLED), ``unmatched_with_
    candidates`` an ALIAS gap (extend the alias table).
    """
    since = datetime.now(tz=UTC) - timedelta(days=days) if days is not None else None
    outcomes = await shadow_match_rate_outcomes(session, since=since)
    report = summarize_match_rate(outcomes).as_dict()
    # Per-sport upcoming capture for ALL arcadia sports (tennis + american_football
    # included), so the panel shows the archive captures every sport, not just the
    # pick sports that appear in the match rate above.
    pinnacle_capture = await pinnacle_archive_capture_by_sport(session)
    report["archive_capture"] = pinnacle_capture
    # Betfair Exchange coverage alongside Pinnacle. The INLINE (canonical-event)
    # reading is CANONICAL: of our scraped fixtures with soft odds, the share also
    # carrying an inline ``bookmaker='Betfair Exchange'`` row (OddsPortal bookie 44,
    # JSON feed) — the REAL anchor that feeds picks (the value engine recognises it as
    # sharp via SHARP_BOOKS name matching). BOTH the per-sport panel (``betfair_capture``)
    # AND the headline now read this same INLINE instrument so the panel matches the
    # headline. The archive (``betfair:`` namespace) capture path — gated behind
    # BETFAIR_EXCHANGE_ENABLED (default OFF) and near-zero since the inline-bind
    # (commit 882bb42) — is kept ONLY as a SEPARATE diagnostic (``betfair_archive_capture``),
    # never the panel source.
    betfair_inline_capture = await betfair_inline_capture_by_sport(session)
    report["betfair_capture"] = betfair_inline_capture
    report["betfair_inline_capture"] = betfair_inline_capture
    report["betfair_archive_capture"] = await betfair_archive_capture_by_sport(session)
    # Scraped-weighted "Betfair X% · Pinnacle Y%" headline — the always-populated
    # summary the dashboard's coverage-panel HEADER shows up front (replaces the
    # bare "—"). Betfair uses the INLINE coverage (the real pick-feeding anchor),
    # NOT the near-empty archive path; Pinnacle uses the strict-matcher rate.
    report["coverage_summary"] = summarize_anchor_coverage(
        betfair_capture=betfair_inline_capture,
        pinnacle_capture=pinnacle_capture,
    ).as_dict()
    return report


@router.post("/events/{event_id}/result", dependencies=[Depends(require_dashboard_auth)])
async def settle_event(
    event_id: int,
    payload: EventResultIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    """Settle ALL open picks of an event from a user-entered final score.

    Manual settlement path (dashboard settle button) — records outcomes
    only; nothing here can place a bet.
    """
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    # Finalize the snapshot close for the picks we settle (audit #4) so a manual
    # settle, like the auto path, enters the sharp-CLV subset. Resolve the devig
    # the same way run_settlement_cycle does.
    from app.config import get_settings
    from app.config import value_policy as build_value_policy
    from app.probabilities.devig import DevigMethod

    settings = get_settings()
    devig = (
        DevigMethod(settings.value_devig)
        if settings.pick_strategy == "value"
        else DevigMethod.POWER
    )
    settled, skipped = await settle_event_picks(
        session,
        event_id,
        payload.home_score,
        payload.away_score,
        datetime.now(tz=UTC),
        devig_method=devig,
        use_pinnacle_archive=settings.clv_use_pinnacle_archive,
        use_betfair_exchange=settings.clv_use_betfair_exchange,
        value_policy=build_value_policy(settings),
    )
    await session.commit()
    return {"settled": settled, "skipped": skipped}


@router.post(
    "/picks/{pick_id}/result",
    status_code=201,
    dependencies=[Depends(require_dashboard_auth)],
)
async def record_result(
    pick_id: int,
    payload: ResultIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    pick = await session.get(Pick, pick_id)
    if pick is None:
        raise HTTPException(status_code=404, detail="pick not found")

    pnl: Decimal | None = None
    roi: Decimal | None = None
    if payload.bet_placed and payload.actual_stake is not None:
        # Canonical settlement math (audit #2): the old inline branches paid 0 for
        # HALF_WON/HALF_LOST (Asian quarter lines) and used unquantized float odds.
        # pick_pnl/pick_roi handle every outcome with Decimal money.
        odds = (
            Decimal(str(payload.actual_odds))
            if payload.actual_odds is not None
            else pick.decimal_odds
        )
        pnl = pick_pnl(payload.outcome, payload.actual_stake, odds)
        roi = pick_roi(pnl, payload.actual_stake)

    # ManualBetLog is append-only audit history (audit #10): a correction/re-post
    # intentionally appends a new row (no unique key); the settlement reader takes
    # the LATEST row per pick_id. Only ResultTracking below is upserted to a single
    # current row — that is what the "idempotent" note refers to.
    await session.execute(
        insert(ManualBetLog).values(
            pick_id=pick_id,
            bet_placed=payload.bet_placed,
            actual_stake=payload.actual_stake,
            actual_odds=payload.actual_odds,
            bookmaker_used=payload.bookmaker_used,
            notes=payload.notes,
        )
    )
    # Idempotent (ResultTracking only): re-posting a result (a correction or a
    # duplicate submit) must UPDATE the existing row, not 500 on the unique
    # (pick_id) constraint.
    result_stmt = pg_insert(ResultTracking).values(
        pick_id=pick_id,
        outcome=str(payload.outcome),
        pnl=pnl,
        roi=roi,
        settled_at=payload.settled_at,
    )
    result_stmt = result_stmt.on_conflict_do_update(
        constraint="uq_result_tracking_pick",
        set_={
            "outcome": result_stmt.excluded.outcome,
            "pnl": result_stmt.excluded.pnl,
            "roi": result_stmt.excluded.roi,
            "settled_at": result_stmt.excluded.settled_at,
        },
    )
    await session.execute(result_stmt)
    # Flip status on the OBJECT (not a bulk update) so finalize sees it settled.
    pick.status = "settled"
    event_id = pick.event_id
    # The user's manual result (ManualBetLog + ResultTracking + status) is
    # authoritative — commit it FIRST so a transient error in the OPTIONAL
    # snapshot-close enrichment below can never roll it back (audit #9).
    await session.commit()
    # audit #4: logging a result settles the pick, removing it from the auto-settle
    # cycle — so without finalizing the snapshot close here, a pick the user logs
    # BEFORE the cycle runs would never enter the sharp-CLV subset. Best-effort:
    # any error is logged (type only — secret hygiene) and the recorded result stands.
    try:
        event = await session.get(Event, event_id)
        fresh_pick = await session.get(Pick, pick_id)
        if event is not None and event.starts_at is not None and fresh_pick is not None:
            from app.clv_trueup import finalize_closing_from_snapshots
            from app.config import get_settings
            from app.config import value_policy as build_value_policy
            from app.probabilities.devig import DevigMethod

            settings = get_settings()
            devig = (
                DevigMethod(settings.value_devig)
                if settings.pick_strategy == "value"
                else DevigMethod.POWER
            )
            await finalize_closing_from_snapshots(
                session,
                fresh_pick,
                event.external_ref,
                event.starts_at,
                devig,
                use_pinnacle_archive=settings.clv_use_pinnacle_archive,
                use_betfair_exchange=settings.clv_use_betfair_exchange,
                value_policy=build_value_policy(settings),
            )
            await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning("record_result: snapshot-close finalize skipped: %s", type(exc).__name__)
    return {"status": "recorded", "outcome": str(payload.outcome)}
