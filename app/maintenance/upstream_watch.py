"""Watch PyPI for new releases of the proven upstream engines.

The platform binds penaltyblog (pricing) and oddsharvester (odds scrape)
directly, so a new upstream release is operationally interesting — but
NEVER auto-installed: a release can change devig numbers or scraper DOM
handling under the live engine. The job only notifies (Telegram/webhook
via the existing dispatcher, once per release thanks to the dedupe key)
and points at scripts/upgrade_deps.sh, which bumps + runs the full test
gate and restores the previous lockfile on any failure.

Read-only GET of the public PyPI JSON API; no keys, no writes.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from typing import Any

import httpx

from app.notifications.base import Alert

logger = logging.getLogger(__name__)

# lightgbm/xgboost are the phase-5 NBA model libraries (ADR-0005/0009);
# not installed until then — installed_version() returns None and the
# watch skips them, so coverage starts automatically with phase 5.
WATCHED_PACKAGES: tuple[str, ...] = ("penaltyblog", "oddsharvester", "lightgbm", "xgboost")

# Surfaced by GET /health and the dashboard banner. Reset on every check;
# in-memory only (the daily job repopulates after a restart).
LAST_CHECK: dict[str, Any] = {"checked_at": None, "updates": [], "error": None}


@dataclass(frozen=True)
class UpdateNotice:
    package: str
    installed: str
    latest: str


def installed_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


async def fetch_latest_version(client: httpx.AsyncClient, package: str) -> str:
    """Latest released version per PyPI's JSON API."""
    response = await client.get(f"https://pypi.org/pypi/{package}/json", timeout=15.0)
    response.raise_for_status()
    return str(response.json()["info"]["version"])


async def check_upstream(
    client: httpx.AsyncClient,
    packages: tuple[str, ...] = WATCHED_PACKAGES,
) -> list[UpdateNotice]:
    """Compare installed versions to PyPI; record the result in LAST_CHECK.

    A PyPI outage is recorded as an error — it must never read as
    "everything is current".
    """
    updates: list[UpdateNotice] = []
    errors: list[str] = []
    for package in packages:
        installed = installed_version(package)
        if installed is None:
            logger.info("upstream watch: %s not installed; skipping", package)
            continue
        try:
            latest = await fetch_latest_version(client, package)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            errors.append(f"{package}: {type(exc).__name__}")
            continue
        # String inequality, not ordering: PyPI's "version" is the latest
        # release, so any difference from the installed version is news.
        if latest != installed:
            updates.append(UpdateNotice(package=package, installed=installed, latest=latest))
            logger.warning(
                "upstream release available: %s %s (installed %s) — "
                "run scripts/upgrade_deps.sh for a tested upgrade",
                package,
                latest,
                installed,
            )
    LAST_CHECK["checked_at"] = datetime.now(tz=UTC).isoformat()
    LAST_CHECK["updates"] = [
        {"package": u.package, "installed": u.installed, "latest": u.latest} for u in updates
    ]
    LAST_CHECK["error"] = "; ".join(errors) if errors else None
    return updates


def update_alert(notice: UpdateNotice) -> Alert:
    """Alert for one new release. The dedupe key is per (package, version),
    so the daily job notifies ONCE per release, not once per day."""
    return Alert(
        pick_id=f"upstream-{notice.package}",
        title=f"Upstream release: {notice.package} {notice.latest}",
        body=(
            f"📦 {notice.package} {notice.latest} is out "
            f"(installed: {notice.installed}).\n"
            "Tested upgrade: bash scripts/upgrade_deps.sh\n"
            "(bumps, runs the full test gate incl. penaltyblog parity, "
            "restores the old lockfile on any failure — review, then commit)"
        ),
        dedupe_key=f"upstream:{notice.package}:{notice.latest}",
    )
