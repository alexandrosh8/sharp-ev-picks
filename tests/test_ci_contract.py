"""Static contract for the repository's required CI gates."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
LOCK = (ROOT / "uv.lock").read_text(encoding="utf-8")
SECCOMP = json.loads((ROOT / "docker" / "seccomp_profile.json").read_text(encoding="utf-8"))


def test_ci_installs_and_tests_every_optional_surface() -> None:
    assert "uv sync --frozen --all-extras --all-groups" in CI
    assert "pytest -n 2 --dist loadfile" in CI
    assert "--cov=app" in CI
    assert "--cov-fail-under=85" in CI


def test_ci_checks_schema_types_dependencies_and_safety() -> None:
    assert "alembic upgrade head" in CI
    assert "alembic check" in CI
    assert "mypy app tests" in CI
    assert "pip-audit" in CI
    assert "scripts/safety_audit.sh" in CI
    assert "gitleaks/gitleaks-action" in CI


def test_ci_rebuilds_and_browser_tests_the_dashboard() -> None:
    assert "tools/build_dashboard.py --check" in CI
    assert "node --check app/api/dashboard_src/app.js" in CI
    assert "playwright install --with-deps chromium" in CI
    assert 'DASHQA_MOCK_ONLY: "1"' in CI
    assert "scripts/dashboard_qa.py" in CI


def test_container_healthchecks_use_process_liveness_not_readiness() -> None:
    """A 20–40 minute first scrape must not make Docker report app death."""
    assert "127.0.0.1:8000/live" in DOCKERFILE
    assert "127.0.0.1:8000/live" in COMPOSE
    assert "127.0.0.1:8000/health" not in DOCKERFILE
    assert "127.0.0.1:8000/health" not in COMPOSE


def test_security_fixed_dependency_floors_are_pinned() -> None:
    assert '"pydantic-settings>=2.14.2"' in PYPROJECT
    assert '"starlette>=1.3.1"' in PYPROJECT
    assert '"pillow>=12.3.0"' in PYPROJECT


def test_repo_lint_excludes_vendored_agent_skill_sources() -> None:
    assert 'exclude = [".agents"]' in PYPROJECT
    assert "check app tests scripts alembic tools" in CI
    assert "format --check app tests scripts alembic tools" in CI


def test_container_runtime_is_immutable_and_unprivileged() -> None:
    assert "chown -R appuser:appuser /srv/betting-ai" not in DOCKERFILE
    assert "RUN chmod 0644 /srv/betting-ai/app/api/dashboard.html" in DOCKERFILE
    for contract in (
        'user: "1000:1000"',
        "read_only: true",
        "cap_drop:",
        "- ALL",
        "cap_add:",
        "- SYS_CHROOT",
        "no-new-privileges:true",
        "seccomp:./docker/seccomp_profile.json",
        "tmpfs:",
    ):
        assert contract in COMPOSE


def test_compose_requires_an_explicit_postgres_password() -> None:
    required_expression = "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}"
    assert COMPOSE.count(required_expression) == 2
    assert "${POSTGRES_PASSWORD:-betting_ai}" not in COMPOSE


def test_playwright_seccomp_profile_allows_only_needed_namespace_primitives() -> None:
    unconditional_allows = {
        syscall
        for rule in SECCOMP["syscalls"]
        if rule["action"] == "SCMP_ACT_ALLOW"
        and not rule.get("includes")
        and not rule.get("excludes")
        for syscall in rule["names"]
    }
    assert {"clone", "setns", "unshare"} <= unconditional_allows


def test_abandoned_socks_placeholder_is_excluded_from_resolution() -> None:
    assert 'exclude-dependencies = [\n    "socks",\n]' in PYPROJECT
    assert '\nname = "socks"\n' not in LOCK
