"""Optional dashboard auth — gating behavior + the stdlib crypto primitives.

Pure: no DB, no network. A minimal app mounts the real router and the
AuthRequired handler; auth is forced ON by monkeypatching app.config.get_settings
(is_authenticated/authenticate import it at call-time, so the patch takes
effect). The session dependency is stubbed like tests/test_api.py, but
require_dashboard_auth is deliberately NOT overridden — these tests exercise
the gate itself.
"""

from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import (
    SESSION_COOKIE,
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


async def _no_session() -> AsyncIterator[None]:
    yield None


def _auth_settings() -> Settings:
    # model_construct skips validation and the real .env so the test never
    # depends on host config; auth is forced ON with a freshly hashed password
    # and a fixed session secret.
    return Settings.model_construct(
        dashboard_auth_enabled=True,
        dashboard_auth_username="admin",
        dashboard_auth_password_hash=hash_password(_TEST_PW),
        dashboard_session_secret="test-secret-key",
        dashboard_session_ttl_seconds=12 * 60 * 60,
        app_env="local",
    )


def _make_auth_app(monkeypatch) -> FastAPI:  # type: ignore[no-untyped-def]
    settings = _auth_settings()
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
    assert 'id="picks-table"' in page.text
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


def test_session_sign_verify_round_trips() -> None:
    secret = "test-secret-key"
    token = sign_session("admin", secret, ttl_seconds=3600, now=1_000_000)
    assert verify_session(token, secret, now=1_000_500) == "admin"


def test_session_rejects_tampered_token() -> None:
    secret = "test-secret-key"
    token = sign_session("admin", secret, ttl_seconds=3600, now=1_000_000)
    body, sig = token.split(".")
    tampered = body + "x." + sig  # body no longer matches the signature
    assert verify_session(tampered, secret, now=1_000_500) is None
    # a different secret also fails closed
    assert verify_session(token, "other-secret", now=1_000_500) is None


def test_session_rejects_expired_token() -> None:
    secret = "test-secret-key"
    token = sign_session("admin", secret, ttl_seconds=3600, now=1_000_000)
    # now >= issued + ttl => expired
    assert verify_session(token, secret, now=1_000_000 + 3600) is None
    assert verify_session(token, secret, now=2_000_000) is None
