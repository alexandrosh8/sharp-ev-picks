"""Optional dashboard auth — gating behavior + the stdlib crypto primitives.

Pure: no DB, no network. A minimal app mounts the real router and the
AuthRequired handler; auth is forced ON by monkeypatching app.config.get_settings
(is_authenticated/authenticate import it at call-time, so the patch takes
effect). The session dependency is stubbed like tests/test_api.py, but
require_dashboard_auth is deliberately NOT overridden — these tests exercise
the gate itself.
"""

import asyncio
import threading
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request

from app.api import routes
from app.api.auth import (
    SESSION_COOKIE,
    authenticate,
    hash_password,
    install_auth,
    sign_session,
    verify_password,
    verify_session,
)
from app.api.deps import get_session
from app.api.routes import router
from app.config import Settings

# A throwaway password used ONLY in-process to build a hash at runtime; no
# secret/hash is committed (the live secret lives in .env, handled separately).
_TEST_PW = "s3cret-test-pw"
_SESSION_SECRET = "test-session-secret-" + ("x" * 32)
_OTHER_SESSION_SECRET = "other-session-secret-" + ("y" * 32)


async def _no_session() -> AsyncIterator[None]:
    yield None


def _auth_settings() -> Settings:
    # model_construct skips validation and the real .env so the test never
    # depends on host config; auth is forced ON with a freshly hashed password
    # and a fixed session secret.
    return Settings.model_construct(
        dashboard_auth_enabled=True,
        dashboard_auth_username="admin",
        dashboard_auth_password_hash=SecretStr(hash_password(_TEST_PW)),
        dashboard_session_secret=SecretStr(_SESSION_SECRET),
        dashboard_session_ttl_seconds=12 * 60 * 60,
        app_env="local",
    )


def _make_auth_app(monkeypatch, *, app_env: str = "local") -> FastAPI:  # type: ignore[no-untyped-def]
    settings = _auth_settings().model_copy(update={"app_env": app_env})
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _no_session
    install_auth(app)
    return app


def test_health_is_public_even_with_auth_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_make_auth_app(monkeypatch))
    assert client.get("/health").status_code == 200


def test_dashboard_redirects_to_login_when_unauthenticated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_make_auth_app(monkeypatch), follow_redirects=False)
    res = client.get("/", headers={"accept": "text/html"})
    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_login_frontend_has_singleflight_timeout_and_bounded_retry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    page = TestClient(_make_auth_app(monkeypatch)).get("/login").text
    assert "AbortController" in page
    assert "15000" in page
    assert "Retry-After" in page
    assert "Math.min(Math.max(raw, 1), 300)" in page
    assert "if (submitBtn.disabled) return;" in page


def test_data_route_returns_401_json_when_unauthenticated(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_make_auth_app(monkeypatch), follow_redirects=False)
    res = client.get("/picks")
    assert res.status_code == 401


