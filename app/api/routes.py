"""API routes: latest picks, manual result tracking, health.

POST /picks/{id}/result is the MANUAL result-tracking entrypoint — the user
records what THEY did (bet placed or not, stake, outcome). Nothing here can
place a bet.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    MAX_PASSWORD_BYTES,
    MAX_USERNAME_BYTES,
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
from app.storage.models import Event, ManualBetLog, MatchReviewQueue, Pick, ResultTracking
from app.storage.repositories import (
    bankroll_ledger_report,
    bet_band_observations,
    betfair_archive_capture_by_sport,
    betfair_inline_capture_by_sport,
    betfair_staleness_metrics,
    create_dashboard_credentials,
    latest_available_games_with_events,
    latest_picks_with_events,
    live_evidence_rows,
    match_ceiling_decomposition,
    performance_report,
    pinnacle_archive_capture_by_sport,
    review_queue_rows,
    shadow_match_rate_outcomes,
    sharp_close_capture_density,
    sharp_slate_coverage,
    source_link_metrics,
    sport_market_promotion_distance,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_BIGINT_MAX = 9_223_372_036_854_775_807

# Self-contained dashboard page (no build step, no CDN — works offline and
# identically on the Ubuntu VPS). Data is fetched from /picks client-side.
_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

# Bundled dark login page (no CDN/JS libs). Posts JSON to /login; on
# success redirects to /. No credential is ever embedded here, and the error
# message is set via textContent (never innerHTML) so a server string can't
# inject markup.
_AUTH_TEMPLATE_DIR = Path(__file__).with_name("auth_templates")
_LOGIN_HTML = (_AUTH_TEMPLATE_DIR / "login.html").read_text(encoding="utf-8")


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
_PWA_MANIFEST = '{"name":"sharp-ev-picks","short_name":"sharp-ev","description":"+EV picks decision-support. You review and place every bet yourself.","start_url":"/","scope":"/","display":"standalone","orientation":"portrait-primary","background_color":"#0a0a0a","theme_color":"#0a0a0a","icons":[{"src":"data:image/svg+xml,%3Csvg%20xmlns=\'http://www.w3.org/2000/svg\'%20viewBox=\'0%200%20192%20192\'%3E%3Crect%20width=\'192\'%20height=\'192\'%20rx=\'42\'%20fill=\'%230a0a0a\'/%3E%3Cpolyline%20points=\'44,140%2082,110%20114,78%20146,44\'%20fill=\'none\'%20stroke=\'%2310b981\'%20stroke-width=\'11\'%20stroke-linecap=\'round\'%20stroke-linejoin=\'round\'/%3E%3Ccircle%20cx=\'146\'%20cy=\'44\'%20r=\'10\'%20fill=\'%2310b981\'/%3E%3C/svg%3E","sizes":"192x192","type":"image/svg+xml","purpose":"any"},{"src":"data:image/svg+xml,%3Csvg%20xmlns=\'http://www.w3.org/2000/svg\'%20viewBox=\'0%200%20512%20512\'%3E%3Crect%20width=\'512\'%20height=\'512\'%20fill=\'%230a0a0a\'/%3E%3Cpolyline%20points=\'128,368%20214,300%20296,216%20384,128\'%20fill=\'none\'%20stroke=\'%2310b981\'%20stroke-width=\'30\'%20stroke-linecap=\'round\'%20stroke-linejoin=\'round\'/%3E%3Ccircle%20cx=\'384\'%20cy=\'128\'%20r=\'26\'%20fill=\'%2310b981\'/%3E%3C/svg%3E","sizes":"512x512","type":"image/svg+xml","purpose":"maskable"}]}'
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
    username: str = Field(min_length=1, max_length=MAX_USERNAME_BYTES)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)


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
_LOGIN_MAX_INFLIGHT = 4  # bound concurrent PBKDF2 workers process-wide

#: ip -> (window_start monotonic seconds, failures in window)
_login_failures: dict[str, tuple[float, int]] = {}
_login_inflight: set[str] = set()


def reset_login_throttle() -> None:
    """Clear all throttle state (tests)."""
    _login_failures.clear()
    _login_inflight.clear()


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
    # Singleflight per peer and a small process-wide ceiling close the race in
    # the fixed-window limiter: without this, a burst could start many 600k-
    # iteration hashes before the first failure had a chance to increment the
    # counter. The direct peer is deliberate (see _client_ip).
    if ip in _login_inflight or len(_login_inflight) >= _LOGIN_MAX_INFLIGHT:
        return JSONResponse(
            {"detail": "another sign-in attempt is already running"},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    _login_inflight.add(ip)
    # authenticate() runs a 600k-iteration PBKDF2 hash — offload it to a worker
    # thread so a burst of login attempts can't block the event loop (and with
    # it every other request + the scheduler) until the hashes finish.
    auth_task = asyncio.create_task(
        asyncio.to_thread(authenticate, payload.username, payload.password)
    )
    try:
        authenticated = await asyncio.shield(auth_task)
    except asyncio.CancelledError:
        # A cancelled HTTP request cannot abandon a still-running PBKDF2 worker
        # and free its slot early. Repeated cancellation must not cancel our
        # own wait either: shield until the worker is actually finished, then
        # consume any worker exception and propagate request cancellation.
        while not auth_task.done():
            try:
                await asyncio.shield(auth_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if auth_task.done() and not auth_task.cancelled():
            auth_task.exception()
        raise
    finally:
        _login_inflight.discard(ip)
    if not authenticated:
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
        secure=settings.secure_session_cookie,
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
_SETUP_HTML = (_AUTH_TEMPLATE_DIR / "setup.html").read_text(encoding="utf-8")

_MIN_PASSWORD_LEN = 8


class _SetupIn(BaseModel):
    username: str = Field(max_length=MAX_USERNAME_BYTES)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_BYTES)


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
    if request.client is None:
        return False
    peer = request.client.host.strip().strip("[]").lower()
    if not (peer in ("localhost", "::1", "::ffff:127.0.0.1") or peer.startswith("127.")):
        return False

    # DNS-rebinding defence: a browser can reach 127.0.0.1 with an attacker-
    # controlled Host while the TCP peer remains loopback. Only literal local
    # authorities may serve setup, and any supplied Origin must be same-origin.
    host_header = request.headers.get("host", "")
    try:
        host_url = urlsplit(f"//{host_header}")
        host_port = host_url.port
    except ValueError:
        return False
    if host_url.username is not None or host_url.password is not None:
        return False
    hostname = (host_url.hostname or "").lower()
    if not (hostname in ("localhost", "::1", "::ffff:127.0.0.1") or hostname.startswith("127.")):
        return False
    origin = request.headers.get("origin")
    if origin:
        try:
            origin_url = urlsplit(origin)
            origin_port = origin_url.port
        except ValueError:
            return False
        default_port = 443 if request.url.scheme == "https" else 80
        request_authority = (request.url.scheme, hostname, host_port or default_port)
        origin_authority = (
            origin_url.scheme,
            (origin_url.hostname or "").lower(),
            origin_port or (443 if origin_url.scheme == "https" else 80),
        )
        if origin_authority != request_authority:
            return False
    return True


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_form(request: Request) -> Response:
    from app.config import get_settings

    # WP7 fix 5: first-run credential claim only over direct loopback — a
    # reverse-proxied (public) visitor must never even learn /setup exists.
    settings = get_settings()
    if settings.is_production or not _setup_request_is_local(request):
        return JSONResponse({"detail": "not found"}, status_code=404)
    # /setup exists ONLY while auth is enabled and no credential is set yet.
    # Once configured it disappears — changing the password later must go
    # through an authenticated path, never this unauthenticated endpoint.
    if not settings.dashboard_auth_enabled or auth_is_configured():
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
    if settings.is_production or not _setup_request_is_local(request):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not settings.dashboard_auth_enabled:
        return JSONResponse({"detail": "auth is disabled"}, status_code=404)
    if auth_is_configured():
        return JSONResponse({"detail": "already configured"}, status_code=409)
    username = payload.username.strip() or "admin"
    if len(username.encode("utf-8")) > MAX_USERNAME_BYTES:
        return JSONResponse({"detail": "username is too long"}, status_code=400)
    if len(payload.password) < _MIN_PASSWORD_LEN:
        return JSONResponse(
            {"detail": f"password must be at least {_MIN_PASSWORD_LEN} characters"},
            status_code=400,
        )
    if len(payload.password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return JSONResponse({"detail": "password is too long"}, status_code=400)
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
        secure=settings.secure_session_cookie,
    )


#: P0-3 /health liveness ceiling: the newest recorded poll must have FINISHED
#: within HEALTH_MAX_POLL_AGE_MULTIPLIER x poll_interval_seconds, else the engine
#: is judged starved/dead (HTTP 503). Named here rather than env (config.py is
#: owned elsewhere) — a one-line promotion to Settings if it ever needs tuning.
HEALTH_MAX_POLL_AGE_MULTIPLIER = 3
HEALTH_FALLBACK_CYCLE_BUDGET_SECONDS = 900
HEALTH_MAX_CYCLE_BUDGET_SECONDS = 7200
HEALTH_MAX_EXPECTED_SPORTS = 16


def _parse_poll_finish(raw: Any) -> datetime | None:
    """Parse a cycle timestamp as an aware UTC datetime, or fail closed."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _newest_poll_finish(polls: Mapping[str, Mapping[str, Any]]) -> datetime | None:
    """Most-recent valid ``finished_at`` across all recorded poll cycles."""
    newest: datetime | None = None
    for poll in polls.values():
        parsed = _parse_poll_finish(poll.get("finished_at"))
        if parsed is not None and (newest is None or parsed > newest):
            newest = parsed
    return newest


