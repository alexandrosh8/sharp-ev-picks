"""Bundled authentication-page asset contracts."""

from pathlib import Path

from app.api.routes import _AUTH_TEMPLATE_DIR, _LOGIN_HTML, _SETUP_HTML


def _template_paths() -> tuple[Path, Path]:
    return (
        (_AUTH_TEMPLATE_DIR / "login.html").resolve(),
        (_AUTH_TEMPLATE_DIR / "setup.html").resolve(),
    )


def test_auth_templates_are_bundled_files_loaded_verbatim() -> None:
    login_path, setup_path = _template_paths()

    assert _AUTH_TEMPLATE_DIR.is_absolute()
    assert login_path.is_file()
    assert setup_path.is_file()
    assert login_path.read_text(encoding="utf-8") == _LOGIN_HTML
    assert setup_path.read_text(encoding="utf-8") == _SETUP_HTML


def test_auth_templates_have_no_embedded_fonts_or_external_dependencies() -> None:
    for path in _template_paths():
        template = path.read_text(encoding="utf-8")
        lowered = template.casefold()

        assert path.stat().st_size < 16_384
        assert "@font-face" not in lowered
        assert "data:font" not in lowered
        assert 'src="http' not in lowered
        assert "src='http" not in lowered
        assert 'href="http' not in lowered
        assert "href='http" not in lowered
        assert "url(http" not in lowered
        assert "<link" not in lowered


def test_auth_controls_keep_mobile_safe_size_and_accessible_contrast_token() -> None:
    for path in _template_paths():
        template = path.read_text(encoding="utf-8")

        assert "--text3: #8b8b96;" in template
        assert "font: 16px var(--mono);" in template
        assert template.count("min-height: 44px;") == 2
        assert "JetBrains Mono" not in template
        assert "Chivo" not in template
