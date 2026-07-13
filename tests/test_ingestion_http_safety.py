"""No-network regressions for bounded same-origin ingestion HTTP."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.ingestion.http_safety import (
    UnsafeUpstreamURL,
    UpstreamBodyTooLarge,
    get_bounded,
    request_httpx_bounded,
    validate_https_url,
)

ALLOWED = frozenset({"www.oddschecker.com"})


@pytest.mark.parametrize(
    "url",
    [
        "http://www.oddschecker.com/football",
        "https://evil.invalid/football",
        "https://www.oddschecker.com.evil.invalid/football",
        "https://user:secret@www.oddschecker.com/football",
        "https://www.oddschecker.com:8443/football",
    ],
)
def test_https_origin_policy_rejects_scheme_host_credential_and_port_escape(url: str) -> None:
    with pytest.raises(UnsafeUpstreamURL):
        validate_https_url(url, allowed_hosts=ALLOWED)


def test_https_origin_policy_accepts_exact_host_and_default_tls_port() -> None:
    assert (
        validate_https_url(
            "https://www.oddschecker.com:443/football#prices",
            allowed_hosts=ALLOWED,
        )
        == "https://www.oddschecker.com:443/football"
    )


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str = "https://www.oddschecker.com/football",
        text: str = "ok",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers or {}


async def test_manual_redirect_validates_location_before_second_request() -> None:
    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, url: str, **kwargs: Any) -> _Response:
            del url, kwargs
            self.calls += 1
            return _Response(
                status_code=302,
                headers={"Location": "https://169.254.169.254/latest/meta-data"},
            )

    session = Session()
    with pytest.raises(UnsafeUpstreamURL):
        await get_bounded(
            session,
            "https://www.oddschecker.com/football",
            allowed_hosts=ALLOWED,
            max_bytes=1024,
        )
    assert session.calls == 1


async def test_final_response_url_is_validated_when_client_ignores_redirect_override() -> None:
    class Session:
        async def get(self, url: str, **kwargs: Any) -> _Response:
            del url, kwargs
            return _Response(url="https://evil.invalid/redirected")

    with pytest.raises(UnsafeUpstreamURL):
        await get_bounded(
            Session(),
            "https://www.oddschecker.com/football",
            allowed_hosts=ALLOWED,
            max_bytes=1024,
        )


async def test_streaming_callback_aborts_before_body_crosses_byte_ceiling() -> None:
    class Session:
        async def get(self, url: str, **kwargs: Any) -> _Response:
            del url
            callback = kwargs["content_callback"]
            assert callback(b"12345678") == 8
            assert callback(b"9") == 0
            raise RuntimeError("curl short write")

    with pytest.raises(UpstreamBodyTooLarge) as caught:
        await get_bounded(
            Session(),
            "https://www.oddschecker.com/football",
            allowed_hosts=ALLOWED,
            max_bytes=8,
        )
    assert caught.value.max_bytes == 8


async def test_declared_content_length_fails_before_body_use() -> None:
    class Session:
        async def get(self, url: str, **kwargs: Any) -> _Response:
            del url, kwargs
            return _Response(headers={"Content-Length": "999"}, text="")

    with pytest.raises(UpstreamBodyTooLarge):
        await get_bounded(
            Session(),
            "https://www.oddschecker.com/football",
            allowed_hosts=ALLOWED,
            max_bytes=8,
        )


async def test_httpx_bounded_request_never_inherits_redirect_following() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "api.example.test":
            return httpx.Response(
                302,
                headers={"Location": "https://evil.invalid/collect"},
                request=request,
            )
        return httpx.Response(200, content=b"followed", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        response = await request_httpx_bounded(
            client,
            "GET",
            "https://api.example.test/data",
            max_bytes=1024,
        )

    assert response.status_code == 302
    assert hosts == ["api.example.test"]