def _bounded_cycle_budget(poll_interval_seconds: int, cycle_timeout_seconds: int) -> float:
    """Finite per-sport health budget, including watchdog-disabled configs."""
    configured = float(cycle_timeout_seconds)
    if configured <= 0.0:
        configured = max(
            float(HEALTH_FALLBACK_CYCLE_BUDGET_SECONDS),
            float(HEALTH_MAX_POLL_AGE_MULTIPLIER * poll_interval_seconds),
        )
    return min(configured, float(HEALTH_MAX_CYCLE_BUDGET_SECONDS))


def _poll_freshness_ceiling(
    poll_interval_seconds: int,
    expected_sport_count: int,
    cycle_timeout_seconds: int,
) -> float:
    """Maximum completed-poll age for one full sequential sport sweep."""
    sport_count = min(max(1, expected_sport_count), HEALTH_MAX_EXPECTED_SPORTS)
    sweep_budget = sport_count * _bounded_cycle_budget(poll_interval_seconds, cycle_timeout_seconds)
    return max(float(HEALTH_MAX_POLL_AGE_MULTIPLIER * poll_interval_seconds), sweep_budget)


def _poll_health(
    polls: Mapping[str, Mapping[str, Any]],
    now: datetime,
    poll_interval_seconds: int,
    max_age_multiplier: int = HEALTH_MAX_POLL_AGE_MULTIPLIER,
    *,
    expected_sport_count: int = 1,
    cycle_timeout_seconds: int = HEALTH_FALLBACK_CYCLE_BUDGET_SECONDS,
) -> tuple[str, int, float | None]:
    """Liveness from poll FRESHNESS, not pick count — a quiet slate that still
    completes cycles is healthy; a stale newest-cycle means a starved/dead engine.

    Returns (status, http_status, newest_poll_age_seconds):
    - No recorded cycle at all -> ok/200 (cold start / router-only test app).
    - Every recorded cycle has a valid, recent, non-degraded finish -> ok/200.
    - Any missing/invalid, stale, future-skewed, or degraded cycle -> degraded/503.
    """
    if not polls:
        return "ok", 200, None
    newest = _newest_poll_finish(polls)
    ceiling = max(
        float(max_age_multiplier * poll_interval_seconds),
        _poll_freshness_ceiling(
            poll_interval_seconds,
            expected_sport_count,
            cycle_timeout_seconds,
        ),
    )
    active_budget = _bounded_cycle_budget(poll_interval_seconds, cycle_timeout_seconds)
    degraded_cycle = False
    active_cycle = False
    for poll in polls.values():
        if poll.get("in_progress") is True:
            active_cycle = True
            started = _parse_poll_finish(poll.get("started_at"))
            if started is None:
                degraded_cycle = True
                continue
            active_age = (now - started).total_seconds()
            if (
                active_age < -60.0
                or active_age > active_budget
                or bool(poll.get("degraded"))
                or poll.get("state") == "failed"
            ):
                degraded_cycle = True
            # A valid in-progress heartbeat supersedes its previous completed
            # timestamp for this sport; the full-sweep ceiling covers siblings.
            continue
        finished = _parse_poll_finish(poll.get("finished_at"))
        if finished is None:
            degraded_cycle = True
            continue
        age = (now - finished).total_seconds()
        if age < -60.0 or age > ceiling or bool(poll.get("degraded")):
            degraded_cycle = True
    if newest is None:
        if active_cycle and not degraded_cycle:
            return "ok", 200, None
        return "degraded", 503, None
    age = (now - newest).total_seconds()
    if degraded_cycle:
        return "degraded", 503, age
    return "ok", 200, age


