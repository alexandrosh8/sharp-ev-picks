"""Uniform browser hardening headers for every HTTP response."""

from collections.abc import Mapping

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_SECURITY_HEADERS: Mapping[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'; img-src 'self' data:; font-src 'self'; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; worker-src 'self'; manifest-src 'self'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}

_PRIVATE_NO_STORE = "private, no-store"


class SecurityHeadersMiddleware:
    """Small ASGI middleware; avoids BaseHTTPMiddleware cancellation semantics."""

    def __init__(
        self,
        app: ASGIApp,
        headers: Mapping[str, str] = DEFAULT_SECURITY_HEADERS,
    ) -> None:
        self._app = app
        self._headers = tuple(headers.items())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._headers:
                    # This is an application invariant, not a route default: a
                    # handler must not accidentally weaken the global policy.
                    headers[name] = value
                # Authenticated picks/performance/bankroll responses must never
                # enter browser or intermediary caches. Preserve an explicit
                # revalidation policy (the public manifest/service worker use
                # ``no-cache``), but make absent or route-level bare ``no-store``
                # policies private and legacy-proxy compatible.
                cache_control = headers.get("Cache-Control")
                if cache_control is None or cache_control.strip().casefold() == "no-store":
                    headers["Cache-Control"] = _PRIVATE_NO_STORE
                    headers["Pragma"] = "no-cache"
            await send(message)

        await self._app(scope, receive, send_with_headers)
