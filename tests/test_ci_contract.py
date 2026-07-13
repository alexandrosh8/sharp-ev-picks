"""Static contract for the repository's required CI gates."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_ci_installs_and_tests_every_optional_surface() -> None:
    assert "uv sync --frozen --all-extras --all-groups" in CI
    assert "pytest --cov=app" in CI
    assert "--cov-fail-under=85" in CI


def test_ci_checks_schema_types_dependencies_and_safety() -> None:
    assert "alembic upgrade head" in CI
    assert "alembic check" in CI
    assert "mypy app tests" in CI
    assert "pip-audit" in CI
    assert "scripts/safety_audit.sh" in CI
    assert "gitleaks/gitleaks-action" in CI


def test_security_fixed_dependency_floors_are_pinned() -> None:
    assert '"pydantic-settings>=2.14.2"' in PYPROJECT
    assert '"starlette>=1.3.1"' in PYPROJECT
    assert '"pillow>=12.3.0"' in PYPROJECT


def test_repo_lint_excludes_vendored_agent_skill_sources() -> None:
    assert 'exclude = [".agents"]' in PYPROJECT
    assert "check app tests scripts alembic" in CI