_READINESS_CACHE_TTL_SECONDS = 5.0
_READINESS_CACHE: dict[int, tuple[float, dict[str, bool]]] = {}
_READINESS_LOCKS: dict[int, asyncio.Lock] = {}


async def _readiness_checks(
    request: Request, poll_status: str, *, force: bool = False
) -> dict[str, bool]:
    """Small-TTL/singleflight wrapper around bounded dependency probes."""
    app_key = id(request.app)
    cached = _READINESS_CACHE.get(app_key)
    now_mono = time.monotonic()
    if not force and cached is not None and now_mono - cached[0] < _READINESS_CACHE_TTL_SECONDS:
        return dict(cached[1])
    lock = _READINESS_LOCKS.setdefault(app_key, asyncio.Lock())
    async with lock:
        cached = _READINESS_CACHE.get(app_key)
        now_mono = time.monotonic()
        if not force and cached is not None and now_mono - cached[0] < _READINESS_CACHE_TTL_SECONDS:
            return dict(cached[1])
        checks = await _probe_readiness(request, poll_status)
        _READINESS_CACHE[app_key] = (time.monotonic(), checks)
        if len(_READINESS_CACHE) > 16:
            oldest = min(_READINESS_CACHE, key=lambda key: _READINESS_CACHE[key][0])
            _READINESS_CACHE.pop(oldest, None)
            _READINESS_LOCKS.pop(oldest, None)
        return dict(checks)


async def _probe_readiness(request: Request, poll_status: str) -> dict[str, bool]:
    """Run DB/Redis/scheduler/exposure checks with bounded network waits."""
    checks = {
        "exposure_seeded": bool(getattr(request.app.state, "exposure_seeded", False)),
        "scheduler": bool(getattr(getattr(request.app.state, "scheduler", None), "running", False)),
        "database": False,
        "redis": False,
        "polls": poll_status == "ok",
    }
    expected = set(getattr(request.app.state, "expected_poll_sports", ()) or ())
    if expected:
        from app.pipeline import LAST_POLL

        checks["polls"] = checks["polls"] and expected.issubset(LAST_POLL)

    factory = getattr(request.app.state, "session_factory", None)
    if factory is not None:
        try:
            from sqlalchemy import text as sql_text

            async def _db_ping() -> None:
                async with factory() as session:
                    await session.execute(sql_text("SELECT 1"))

            await asyncio.wait_for(_db_ping(), timeout=2.0)
            checks["database"] = True
        except Exception as exc:
            logger.debug("readiness database probe failed: %s", type(exc).__name__)
    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        with contextlib.suppress(Exception):
            checks["redis"] = bool(await asyncio.wait_for(redis.ping(), timeout=2.0))
    return checks


