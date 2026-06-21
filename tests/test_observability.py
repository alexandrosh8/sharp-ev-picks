"""SAFETY layer for the Sentry integration.

Sentry ships error data to a third party, so the ``before_send`` scrubber MUST
never let a secret or a query-stringed odds URL (whose query carries API keys)
reach Sentry, and ``init_sentry`` MUST be a no-op without a DSN. Pure-function
tests — no network, no real Sentry init.
"""

import asyncio

from app.config import Settings
from app.observability import init_sentry, scrub_event


def test_scrub_redacts_secret_query_params_in_urls() -> None:
    event = {"request": {"url": "https://api.example.com/odds?apiKey=SECRET123&x=1"}}
    out = scrub_event(event, None)
    assert out is not None
    assert "SECRET123" not in str(out)
    assert "[redacted]" in out["request"]["url"]
    assert "x=1" in out["request"]["url"]  # non-secret params survive


def test_scrub_redacts_values_by_secret_key() -> None:
    event = {"extra": {"authorization": "Bearer abc.def", "note": "ok"}}
    out = scrub_event(event, None)
    assert out is not None
    assert out["extra"]["authorization"] == "[redacted]"
    assert out["extra"]["note"] == "ok"


def test_scrub_redacts_secret_in_exception_message() -> None:
    event = {"exception": {"values": [{"value": "GET failed for token=topsecret"}]}}
    out = scrub_event(event, None)
    assert out is not None
    assert "topsecret" not in str(out)


def test_scrub_drops_benign_cancellation() -> None:
    hint = {"exc_info": (asyncio.CancelledError, asyncio.CancelledError(), None)}
    assert scrub_event({"message": "shutdown"}, hint) is None


def test_scrub_passes_clean_event_through() -> None:
    event = {"message": "something broke", "level": "error"}
    assert scrub_event(event, None) == event


def test_init_sentry_is_noop_without_dsn() -> None:
    # Locked safety flags default correctly; only the DSN is empty -> disabled.
    assert init_sentry(Settings(sentry_dsn="")) is False


# --- audit-hardening regressions (vectors the security review surfaced) -------


def test_scrub_redacts_bytes() -> None:
    out = scrub_event({"extra": {"raw": b"apiKey=DEADBEEF"}}, None)
    assert out is not None
    assert "DEADBEEF" not in str(out)


def test_scrub_redacts_embedded_cookie_and_dsn() -> None:
    out = scrub_event({"message": "cookie=sess123 dsn=https://k@o.io/1"}, None)
    assert out is not None
    assert "sess123" not in str(out)
    assert "k@o.io" not in str(out)


def test_scrub_redacts_secret_key_value_pairs_list() -> None:
    event = {"request": {"query_string": [["apiKey", "DEADBEEF"], ["regions", "eu"]]}}
    out = scrub_event(event, None)
    assert out is not None
    assert "DEADBEEF" not in str(out)
    assert "eu" in str(out)  # non-secret pair survives


def test_scrub_redacts_bare_secret_shapes() -> None:
    out = scrub_event({"message": "auth Bearer abc.def.ghijkl and sk_live_ABC123"}, None)
    assert out is not None
    assert "abc.def.ghijkl" not in str(out)
    assert "sk_live_ABC123" not in str(out)


def test_init_sentry_uses_hardened_options(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.observability as obs

    captured: dict = {}

    class _Fake:
        @staticmethod
        def init(**kw: object) -> None:
            captured.update(kw)

    monkeypatch.setattr(obs, "sentry_sdk", _Fake)
    assert obs.init_sentry(Settings(sentry_dsn="https://x@o.sentry.io/1")) is True
    assert captured["send_default_pii"] is False
    assert captured["include_local_variables"] is False
    assert captured["include_source_context"] is False  # the audit blocker
    assert captured["max_request_body_size"] == "never"
    assert captured["before_send"] is obs.scrub_event
