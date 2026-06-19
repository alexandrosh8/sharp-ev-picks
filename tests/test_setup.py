"""First-run /setup flow — the unauthenticated, one-shot create-admin-password
screen shown only while auth is ENABLED and no credential exists yet.

The repo write is monkeypatched (real DB coverage lives in test_setup_db.py);
these pin the security-critical ROUTE behaviour: redirect-to-setup while
unconfigured, the screen disappearing once configured, the one-shot 409 guard
against an unauthenticated password reset, password-length validation, and that
the issued cookie is signed with the SAME secret auth verifies against.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.api.routes as routes_mod
from app.api.auth import (
    SESSION_COOKIE,
    auth_is_configured,
    hash_password,
    install_auth,
    reset_active_credentials,
    set_active_credentials,
)
from app.api.deps import get_session
from app.api.routes import router
from app.config import Settings

_PW = "s3cret-test-pw"  # synthetic, in-process only


async def _no_session() -> AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def _clean_active_credentials() -> Iterator[None]:
    # The in-memory credential holder is module-global; keep it from leaking
    # into (or out of) other test modules.
    reset_active_credentials()
    yield
    reset_active_credentials()


def _unconfigured_settings() -> Settings:
    # Auth ON, NO .env hash/secret -> unconfigured/first-run mode.
    return Settings.model_construct(
        dashboard_auth_enabled=True,
        dashboard_auth_username="admin",
        dashboard_auth_password_hash="",
        dashboard_session_secret="",
        dashboard_session_ttl_seconds=12 * 60 * 60,
        app_env="local",
    )


def _build_app(monkeypatch, settings: Settings, *, created_ok: bool = True):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    calls: list[tuple[str, str, str]] = []

    async def _fake_create(session, *, username, password_hash, session_secret):  # type: ignore[no-untyped-def]
        calls.append((username, password_hash, session_secret))
        return created_ok

    monkeypatch.setattr(routes_mod, "create_dashboard_credentials", _fake_create)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _no_session
    install_auth(app)
    return app, calls


def test_unconfigured_root_redirects_to_setup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, _ = _build_app(monkeypatch, _unconfigured_settings())
    client = TestClient(app, follow_redirects=False)
    res = client.get("/", headers={"accept": "text/html"})
    assert res.status_code == 303
    assert res.headers["location"] == "/setup"


def test_unconfigured_login_redirects_to_setup(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, _ = _build_app(monkeypatch, _unconfigured_settings())
    client = TestClient(app, follow_redirects=False)
    res = client.get("/login", headers={"accept": "text/html"})
    assert res.status_code == 303
    assert res.headers["location"] == "/setup"


def test_setup_page_served_only_while_unconfigured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, _ = _build_app(monkeypatch, _unconfigured_settings())
    client = TestClient(app, follow_redirects=False)
    res = client.get("/setup")
    assert res.status_code == 200
    assert 'id="setup-form"' in res.text
    assert "first run" in res.text.lower()
    # textContent-only error rendering — no innerHTML on the auth pages
    assert "innerHTML" not in res.text


def test_setup_post_creates_credential_and_signs_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, calls = _build_app(monkeypatch, _unconfigured_settings())
    client = TestClient(app, follow_redirects=False)
    res = client.post("/setup", json={"username": "admin", "password": _PW})
    assert res.status_code == 200
    assert SESSION_COOKIE in res.cookies
    # persisted exactly once, as a salted PBKDF2 hash (never the plaintext)
    assert len(calls) == 1
    username, password_hash, session_secret = calls[0]
    assert username == "admin"
    assert password_hash.startswith("pbkdf2_sha256$")
    assert _PW not in password_hash
    assert session_secret  # a generated secret, not blank
    # the app is now configured in memory
    assert auth_is_configured() is True


def test_setup_post_is_one_shot_409_when_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, calls = _build_app(monkeypatch, _unconfigured_settings())
    set_active_credentials("admin", hash_password(_PW), "an-existing-secret")
    client = TestClient(app, follow_redirects=False)
    res = client.post("/setup", json={"username": "admin", "password": "another-pw-9"})
    assert res.status_code == 409
    assert calls == []  # never reached the writer — no unauthenticated reset


def test_setup_get_redirects_home_when_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, _ = _build_app(monkeypatch, _unconfigured_settings())
    set_active_credentials("admin", hash_password(_PW), "an-existing-secret")
    client = TestClient(app, follow_redirects=False)
    res = client.get("/setup", headers={"accept": "text/html"})
    assert res.status_code == 303
    assert res.headers["location"] == "/"


def test_setup_rejects_short_password(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, calls = _build_app(monkeypatch, _unconfigured_settings())
    client = TestClient(app, follow_redirects=False)
    res = client.post("/setup", json={"username": "admin", "password": "short"})
    assert res.status_code == 400
    assert calls == []  # rejected before writing


def test_setup_404_when_auth_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = Settings.model_construct(
        dashboard_auth_enabled=False,
        dashboard_auth_username="admin",
        dashboard_auth_password_hash="",
        dashboard_session_secret="",
        dashboard_session_ttl_seconds=12 * 60 * 60,
        app_env="local",
    )
    app, calls = _build_app(monkeypatch, settings)
    client = TestClient(app, follow_redirects=False)
    res = client.post("/setup", json={"username": "admin", "password": _PW})
    assert res.status_code == 404
    assert calls == []


def test_cookie_from_setup_authenticates_dashboard(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The whole point: the cookie /setup issues is signed with the freshly
    # generated session secret (NOT the blank .env one), so it validates and the
    # operator lands on the dashboard without a second login.
    app, _ = _build_app(monkeypatch, _unconfigured_settings())
    client = TestClient(app, follow_redirects=False)
    assert client.post("/setup", json={"username": "admin", "password": _PW}).status_code == 200
    # TestClient carries the Set-Cookie forward.
    page = client.get("/", headers={"accept": "text/html"})
    assert page.status_code == 200
    assert 'id="picks-table"' in page.text