def test_login_rejects_wrong_password_and_accepts_right_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_make_auth_app(monkeypatch), follow_redirects=False)
    bad = client.post("/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401
    good = client.post("/login", json={"username": "admin", "password": _TEST_PW})
    assert good.status_code == 200
    assert SESSION_COOKIE in good.cookies


def test_login_cookie_secure_flag_tracks_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    local = TestClient(_make_auth_app(monkeypatch, app_env="local"), follow_redirects=False)
    local_response = local.post(
        "/login",
        json={"username": "admin", "password": _TEST_PW},
    )
    assert "secure" not in local_response.headers["set-cookie"].lower()

    production = TestClient(
        _make_auth_app(monkeypatch, app_env="production"),
        follow_redirects=False,
    )
    production_response = production.post(
        "/login",
        json={"username": "admin", "password": _TEST_PW},
    )
    assert "secure" in production_response.headers["set-cookie"].lower()


async def test_repeated_login_cancellation_keeps_singleflight_slot_until_hash_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_authenticate(username: str, password: str) -> bool:
        started.set()
        release.wait(timeout=2.0)
        return username == "admin" and password == _TEST_PW

    monkeypatch.setattr("app.config.get_settings", lambda: _auth_settings())
    monkeypatch.setattr(routes, "authenticate", blocking_authenticate)
    ip = "198.51.100.77"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [],
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    task = asyncio.create_task(
        routes.login_submit(
            routes._LoginIn(username="admin", password=_TEST_PW),
            request,
        )
    )
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert ip in routes._login_inflight
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ip not in routes._login_inflight
    finally:
        release.set()
        routes._login_inflight.discard(ip)


def test_valid_session_cookie_unlocks_dashboard_and_data(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # raise_server_exceptions=False: /picks passes the gate and reaches its
    # handler, which then hits the stubbed (None) session and 500s — we only
    # care that it is NOT 401, i.e. the auth gate let the cookie through.
    client = TestClient(
        _make_auth_app(monkeypatch),
        follow_redirects=False,
        raise_server_exceptions=False,
    )
    login = client.post("/login", json={"username": "admin", "password": _TEST_PW})
    assert login.status_code == 200
    # TestClient carries the Set-Cookie forward on subsequent requests.
    page = client.get("/", headers={"accept": "text/html"})
    assert page.status_code == 200
    # SignalDesk redesign: the Edges master-detail console lives in id="view-edges".
    assert 'id="view-edges"' in page.text
    assert client.get("/picks").status_code != 401


def test_logout_clears_cookie_and_redirects(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(_make_auth_app(monkeypatch), follow_redirects=False)
    client.post("/login", json={"username": "admin", "password": _TEST_PW})
    res = client.post("/logout")
    assert res.status_code == 303
    assert res.headers["location"] == "/login"
    # delete_cookie emits a Set-Cookie that expires bp_session.
    set_cookie = res.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookie


def test_password_hash_round_trips_and_rejects_wrong_password() -> None:
    stored = hash_password(_TEST_PW)
    assert verify_password(_TEST_PW, stored) is True
    assert verify_password("not-it", stored) is False
    # malformed stored value never crashes, just fails closed
    assert verify_password(_TEST_PW, "garbage") is False


def test_authenticate_accepts_utf8_username(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _auth_settings()
    settings.dashboard_auth_username = "διαχειριστής"
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    assert authenticate("διαχειριστής", _TEST_PW) is True
    assert authenticate("άλλος", _TEST_PW) is False


def test_session_sign_verify_round_trips() -> None:
    secret = _SESSION_SECRET
    token = sign_session("admin", secret, ttl_seconds=3600, now=1_000_000)
    assert verify_session(token, secret, now=1_000_500) == "admin"


def test_session_round_trips_delimiter_username() -> None:
    secret = _SESSION_SECRET
    username = "ops|primary|admin"
    token = sign_session(username, secret, ttl_seconds=3600, now=1_000_000)
    assert verify_session(token, secret, now=1_000_500) == username


def test_password_and_kdf_bounds_fail_closed() -> None:
    salt = "00" * 16
    digest = "00" * 32
    assert verify_password(_TEST_PW, f"pbkdf2_sha256$99999${salt}${digest}") is False
    assert verify_password(_TEST_PW, f"pbkdf2_sha256$2000001${salt}${digest}") is False
    assert verify_password(_TEST_PW, f"pbkdf2_sha256$600000$00${digest}") is False
    assert verify_password(_TEST_PW, f"pbkdf2_sha256$600000${salt}$00") is False
    assert verify_password("x" * 1025, hash_password(_TEST_PW)) is False
    with pytest.raises(ValueError, match="iteration count"):
        hash_password(_TEST_PW, iterations=99_999)
    with pytest.raises(ValueError, match="iteration count"):
        hash_password(_TEST_PW, iterations=2_000_001)


def test_session_bounds_fail_closed() -> None:
    secret = _SESSION_SECRET
    with pytest.raises(ValueError, match="username length"):
        sign_session("u" * 129, secret, ttl_seconds=3600)
    with pytest.raises(ValueError, match="secret length"):
        sign_session("admin", "too-short", ttl_seconds=3600)
    with pytest.raises(ValueError, match="TTL"):
        sign_session("admin", secret, ttl_seconds=7 * 24 * 60 * 60 + 1)
    assert verify_session("x" * 4097, secret) is None


def test_session_rejects_tampered_token() -> None:
    secret = _SESSION_SECRET
    token = sign_session("admin", secret, ttl_seconds=3600, now=1_000_000)
    body, sig = token.split(".")
    tampered = body + "x." + sig  # body no longer matches the signature
    assert verify_session(tampered, secret, now=1_000_500) is None
    # a different secret also fails closed
    assert verify_session(token, _OTHER_SESSION_SECRET, now=1_000_500) is None


def test_session_rejects_expired_token() -> None:
    secret = _SESSION_SECRET
    token = sign_session("admin", secret, ttl_seconds=3600, now=1_000_000)
    # now >= issued + ttl => expired
    assert verify_session(token, secret, now=1_000_000 + 3600) is None
    assert verify_session(token, secret, now=2_000_000) is None
