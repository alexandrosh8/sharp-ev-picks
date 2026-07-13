"""Deterministic build contract for the generated single-file dashboard."""

from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tools import build_dashboard


def test_generated_dashboard_has_exact_source_parity() -> None:
    rendered = build_dashboard.render_dashboard().encode("utf-8")
    assert rendered == build_dashboard.OUTPUT_PATH.read_bytes()


def test_build_inputs_are_absolute_resolved_and_fixed_order() -> None:
    expected = (
        build_dashboard.SHELL_PATH,
        build_dashboard.STYLES_PATH,
        build_dashboard.SCRIPT_PATH,
    )
    assert expected == build_dashboard.INPUT_PATHS
    for path in (*build_dashboard.INPUT_PATHS, build_dashboard.OUTPUT_PATH):
        assert path.is_absolute()
        assert path == path.resolve()


def test_write_is_byte_deterministic_and_idempotent(tmp_path: Path) -> None:
    first_render = build_dashboard.render_dashboard()
    second_render = build_dashboard.render_dashboard()
    assert first_render == second_render

    output_path = (tmp_path / "dashboard.html").resolve()
    assert build_dashboard.write_dashboard(first_render, output_path) is True
    first_bytes = output_path.read_bytes()
    assert stat.S_IMODE(output_path.stat().st_mode) == build_dashboard.OUTPUT_MODE
    output_path.chmod(0o600)
    first_mtime_ns = output_path.stat().st_mtime_ns
    assert build_dashboard.write_dashboard(second_render, output_path) is False
    assert output_path.read_bytes() == first_bytes
    assert output_path.stat().st_mtime_ns == first_mtime_ns
    assert stat.S_IMODE(output_path.stat().st_mode) == build_dashboard.OUTPUT_MODE
    assert (
        hashlib.sha256(first_bytes).hexdigest()
        == build_dashboard.build_metrics(first_render).sha256
    )


def test_check_mode_accepts_committed_artifact_without_writing() -> None:
    before = build_dashboard.OUTPUT_PATH.stat()
    builder_path = (build_dashboard.REPOSITORY_ROOT / "tools/build_dashboard.py").resolve()
    completed = subprocess.run(
        [sys.executable, str(builder_path), "--check"],
        check=False,
        capture_output=True,
        text=True,
    )
    after = build_dashboard.OUTPUT_PATH.stat()
    assert completed.returncode == 0, completed.stderr
    assert "dashboard verified:" in completed.stdout
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)


def test_reviewable_sources_have_no_embedded_fonts_or_external_code() -> None:
    styles = build_dashboard.STYLES_PATH.read_text(encoding="utf-8")
    script = build_dashboard.SCRIPT_PATH.read_text(encoding="utf-8")
    shell = build_dashboard.SHELL_PATH.read_text(encoding="utf-8")

    assert "@font-face" not in styles
    assert "data:font" not in styles
    assert "IBM Plex Sans" not in styles
    assert "JetBrains Mono" not in styles
    assert "<style" not in styles.casefold()
    assert "<script" not in script.casefold()
    assert shell.count(build_dashboard.STYLES_MARKER) == 1
    assert shell.count(build_dashboard.SCRIPT_MARKER) == 1


@pytest.mark.parametrize(
    ("fragment", "expected_error"),
    [
        ('<div id="rail"></div>', "Duplicate HTML ids"),
        ('<img src="https://assets.example/dashboard.png">', r"Unexpected img\[src\]"),
        ('<img src="/dashboard.png">', r"Unexpected img\[src\]"),
        ('<link rel="stylesheet" href="/dashboard.css">', "Unexpected link relation"),
        ("@@UNRESOLVED_BUILD_VALUE@@", "Unresolved placeholder"),
        ("<style></style>", "exactly one style"),
        ("<script></script>", "exactly one style and one script"),
    ],
)
def test_validation_rejects_ambiguous_or_external_artifacts(
    fragment: str, expected_error: str
) -> None:
    rendered = build_dashboard.render_dashboard().replace("</body>", f"{fragment}</body>")
    with pytest.raises(build_dashboard.DashboardBuildError, match=expected_error):
        build_dashboard.validate_dashboard(rendered)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_extracted_javascript_passes_node_syntax_check() -> None:
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "--check", str(build_dashboard.SCRIPT_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
