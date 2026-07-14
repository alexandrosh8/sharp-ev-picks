"""Bounded, same-origin HTTPS GETs for read-only ingestion adapters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx

REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
DEFAULT_MAX_REDIRECTS = 5


class UnsafeUpstreamURL(RuntimeError):
    """A source URL or redirect escaped the adapter's HTTPS origin policy."""


class UpstreamBodyTooLarge(RuntimeError):
    """An upstream body crossed its adapter-specific byte ceiling."""

    def __init__(self, *, max_bytes: int) -> None:
        super().__init__(f"upstream response exceeded {max_bytes} bytes")
        self.max_bytes = max_bytes


class AsyncGetSession(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class BoundedHTTPResponse:
    response: Any
    body: bytes
    final_url: str


class _BoundedBodySink:
    """curl_cffi WRITEFUNCTION that stops transfer before buffering past cap."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._chunks: list[bytes] = []
        self._size = 0
        self.called = False
        self.exceeded = False

    def __call__(self, chunk: bytes) -> int:
        self.called = True
        raw = bytes(chunk)
        if self._size + len(raw) > self._max_bytes:
            self.exceeded = True
            # curl treats a short write as CURLE_WRITE_ERROR and aborts the
            # transfer. ``get_bounded`` translates that transport error into a
            # stable UpstreamBodyTooLarge signal without logging the URL.
            return 0
        self._chunks.append(raw)
        self._size += len(raw)
        return len(raw)

    def body(self) -> bytes:
        return b"".join(self._chunks)


def validate_https_url(url: str, *, allowed_hosts: Iterable[str]) -> str:
    """Return a fragment-free URL only when it is HTTPS on an exact host.

    Credentials and non-443 ports are rejected even on an allowed hostname.
    Exact host matching prevents suffix tricks such as
    ``www.oddschecker.com.attacker.invalid``.
    """
    allowed = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
    try:
        parsed = urlparse(str(url).strip())
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUpstreamURL("upstream URL is malformed") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or hostname not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise UnsafeUpstreamURL("upstream URL violates HTTPS origin policy")
    return parsed._replace(fragment="").geturl()


def _header_value(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name) or headers.get(name.lower())
    except AttributeError:
        return None
    return None if value is None else str(value)


def _declared_length(response: Any) -> int | None:
    raw = _header_value(getattr(response, "headers", {}), "Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _fallback_response_body(response: Any) -> bytes:
    """Read a fake/non-callback response for tests and compatible clients."""
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray | memoryview):
        return bytes(content)
    text = getattr(response, "text", "")
    if isinstance(text, bytes):
        return text
    return str(text or "").encode("utf-8")


async def get_bounded(
    session: AsyncGetSession,
    url: str,
    *,
    allowed_hosts: Iterable[str],
    max_bytes: int,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    **kwargs: Any,
) -> BoundedHTTPResponse:
    """GET with a streaming byte cap and validated manual redirects.

    Redirect following is manual and disabled at the HTTP client boundary, so
    every ``Location`` is checked before the next network request. The final
    response URL is checked again for test doubles and clients that ignore the
    per-request redirect override.
    """
    if isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    if isinstance(max_redirects, bool) or max_redirects < 0:
        raise ValueError("max_redirects must be >= 0")

    current = validate_https_url(url, allowed_hosts=allowed_hosts)
    request_kwargs = dict(kwargs)
    request_kwargs.pop("allow_redirects", None)
    request_kwargs.pop("content_callback", None)

    for redirect_count in range(max_redirects + 1):
        sink = _BoundedBodySink(max_bytes)
        try:
            response = await session.get(
                current,
                allow_redirects=False,
                content_callback=sink,
                **request_kwargs,
            )
        except Exception as exc:
            if sink.exceeded:
                raise UpstreamBodyTooLarge(max_bytes=max_bytes) from exc
            raise
        if sink.exceeded:
            raise UpstreamBodyTooLarge(max_bytes=max_bytes)
        declared = _declared_length(response)
        if declared is not None and declared > max_bytes:
            raise UpstreamBodyTooLarge(max_bytes=max_bytes)

        body = sink.body() if sink.called else _fallback_response_body(response)
        if len(body) > max_bytes:
            raise UpstreamBodyTooLarge(max_bytes=max_bytes)

        response_url = str(getattr(response, "url", "") or current)
        final_url = validate_https_url(response_url, allowed_hosts=allowed_hosts)
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in REDIRECT_STATUSES:
            return BoundedHTTPResponse(response=response, body=body, final_url=final_url)

        if redirect_count >= max_redirects:
            raise UnsafeUpstreamURL("upstream redirect limit exceeded")
        location = _header_value(getattr(response, "headers", {}), "Location")
        if not location:
            raise UnsafeUpstreamURL("upstream redirect omitted Location")
        current = validate_https_url(
            urljoin(final_url, location),
            allowed_hosts=allowed_hosts,
        )

    raise UnsafeUpstreamURL("upstream redirect limit exceeded")  # pragma: no cover


async def request_httpx_bounded(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an httpx request and incrementally buffer at most ``max_bytes``.

    ``AsyncClient.get/post`` eagerly buffers the whole remote response. This
    helper keeps transport retries effective while enforcing the ceiling on
    decoded bytes before JSON/HTML parsing. The returned in-memory response is
    detached from the streaming context and safe to consume with ``.json()``.
    """
    if isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be >= 1")
    request_kwargs = dict(kwargs)
    # Do not inherit a caller-owned client's redirect setting. JSON API
    # adapters classify 3xx explicitly; silently following can forward secret
    # headers/query parameters or escape a source origin before validation.
    request_kwargs.pop("follow_redirects", None)
    async with client.stream(
        method,
        url,
        follow_redirects=False,
        **request_kwargs,
    ) as response:
        declared = _declared_length(response)
        if declared is not None and declared > max_bytes:
            raise UpstreamBodyTooLarge(max_bytes=max_bytes)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise UpstreamBodyTooLarge(max_bytes=max_bytes)
            body.extend(chunk)
        headers = dict(response.headers)
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=bytes(body),
            request=response.request,
            extensions=response.extensions,
        )
