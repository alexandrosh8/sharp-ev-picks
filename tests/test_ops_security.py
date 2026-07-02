"""WP7 fixes 4/5 — dashboard surface hardening (no DB, no network):

- /login throttle: an in-process per-IP failure window returns 429 BEFORE the
  600k-iteration PBKDF2 runs (brute-force + CPU-DoS guard on a 2-CPU box);
- /health split: liveness stays public (compose healthcheck), but dependency
  versions / poll internals / strategy thresholds require an authenticated
  session once dashboard auth is enabled;
- /setup loopback gate: the first-run create-admin screen answers only DIRECT
  loopback connections — a reverse-proxied request (Forwarded headers) or a
  non-loopback peer gets 404, and X-Forwarded-For can never spoof access.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.testclient import TestClient

import app.api.routes as routes_mod
from app.api.auth import (
    SESSION_COOKIE,
    reset_active_credentials,
    set_active_credentials,
    sign_session,
)
from app.api.deps import get_session
from app.api.routes import reset_login_throttle, router
from app.config import Settings

_LOOPBACK = ("127.0.0.1", 50000)


async def _no_session() -> AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    reset_active_credentials()
    reset_login_throttle()
    yield
    reset_active_credentials()
    reset_login_throttle()


def _auth_enabled_settings() -> Settings:
    return Settings.model_construct(
        dashboard_auth_enabled=True,
        dashboard_auth_username="admin",
        dashboard_auth_password_hash=SecretStr(""),
        dashboard_session_secret=SecretStr(""),
        dashboard_session_ttl_seconds=12 * 60 * 60,
        app_env="local",
    )


def _make_app(monkeypatch, settings: Settings):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _no_session
    return app


# --- fix 4: /login throttle --------------------------------------------------- #


def test_login_throttled_after_max_failures_and_pbkdf2_skipped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _auth_enabled_settings()
    set_active_credentials("admin", "not-a-real-hash", "session-secret")
    app = _make_app(monkeypatch, settings)
    calls = {"n": 0}

    def fake_authenticate(username: str, password: str) -> bool:
        calls["n"] += 1
        return False

    monkeypatch.setattr(routes_mod, "authenticate", fake_authenticate)
    client = TestClient(app, client=_LOOPBACK)
    for _ in range(routes_mod.LOGIN_MAX_FAILURES):
        assert client.post("/login", json={"username": "admin", "password": "bad"}).status_code == (
            401
        )
    assert calls["n"] == routes_mod.LOGIN_MAX_FAILURES

    res = client.post("/login", json={"username": "admin", "password": "bad"})
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) >= 1
    # the throttle answered BEFORE the expensive hash ran
    assert calls["n"] == routes_mod.LOGIN_MAX_FAILURES


def test_login_success_clears_the_failure_window(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _auth_enabled_settings()
    set_active_credentials("admin", "not-a-real-hash", "session-secret")
    app = _make_app(monkeypatch, settings)
    outcomes = iter([False, False, True, False])
    monkeypatch.setattr(routes_mod, "authenticate", lambda u, p: next(outcomes))
    client = TestClient(app, client=_LOOPBACK)
    assert client.post("/login", json={"username": "admin", "password": "x"}).status_code == 401
    assert client.post("/login", json={"username": "admin", "password": "x"}).status_code == 401
    assert client.post("/login", json={"username": "admin", "password": "ok"}).status_code == 200
    # success reset the counter: the next failure is 401, not an instant 429
    assert client.post("/login", json={"username": "admin", "password": "x"}).status_code == 401


def test_login_throttle_is_per_ip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _auth_enabled_settings()
    set_active_credentials("admin", "not-a-real-hash", "session-secret")
    app = _make_app(monkeypatch, settings)
    monkeypatch.setattr(routes_mod, "authenticate", lambda u, p: False)
    hammering = TestClient(app, client=("203.0.113.7", 40000))
    for _ in range(routes_mod.LOGIN_MAX_FAILURES):
        hammering.post("/login", json={"username": "admin", "password": "bad"})
    assert hammering.post("/login", json={"username": "a", "password": "b"}).status_code == 429
    # a different source address is not collateral damage
    other = TestClient(app, client=_LOOPBACK)
    assert other.post("/login", json={"username": "admin", "password": "bad"}).status_code == 401


def test_login_throttle_window_expires(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Pure-throttle check (no HTTP): the block lifts after the window passes.
    reset_login_throttle()
    t0 = 1000.0
    for _ in range(routes_mod.LOGIN_MAX_FAILURES):
        routes_mod._login_record_failure("198.51.100.9", now=t0)
    assert routes_mod._login_retry_after("198.51.100.9", now=t0) is not None
    after = t0 + routes_mod.LOGIN_WINDOW_SECONDS + 1
    assert routes_mod._login_retry_after("198.51.100.9", now=after) is None


# --- fix 5a: /health public liveness vs authenticated detail ------------------ #

_DETAIL_KEYS = ("upstream", "polls", "value_min_edge", "poll_interval_seconds")


def test_health_hides_detail_from_anonymous_when_auth_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _auth_enabled_settings()
    set_active_credentials("admin", "not-a-real-hash", "session-secret")
    app = _make_app(monkeypatch, settings)
    body = TestClient(app, client=_LOOPBACK).get("/health").json()
    assert body["status"] in ("ok", "degraded")  # liveness stays public
    assert body["mode"] == "picks-only"
    for key in _DETAIL_KEYS:  # versions/poll internals/thresholds do NOT leak
        assert key not in body


def test_health_shows_detail_to_authenticated_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _auth_enabled_settings()
    secret = "session-secret"
    set_active_credentials("admin", "not-a-real-hash", secret)
    app = _make_app(monkeypatch, settings)
    client = TestClient(app, client=_LOOPBACK)
    client.cookies.set(SESSION_COOKIE, sign_session("admin", secret, 3600))
    body = client.get("/health").json()
    for key in _DETAIL_KEYS:
        assert key in body


def test_health_shows_detail_when_auth_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Local/dev (auth off) keeps the full payload — the dashboard needs it.
    settings = Settings.model_construct(dashboard_auth_enabled=False, app_env="local")
    app = _make_app(monkeypatch, settings)
    body = TestClient(app, client=_LOOPBACK).get("/health").json()
    for key in _DETAIL_KEYS:
        assert key in body


# --- fix 5b: /setup answers only direct loopback connections ------------------ #


def _setup_app(monkeypatch):  # type: ignore[no-untyped-def]
    settings = _auth_enabled_settings()  # auth ON, unconfigured -> first-run mode
    app = _make_app(monkeypatch, settings)
    calls: list[str] = []

    async def _fake_create(session, *, username, password_hash, session_secret):  # type: ignore[no-untyped-def]
        calls.append(username)
        return True

    monkeypatch.setattr(routes_mod, "create_dashboard_credentials", _fake_create)
    return app, calls


def test_setup_denied_for_non_loopback_peer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, calls = _setup_app(monkeypatch)
    remote = TestClient(app, client=("203.0.113.50", 40000), follow_redirects=False)
    assert remote.get("/setup").status_code == 404
    res = remote.post("/setup", json={"username": "admin", "password": "long-enough-pw"})
    assert res.status_code == 404
    assert calls == []  # the credential writer was never reached


def test_setup_denied_when_x_forwarded_for_spoofs_loopback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, calls = _setup_app(monkeypatch)
    remote = TestClient(app, client=("203.0.113.50", 40000), follow_redirects=False)
    headers = {"X-Forwarded-For": "127.0.0.1"}
    assert remote.get("/setup", headers=headers).status_code == 404
    res = remote.post(
        "/setup", json={"username": "admin", "password": "long-enough-pw"}, headers=headers
    )
    assert res.status_code == 404
    assert calls == []


def test_setup_denied_for_proxied_request_even_from_loopback_peer(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The Traefik hole: the proxy binds loopback on the host, so its upstream
    # connection LOOKS local — but a Forwarded header proves a proxied (public)
    # origin, so the first-run screen must refuse it.
    app, calls = _setup_app(monkeypatch)
    local_proxy = TestClient(app, client=_LOOPBACK, follow_redirects=False)
    headers = {"X-Forwarded-For": "198.51.100.20"}
    assert local_proxy.get("/setup", headers=headers).status_code == 404
    res = local_proxy.post(
        "/setup", json={"username": "admin", "password": "long-enough-pw"}, headers=headers
    )
    assert res.status_code == 404
    assert calls == []


def test_setup_still_served_to_direct_loopback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, calls = _setup_app(monkeypatch)
    local = TestClient(app, client=_LOOPBACK, follow_redirects=False)
    assert local.get("/setup").status_code == 200
    res = local.post("/setup", json={"username": "admin", "password": "long-enough-pw"})
    assert res.status_code == 200
    assert calls == ["admin"]
