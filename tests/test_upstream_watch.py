"""Upstream release watch: PyPI version check -> once-per-release alert."""

import httpx

from app.maintenance.upstream_watch import (
    LAST_CHECK,
    UpdateNotice,
    check_upstream,
    fetch_latest_version,
    installed_version,
    update_alert,
)


def pypi_handler(versions: dict[str, str]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        for pkg, version in versions.items():
            if request.url.path == f"/pypi/{pkg}/json":
                return httpx.Response(200, json={"info": {"version": version}})
        return httpx.Response(404)

    return handler


async def test_fetch_latest_version_reads_pypi_json() -> None:
    transport = httpx.MockTransport(pypi_handler({"penaltyblog": "9.9.9"}))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await fetch_latest_version(client, "penaltyblog") == "9.9.9"


def test_installed_version_resolves_real_package() -> None:
    # penaltyblog is a pinned dependency of this project — must resolve.
    version = installed_version("penaltyblog")
    assert version is not None
    assert version.count(".") >= 1


async def test_check_upstream_reports_newer_release() -> None:
    transport = httpx.MockTransport(
        pypi_handler({"penaltyblog": "9.9.9", "oddsharvester": "9.9.9"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        updates = await check_upstream(client)
    assert {u.package for u in updates} == {"penaltyblog", "oddsharvester"}
    for u in updates:
        assert u.latest == "9.9.9"
        assert u.installed != u.latest
    assert LAST_CHECK["updates"]  # surfaced for /health + dashboard
    assert LAST_CHECK["checked_at"] is not None


async def test_check_upstream_quiet_when_current() -> None:
    current = {
        "penaltyblog": installed_version("penaltyblog") or "",
        "oddsharvester": installed_version("oddsharvester") or "",
    }
    transport = httpx.MockTransport(pypi_handler(current))
    async with httpx.AsyncClient(transport=transport) as client:
        updates = await check_upstream(client)
    assert updates == []
    assert LAST_CHECK["updates"] == []


async def test_check_upstream_survives_pypi_outage() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    async with httpx.AsyncClient(transport=transport) as client:
        updates = await check_upstream(client)
    assert updates == []  # outage is not "no updates known" — error recorded
    assert LAST_CHECK["error"] is not None


def test_update_alert_dedupes_per_release_not_per_day() -> None:
    notice = UpdateNotice(package="penaltyblog", installed="1.11.0", latest="1.12.0")
    a1 = update_alert(notice)
    a2 = update_alert(notice)
    assert a1.dedupe_key == a2.dedupe_key  # same release -> one alert ever
    assert (
        a1.dedupe_key
        != update_alert(
            UpdateNotice(package="penaltyblog", installed="1.11.0", latest="1.13.0")
        ).dedupe_key
    )  # next release -> new alert
    assert "penaltyblog" in a1.body
    assert "1.12.0" in a1.body
    assert "scripts/upgrade_deps.sh" in a1.body  # tells the user the safe path
