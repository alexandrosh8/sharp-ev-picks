"""Opt-in Sentry error monitoring + the ``before_send`` SECRET SCRUBBER.

Sentry is DISABLED unless ``settings.sentry_dsn`` is set (in .env only). Because
Sentry ships error data to a third party, every event passes through
``scrub_event`` first, which:

  * redacts dict values whose KEY names a secret
    (token/password/bearer/authorization/api-key/secret/dsn/cookie/session),
  * redacts ``secret=value`` pairs inside any string — URLs (incl. odds query
    strings that carry API keys) and exception messages, and
  * drops benign shutdown exceptions (CancelledError / KeyboardInterrupt).

``scrub_event`` is pure (dict -> dict | None) and unit-tested. ``init_sentry`` is
the composition-root entry point; it reads only the DSN/env/sample-rate already
parsed by ``Settings`` — no env access here (that lives in app/config.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import Settings

try:  # optional dependency — the app runs fine without it
    import sentry_sdk
except ImportError:  # pragma: no cover - exercised only when uninstalled
    sentry_sdk = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Keys whose VALUE must never leave the process (matched case-insensitively,
# anywhere in the key name).
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|password|passwd|bearer|authorization|api[_-]?key"
    r"|app[_-]?key|secret|dsn|cookie|session)"
)
# ``secret=value`` / ``secret: value`` pairs embedded in strings (URLs, messages).
_SECRET_KV_RE = re.compile(
    r"(?i)\b(token|password|passwd|bearer|authorization|api[_-]?key"
    r"|app[_-]?key|secret|dsn|cookie|session|key)\b"
    r"\s*(=|%3D|:\s*)([^&\s\"']+)"
)
# Bare secret SHAPES carrying no ``key=`` prefix — Bearer tokens, Stripe-style
# keys, JWTs — redacted whole (closes the audit's bare-value gap for these).
_SECRET_SHAPE_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|sk_(?:live|test)_[A-Za-z0-9]+"
    r"|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_REDACTED = "[redacted]"
_DROP_EXC: tuple[type[BaseException], ...] = (asyncio.CancelledError, KeyboardInterrupt)


def _scrub_str(value: str) -> str:
    value = _SECRET_SHAPE_RE.sub(_REDACTED, value)
    return _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", value)


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else _scrub(v)) for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        # ``[secret_key, value]`` pairs (e.g. query_string as pairs): redact value.
        if (
            len(obj) == 2
            and isinstance(obj[0], str)
            and isinstance(obj[1], str)
            and _SECRET_KEY_RE.search(obj[0])
        ):
            return [obj[0], _REDACTED]
        return [_scrub(x) for x in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    if isinstance(obj, bytes):
        return _scrub_str(obj.decode("utf-8", "replace"))
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return _scrub_str(repr(obj))  # unknown object: scrub its repr (secrets can hide there)


def scrub_event(event: dict, hint: dict | None) -> dict | None:
    """Sentry ``before_send``: drop benign shutdown noise, else redact secrets.

    Pure + defensive — returns a scrubbed copy, or ``None`` to drop the event."""
    exc_info = (hint or {}).get("exc_info")
    if isinstance(exc_info, (list, tuple)) and exc_info and exc_info[0] is not None:
        try:
            if issubclass(exc_info[0], _DROP_EXC):
                return None
        except TypeError:  # exc_info[0] isn't a class — fall through to scrub
            pass
    return _scrub(event)


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry IFF a DSN is configured. Returns True when enabled.

    No-op (returns False) without a DSN or without sentry-sdk installed. Reads no
    environment directly — only the already-parsed ``settings``."""
    dsn = (settings.sentry_dsn or "").strip()
    if not dsn:
        return False
    if sentry_sdk is None:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; skipping")
        return False
    environment = settings.sentry_environment or settings.app_env
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        server_name="betting-ai",  # don't leak the real host identity to a third party
        send_default_pii=False,  # never attach user IP / cookies / headers
        include_local_variables=False,  # don't capture stack-frame locals (may hold raw secrets)
        include_source_context=False,  # no source lines around frames (may hold secret literals)
        max_request_body_size="never",  # never capture request bodies
        # sentry types before_send as (Event TypedDict, Hint) -> Event|None; our
        # signature is the structurally-compatible (dict, dict|None) -> dict|None.
        before_send=scrub_event,  # type: ignore[arg-type]
    )
    logger.info("Sentry error monitoring enabled (env=%s)", environment)
    return True