@router.get("/live")
async def live() -> dict[str, str]:
    """Process liveness only: if the event loop serves this, the process is up."""
    return {"status": "ok", "mode": "picks-only"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict[str, Any]:
    from app.config import get_settings
    from app.pipeline import LAST_POLL

    settings = get_settings()
    expected = tuple(getattr(request.app.state, "expected_poll_sports", ()) or ())
    expected_count = max(len(expected), len(LAST_POLL), 1)
    status, _, newest_age = _poll_health(
        LAST_POLL,
        datetime.now(tz=UTC),
        settings.poll_interval_seconds,
        expected_sport_count=expected_count,
        cycle_timeout_seconds=settings.poll_cycle_timeout_seconds,
    )
    checks = await _readiness_checks(request, status)
    is_ready = all(checks.values())
    response.status_code = 200 if is_ready else 503
    payload: dict[str, Any] = {
        "status": "ready" if is_ready else "not_ready",
        "mode": "picks-only",
    }
    # Keep the readiness HTTP code public for orchestrators without disclosing
    # which dependency is down (or whether a scheduler/exposure guard exists).
    # Local auth-disabled operation remains detailed, matching /health.
    if is_authenticated(request):
        payload["checks"] = checks
        payload["newest_poll_age_seconds"] = newest_age
    return payload


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    from app.config import get_settings
    from app.ingestion.proxy_health import get_registry as _get_proxy_registry
    from app.maintenance.upstream_watch import LAST_CHECK
    from app.pipeline import LAST_POLL
    from app.storage.repositories import resolver_quarantine_stats

    settings = get_settings()
    # P0-3: real liveness — a process that is up but whose poll cycles stopped
    # finishing (starved/dead scraper) now reads degraded/503 instead of a
    # hardcoded "ok"/200. Based on poll freshness, never pick count.
    expected = tuple(getattr(request.app.state, "expected_poll_sports", ()) or ())
    expected_count = max(len(expected), len(LAST_POLL), 1)
    poll_max_age = _poll_freshness_ceiling(
        settings.poll_interval_seconds,
        expected_count,
        settings.poll_cycle_timeout_seconds,
    )
    status, http_status, newest_age = _poll_health(
        LAST_POLL,
        datetime.now(tz=UTC),
        settings.poll_interval_seconds,
        expected_sport_count=expected_count,
        cycle_timeout_seconds=settings.poll_cycle_timeout_seconds,
    )
    # Router-only test apps do not install lifespan state. In the real app,
    # dependency/readiness failures turn aggregate health red even if one poll
    # happens to be fresh.
    readiness: dict[str, bool] | None = None
    if hasattr(request.app.state, "scheduler"):
        readiness = await _readiness_checks(request, status)
        if not all(readiness.values()):
            status, http_status = "degraded", 503
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
        "readiness": readiness,
        "upstream": LAST_CHECK,
        "polls": LAST_POLL,
        # Newest cycle's age + the staleness ceiling the dead-engine check uses
        # (N x poll_interval). status flips to "degraded" (503) when the age
        # exceeds it. None age == no cycle recorded yet (still "ok").
        "newest_poll_age_seconds": newest_age,
        "poll_max_age_seconds": poll_max_age,
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
        # Resolver quarantine counters (monitor-only): how many pinnacle-close
        # attachments the league-marker veto / same-pair ambiguity guards
        # refused SINCE PROCESS START ("since" carries that instant; the
        # counters are in-memory and reset on restart). Operator visibility
        # for the fail-closed refusal volume — nothing reads these to gate.
        "resolver_quarantine": resolver_quarantine_stats(),
        # Proxy-pool health for the Diagnostics tile — REDACTED (indices,
        # counters, exception class names; never a URL/IP/credential) and
        # process-local (no DB), so it is cheap here. It ALSO rides
        # /resolution/match-rate, but that endpoint is slow enough on live
        # (10s+ under scrape load) to hit the dashboard's fetch abort — the
        # tile reads THIS eager, every-cycle payload instead (2026-07-03).
        # Active data provenance for the Sources view: where ODDS come from and
        # where the BETFAIR anchor comes from under the current ODDS_SOURCE.
        "odds_source": settings.odds_source,
        "betfair_source": (
            "oddschecker — inline Betfair Exchange + Sportsbook"
            if settings.odds_source == "oddschecker"
            else (
                "oddsportal — inline Betfair"
                + (" + dedicated Exchange capture" if settings.betfair_exchange_enabled else "")
                if settings.odds_source == "oddsportal"
                else "none (The Odds API has no Betfair)"
            )
        ),
        "proxy_pool": _get_proxy_registry().diagnostics(
            configured=len(settings.scraper_proxies()),
            # Headroom vs the ACTIVE source's fetch concurrency (see the other
            # diagnostics call site) — OddsChecker uses oddschecker_max_clients.
            concurrency_floor=(
                settings.oddschecker_max_clients
                if settings.odds_source == "oddschecker"
                else settings.oddsportal_concurrency
            ),
        ),
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


def _row_structural_sane(row: dict[str, Any]) -> bool:
    """FIX 1 display defense-in-depth: whether a stored /picks row's headline
    numbers are internally consistent, so a stored-impossible pick can NEVER
    render star-rated on the dashboard even if it slipped past the mint gate.

    Impossible (returns False) when EITHER:
      * the offered ``decimal_odds`` (the ENTRY price) sits below the floor
        recomputed from the row's OWN entry fair (``model_probability``) and its
        tier ``edge_floor`` (the "MIN ACCEPTABLE 2.06 > OFFERED 1.67" symptom).
        The row's ``min_acceptable_odds`` field is deliberately NOT used here:
        it reasons against the LIVE fair (closing_fair_probability once a
        re-price exists), so comparing the ENTRY price against it falsely
        flagged exactly the picks the market moved TOWARD — positive-CLV picks
        lost their rating (audit 2026-07-10). Structural sanity must compare
        like-for-like: entry price vs entry-fair floor; or
      * the fair odds (1 / ``model_probability`` — the sharp fair on value picks)
        are at or above the offered price while a POSITIVE edge is claimed (an
        inverted fair/offered pair — no real edge).
    Missing/degenerate fields => sane (True): absence is never a violation."""
    from app.edge.value import min_acceptable_odds

    offered = _coerce_float(row.get("decimal_odds"))
    if offered is None or offered <= 1.0:
        return True
    entry_fair = _coerce_float(row.get("model_probability"))
    floor_edge = _coerce_float(row.get("edge_floor"))
    if entry_fair is not None and 0.0 < entry_fair < 1.0 and floor_edge is not None:
        try:
            entry_floor = min_acceptable_odds(
                entry_fair, floor_edge, book=str(row.get("bookmaker") or "")
            )
        except ValueError:
            entry_floor = None
        # epsilon absorbs float noise at the exact mint boundary (a pick minted
        # AT the floor is sane, not impossible).
        if entry_floor is not None and offered < entry_floor - 1e-9:
            return False
    edge = _coerce_float(row.get("current_edge"))
    if edge is None:
        edge = _coerce_float(row.get("edge")) or 0.0
    fair_prob = _coerce_float(row.get("model_probability"))
    # inverted fair/offered pair: fair odds (1/fair_prob) at/above the offered
    # price while a positive edge is claimed => structurally impossible.
    return not (
        fair_prob is not None
        and 0.0 < fair_prob < 1.0
        and edge > 0.0
        and 1.0 / fair_prob >= offered
    )


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
        # FIX 1 display defense-in-depth: a stored-impossible row (offered below
        # its own min-acceptable, or an inverted fair/offered pair) is flagged so
        # the dashboard can refuse to render it star-rated.
        row["structural_sane"] = _row_structural_sane(row)
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
    sport: Annotated[
        str | None,
        Query(pattern="^(soccer|basketball|tennis|american_football)$"),
    ] = None,
) -> list[dict[str, Any]]:
    """Latest unrestricted football/NBA fixture list from odds ingestion.

    This is a read-only visibility feed. It does not apply edge, odds-age,
    exposure, tier, or pick-status gates; those remain exclusive to /picks.
    """
    from app.pipeline import AVAILABLE_GAMES

    memory_rows: list[dict[str, Any]] = []
    for sport_key in sorted(AVAILABLE_GAMES):
        if sport is not None and sport_key != sport and not sport_key.startswith(f"{sport}_"):
            continue
        memory_rows.extend(AVAILABLE_GAMES[sport_key])

    # A restart repopulates one poll family at a time. Falling back only while
    # the entire registry was empty made every not-yet-polled sport disappear
    # as soon as the first family completed. Always merge durable coverage;
    # live memory wins for the same canonical (sport, event_id) fixture.
    warehouse_rows = await _warehouse_available_games(request, limit=limit, sport=sport)
    merged: dict[tuple[str, str], dict[str, Any]] = {
        (str(row.get("sport", "")), str(row.get("event_id", ""))): row for row in warehouse_rows
    }
    for row in memory_rows:
        merged[(str(row.get("sport", "")), str(row.get("event_id", "")))] = row
    rows = list(merged.values())
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
    from app.config import get_settings

    report = await performance_report(
        session, close_coverage_sla=get_settings().value_close_coverage_sla
    )
    rows = await live_evidence_rows(session)
    report["live_evidence"] = live_evidence_report(rows, ml_threshold=_ml_operating_point())
    # P1-1 claimed-fair RELIABILITY MONITOR (report-only — NOT a release gate,
    # NOT a recalibration haircut): does model_probability match the realized
    # win-rate in the odds band actually bet? Surfaced beside ROI/CLV so a
    # calibration drift is visible; its own insufficient-n honesty gate applies.
    band_obs = await bet_band_observations(session)
    report["calibration"] = bet_band_reliability(band_obs)
    return report


@router.get("/bankroll", dependencies=[Depends(require_dashboard_auth)])
async def bankroll_ledger(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """HYPOTHETICAL bankroll ledger (A8): manual starting balance + running
    settled P&L, with current balance and max drawdown — feeds the B7 chart.
    Informational ONLY: no money movement, never an input to live staking.
    Read-only; serves the empty inactive shape on a pre-migration DB or while
    BANKROLL_STARTING_BALANCE is unset (the shipped default)."""
    return await bankroll_ledger_report(session)


# Server-side TTL cache for the expensive multi-metric match-rate report (~12-20s
# to compute; output is identical within the window). Keyed by ``days`` with a
# short TTL so the Radar/Sources panels are instant after the first computation
# without staleness risk (the frontend already tolerates 5-min-old data).
# Read-only, per-process.
_MATCH_RATE_CACHE: dict[int | None, tuple[float, dict[str, Any]]] = {}
_MATCH_RATE_CACHE_TTL_S = 60.0
_MATCH_RATE_INFLIGHT: dict[int | None, asyncio.Task[dict[str, Any]]] = {}


async def _compute_resolution_match_rate(
    request: Request,
    session: AsyncSession | None,
    days: int | None,
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
    from app.config import get_settings as _get_settings

    _s = _get_settings()
    _staleness_ttl = float(_s.value_betfair_staleness_verdict_ttl_seconds)
    # Audit 2026-07-09: the 7 report queries are independent — run them
    # CONCURRENTLY, each on its own session (one AsyncSession cannot multiplex
    # statements), so a cold compute costs the slowest query, not the serial
    # sum (~12-20s). Without a session factory (router-only test apps, no
    # lifespan) fall back to the original sequential path on the injected
    # session.
    factory = getattr(request.app.state, "session_factory", None)

    async def _own_session(fn: Any, /, **kwargs: Any) -> Any:
        async with factory() as own:  # type: ignore[misc]
            return await fn(own, **kwargs)

    if factory is not None:
        (
            outcomes,
            pinnacle_capture,
            betfair_inline_capture,
            betfair_archive_capture,
            link_metrics,
            close_density,
            staleness_metrics,
            slate_coverage,
        ) = await asyncio.gather(
            _own_session(shadow_match_rate_outcomes, since=since),
            _own_session(pinnacle_archive_capture_by_sport),
            _own_session(betfair_inline_capture_by_sport),
            _own_session(betfair_archive_capture_by_sport),
            _own_session(source_link_metrics),
            _own_session(sharp_close_capture_density),
            _own_session(betfair_staleness_metrics, ttl_seconds=_staleness_ttl),
            _own_session(sharp_slate_coverage),
        )
    else:
        if session is None:
            raise RuntimeError("resolution match-rate requires a database session")
        outcomes = await shadow_match_rate_outcomes(session, since=since)
        pinnacle_capture = await pinnacle_archive_capture_by_sport(session)
        betfair_inline_capture = await betfair_inline_capture_by_sport(session)
        betfair_archive_capture = await betfair_archive_capture_by_sport(session)
        link_metrics = await source_link_metrics(session)
        close_density = await sharp_close_capture_density(session)
        staleness_metrics = await betfair_staleness_metrics(session, ttl_seconds=_staleness_ttl)
        slate_coverage = await sharp_slate_coverage(session)
    report = summarize_match_rate(outcomes).as_dict()
    # Per-sport upcoming capture for ALL arcadia sports (tennis + american_football
    # included), so the panel shows the archive captures every sport, not just the
    # pick sports that appear in the match rate above.
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
    report["betfair_capture"] = betfair_inline_capture
    report["betfair_inline_capture"] = betfair_inline_capture
    report["betfair_archive_capture"] = betfair_archive_capture
    # Scraped-weighted "Betfair X% · Pinnacle Y%" headline — the always-populated
    # summary the dashboard's coverage-panel HEADER shows up front (replaces the
    # bare "—"). Betfair uses the INLINE coverage (the real pick-feeding anchor),
    # NOT the near-empty archive path; Pinnacle uses the strict-matcher rate.
    report["coverage_summary"] = summarize_anchor_coverage(
        betfair_capture=betfair_inline_capture,
        pinnacle_capture=pinnacle_capture,
    ).as_dict()
    # Sharp-over-SOFT SLATE coverage (2026-07-12): the operator's actual model —
    # of the events we priced from soft books in the last 60 min, the share that
    # ALSO carry a Betfair EXCHANGE / Pinnacle sharp price, denominator SHOWN.
    # Distinct from coverage_summary (whose denominator is the dedicated capture's
    # own small fixture list and can read a confident "100%" over n=3).
    report["slate_sharp_coverage"] = slate_coverage.as_dict()
    # Cross-source LINK observability (event_source_links + match_review_queue):
    # auto-linked count, per-source averages, weak links (<0.95 confidence), and
    # the review-queue depth. Null-safe — empty tables yield zeros/empty maps.
    report["links"] = link_metrics
    # D4 capture-density panel: final-hour sharp rows per source on recently
    # kicked-off events — whether the D1 close-boost band is actually producing
    # a fresh (non-echo) sharp close. Read-only; settled-close QUALITY lives on
    # /performance ("clv_quality"), not here.
    report["close_capture_density"] = close_density
    # Betfair STALENESS-GUARD diagnostics (P3): write-time decision counts
    # (pass/demote/no_api_match/no_api_price), the fresh-vs-stale split at the
    # read TTL, median tick distance and median inline->API freshness gap —
    # the demote-rate instrument to review BEFORE flipping
    # VALUE_BETFAIR_STALENESS_SHADOW off. Null-safe: an empty (or not yet
    # migrated) verdict table yields zeros/None, never a 500.
    report["betfair_staleness"] = staleness_metrics
    # Proxy-pool health (audit 2026-07-03 §5): the shared quarantine registry,
    # REDACTED — pool indices, counters, and exception class names only; never
    # a proxy URL/IP/credential. Auth-gated with the rest of this endpoint.
    # A degraded pool slows the scrape but the 600s odds-age gate discards
    # stale candidates — the payload's fixed wording says exactly that.
    from app.ingestion.proxy_health import get_registry as _get_proxy_registry

    # Headroom is measured against the ACTIVE source's parallelism: OddsChecker
    # fetches with oddschecker_max_clients, OddsPortal with oddsportal_concurrency.
    # Using the wrong knob mislabels the "no spare proxies" headroom warning.
    _floor = (
        _s.oddschecker_max_clients if _s.odds_source == "oddschecker" else _s.oddsportal_concurrency
    )
    report["proxy_pool"] = _get_proxy_registry().diagnostics(
        configured=len(_s.scraper_proxies()),
        concurrency_floor=_floor,
    )
    return report


@router.get("/resolution/match-rate", dependencies=[Depends(require_dashboard_auth)])
async def resolution_match_rate(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> dict[str, Any]:
    """TTL cache plus per-key singleflight for the expensive diagnostic."""
    cached = _MATCH_RATE_CACHE.get(days)
    if cached is not None and time.monotonic() - cached[0] < _MATCH_RATE_CACHE_TTL_S:
        return cached[1]

    # A shared task may outlive the first request. Only singleflight when it can
    # open its own sessions from lifespan state; router-only/test apps use the
    # request-scoped session directly without spawning a detached task.
    if getattr(request.app.state, "session_factory", None) is None:
        report = await _compute_resolution_match_rate(request, session, days)
        _MATCH_RATE_CACHE[days] = (time.monotonic(), report)
        return report

    task = _MATCH_RATE_INFLIGHT.get(days)
    if task is None:
        task = asyncio.create_task(_compute_resolution_match_rate(request, None, days))
        _MATCH_RATE_INFLIGHT[days] = task

        def _finalize(done: asyncio.Task[dict[str, Any]]) -> None:
            if _MATCH_RATE_INFLIGHT.get(days) is done:
                _MATCH_RATE_INFLIGHT.pop(days, None)
            if done.cancelled():
                logger.debug("resolution match-rate background compute cancelled")
                return
            try:
                completed_report = done.result()
            except Exception as exc:
                # Retrieve every detached-task exception (request cancellation
                # may leave no waiter) without logging query/credential text.
                logger.error(
                    "resolution match-rate background compute failed: %s",
                    type(exc).__name__,
                )
                return
            # The originating request may have disconnected while the shared
            # task completed. Cache here, under task ownership, so the work is
            # not thrown away and later callers do not immediately repeat it.
            _MATCH_RATE_CACHE[days] = (time.monotonic(), completed_report)

        task.add_done_callback(_finalize)
    report = await asyncio.shield(task)
    _MATCH_RATE_CACHE[days] = (time.monotonic(), report)
    return report


def serialize_review_queue_row(
    row: MatchReviewQueue, kickoff_utc: datetime | None
) -> dict[str, Any]:
    """One match_review_queue row -> the read-only browse shape (pure; no IO).

    Event names come from the matcher's evidence_json (query vs candidate base
    forms — the same fields tools/review_queue_cli.py prints); absent evidence
    serializes as None so the dashboard renders '—', never a guess."""
    ev: Mapping[str, Any] = row.evidence_json or {}

    def _pair(prefix: str) -> str | None:
        home = ev.get(f"{prefix}_base_home")
        away = ev.get(f"{prefix}_base_away")
        return f"{home} v {away}" if home and away else None

    delta = ev.get("kickoff_delta_seconds")
    return {
        "id": row.id,
        "source": row.source,
        "source_event_id": row.source_event_id,
        "event": _pair("query"),
        "candidate": _pair("candidate"),
        "kickoff_utc": kickoff_utc.isoformat() if kickoff_utc is not None else None,
        "kickoff_delta_seconds": float(delta) if delta is not None else None,
        "confidence": float(row.confidence_score),
        "reason": row.reason,
        "review_status": row.review_status,
        "created_at": row.created_at.isoformat() if row.created_at is not None else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at is not None else None,
    }


@router.get("/resolution/review-queue", dependencies=[Depends(require_dashboard_auth)])
async def resolution_review_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """STRICTLY read-only browse of the newest match_review_queue rows — the
    borderline matcher rejects captured for human triage (observability tap,
    never a gate). No review action exists on this API: marking a row reviewed
    stays in tools/review_queue_cli.py. One SELECT, nothing written."""
    rows = await review_queue_rows(session, limit=limit)
    return {
        "limit": limit,
        "count": len(rows),
        "rows": [serialize_review_queue_row(q, kickoff) for q, kickoff in rows],
    }


@router.get("/lab/promotion-distance", dependencies=[Depends(require_dashboard_auth)])
async def lab_promotion_distance(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """B1: per-(sport, market) trusted-CLV evidence accrual vs the reporting
    'ok' floor (read-only, informational). This is the distance to the
    EVIDENCE threshold only — promotion stays gated by SportMarketClvGate and
    operator ADR sign-off; nothing here promotes or implies imminence. Point
    estimates are nulled at the source below the floor, so no consumer can
    read a sub-floor CLV number."""
    return await sport_market_promotion_distance(session)


@router.get("/resolution/match-ceiling", dependencies=[Depends(require_dashboard_auth)])
async def resolution_match_ceiling(
    session: Annotated[AsyncSession, Depends(get_session)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """B3: per-sport Pinnacle match-ceiling decomposition — structural (the
    league is not priced by Pinnacle in-window, so no match was ever possible)
    vs addressable (the matcher missed it) vs unknown-league — computed LIVE
    against the DB with the same conservative classification as
    scripts/research/sport_quality_report.py (A1), never the static research
    artifact. Read-only: a handful of SELECTs, nothing written."""
    return await match_ceiling_decomposition(session, days=days)


@router.post("/events/{event_id}/result", dependencies=[Depends(require_dashboard_auth)])
async def settle_event(
    event_id: Annotated[int, PathParam(ge=1, le=_BIGINT_MAX)],
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
        sharp_close_echo_gate=settings.clv_sharp_close_echo_gate,
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
    pick_id: Annotated[int, PathParam(ge=1, le=_BIGINT_MAX)],
    payload: ResultIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    # Audit 2026-07-09: the body's pick_id used to be silently ignored — two
    # sources of truth. The path is canonical; a mismatching body is a 422.
    try:
        body_pick_id = int(payload.pick_id)
    except ValueError:
        body_pick_id = None
    if body_pick_id != pick_id:
        raise HTTPException(status_code=422, detail="body pick_id does not match the path pick_id")
    pick = await session.get(Pick, pick_id)
    if pick is None:
        raise HTTPException(status_code=404, detail="pick not found")
    pick_created_at = pick.created_at
    if pick_created_at.tzinfo is None:
        pick_created_at = pick_created_at.replace(tzinfo=UTC)
    request_now = datetime.now(tz=UTC)
    # DB/Python clocks and timestamp precision may differ by sub-second; keep a
    # one-second ingestion tolerance while rejecting materially impossible time.
    if payload.settled_at < pick_created_at - timedelta(seconds=1):
        raise HTTPException(status_code=422, detail="settled_at predates pick creation")
    if payload.settled_at > request_now + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="settled_at is too far in the future")
    # Audit 2026-07-09: a superseded pick is a duplicate twin parked by the
    # settlement dedup guard so it never gains a ResultTracking row — the
    # /performance join counts every ResultTracking row, so settling one here
    # would double-count the bet. First settles ('alerted') and corrections
    # ('settled') stay allowed.
    if pick.status == "superseded":
        raise HTTPException(
            status_code=409,
            detail="pick is superseded (duplicate twin); settle the canonical pick instead",
        )
    # Audit 2026-07-10 L-routes-1447: apply the SAME settled-sibling dedup guard
    # as the auto settler and the manual event path (settle_event_picks). A
    # cross-source duplicate that is still 'alerted' (its twin on ANOTHER event
    # row of the same fixture already settled) would double-count the bet in
    # pnl/ROI/CLV if settled here. Park it terminally as 'superseded' and 409 —
    # fail-closed, exactly the auto pass's terminal shape. A NULL kickoff or
    # unresolvable teams cannot be dedup-matched, so the guard simply does not
    # apply (same as the auto path's NULL-starts_at filter).
    from sqlalchemy.orm import aliased

    from app.resolution.matching import fixture_pair_key
    from app.settlement.engine import (
        _effective_settlement_odds,
        _lock_settlement_instrument,
        _recommended_settlement_basis,
        _settled_sibling_exists,
    )
    from app.storage.models import Sport, Team

    _home_t, _away_t = aliased(Team), aliased(Team)
    guard_row = (
        await session.execute(
            select(Event.starts_at, Event.sport_id, Sport.key, _home_t.name, _away_t.name)
            .select_from(Event)
            .join(Sport, Event.sport_id == Sport.id)
            .join(_home_t, Event.home_team_id == _home_t.id)
            .join(_away_t, Event.away_team_id == _away_t.id)
            .where(Event.id == pick.event_id)
        )
    ).first()
    if guard_row is not None and guard_row[0] is not None:
        starts_at, sport_id, sport_key, home_name, away_name = guard_row
        pair = fixture_pair_key(home_name, away_name)
        if pair is not None:
            await _lock_settlement_instrument(
                session,
                sport_id=sport_id,
                market=pick.market,
                market_detail=pick.market_detail,
                selection=pick.selection,
                model_version_id=pick.model_version_id,
                target_pair=pair,
            )
        if pair is not None and await _settled_sibling_exists(
            session,
            pick_id=pick.id,
            event_id=pick.event_id,
            sport_id=sport_id,
            starts_at=starts_at,
            market=pick.market,
            market_detail=pick.market_detail,
            selection=pick.selection,
            model_version_id=pick.model_version_id,
            target_pair=pair,
            sport_key=sport_key,
        ):
            # Same terminal shape as the auto/manual-event passes: 'superseded'
            # writes NO result_tracking row, so it never enters pnl/ROI/CLV.
            pick.status = "superseded"
            await session.commit()
            logger.info(
                "record_result: superseded duplicate pick %d (%s %s) — a sibling of "
                "the same fixture is already settled (cross-source event dedup)",
                pick.id,
                pick.market,
                pick.selection,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "an equivalent pick on a sibling event of the same fixture is "
                    "already settled; this duplicate was parked as superseded"
                ),
            )

    stake, odds, payout_bookmaker = _recommended_settlement_basis(pick)
    if payload.bet_placed and payload.actual_stake is not None:
        # Canonical settlement math (audit #2): the old inline branches paid 0 for
        # HALF_WON/HALF_LOST (Asian quarter lines) and used unquantized float odds.
        # pick_pnl/pick_roi handle every outcome with Decimal money.
        stake = payload.actual_stake
        if payload.actual_odds is not None:
            odds = payload.actual_odds
            payout_bookmaker = payload.bookmaker_used or pick.bookmaker
        else:
            _, odds, payout_bookmaker = _recommended_settlement_basis(pick)
    pnl = pick_pnl(payload.outcome, stake, odds, bookmaker=payout_bookmaker)
    roi = pick_roi(pnl, stake)
    settled_effective_odds = _effective_settlement_odds(odds, payout_bookmaker)

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
        settled_stake_amount=stake,
        settled_effective_odds=settled_effective_odds,
        settled_at=payload.settled_at,
    )
    result_stmt = result_stmt.on_conflict_do_update(
        constraint="uq_result_tracking_pick",
        set_={
            "outcome": result_stmt.excluded.outcome,
            "pnl": result_stmt.excluded.pnl,
            "roi": result_stmt.excluded.roi,
            "settled_stake_amount": result_stmt.excluded.settled_stake_amount,
            "settled_effective_odds": result_stmt.excluded.settled_effective_odds,
            "settled_at": result_stmt.excluded.settled_at,
            # Audit 2026-07-09: the manual path never carries scores, so a
            # correction over an engine-settled row must CLEAR the previous
            # settlement's scores, not leave them beside the corrected outcome.
            "home_score": None,
            "away_score": None,
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
                sharp_close_echo_gate=settings.clv_sharp_close_echo_gate,
                value_policy=build_value_policy(settings),
            )
            await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.warning("record_result: snapshot-close finalize skipped: %s", type(exc).__name__)
    return {"status": "recorded", "outcome": str(payload.outcome)}
