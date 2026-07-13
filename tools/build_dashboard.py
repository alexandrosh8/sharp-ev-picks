"""Build the single-file dashboard from deterministic, reviewable sources."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import stat
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY: Final = (REPOSITORY_ROOT / "app/api/dashboard_src").resolve()
SHELL_PATH: Final = (SOURCE_DIRECTORY / "shell.html").resolve()
STYLES_PATH: Final = (SOURCE_DIRECTORY / "styles.css").resolve()
SCRIPT_PATH: Final = (SOURCE_DIRECTORY / "app.js").resolve()
OUTPUT_PATH: Final = (REPOSITORY_ROOT / "app/api/dashboard.html").resolve()
INPUT_PATHS: Final = (SHELL_PATH, STYLES_PATH, SCRIPT_PATH)
OUTPUT_MODE: Final = 0o644

STYLES_MARKER: Final = "@@DASHBOARD_STYLES@@"
SCRIPT_MARKER: Final = "@@DASHBOARD_SCRIPT@@"
_STYLES_MARKER_LINE: Final = f"      {STYLES_MARKER}\n"
_SCRIPT_MARKER_LINE: Final = f"      {SCRIPT_MARKER}\n"

_PLACEHOLDER_RE: Final = re.compile(r"@@[A-Z][A-Z0-9_]*@@|\{\{[^{}\n]+\}\}|<%[^%]+%>")
_CSS_URL_RE: Final = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<url>.*?)(?P=quote)\s*\)",
    flags=re.IGNORECASE | re.DOTALL,
)
_ALLOWED_LINK_RELS: Final = frozenset({"apple-touch-icon", "icon", "manifest"})
_ASSET_TAG_ATTRIBUTES: Final = {
    "audio": ("src",),
    "embed": ("src",),
    "iframe": ("src",),
    "img": ("src", "srcset"),
    "object": ("data",),
    "script": ("src",),
    "source": ("src", "srcset"),
    "video": ("poster", "src"),
}


class DashboardBuildError(ValueError):
    """Raised when a source or rendered dashboard violates the build contract."""


@dataclass(frozen=True)
class BuildMetrics:
    raw_bytes: int
    gzip_bytes: int
    sha256: str


class _DashboardAuditParser(HTMLParser):
    """Collect only the structural facts required by the build gate."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.ids: list[str] = []
        self.style_blocks = 0
        self.script_blocks = 0
        self.asset_references: list[tuple[str, str, str]] = []
        self.link_relations: list[tuple[frozenset[str], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        attributes = {name.casefold(): value or "" for name, value in attrs}
        element_id = attributes.get("id")
        if element_id is not None:
            self.ids.append(element_id)

        if normalized_tag == "style":
            self.style_blocks += 1
        elif normalized_tag == "script":
            self.script_blocks += 1

        if normalized_tag == "link":
            relations = frozenset(attributes.get("rel", "").casefold().split())
            self.link_relations.append((relations, attributes.get("href", "")))

        for attribute_name in _ASSET_TAG_ATTRIBUTES.get(normalized_tag, ()):
            if reference := attributes.get(attribute_name):
                self.asset_references.append((normalized_tag, attribute_name, reference))


def _read_utf8(path: Path) -> str:
    resolved_path = path.expanduser().resolve(strict=True)
    if not resolved_path.is_absolute():  # pragma: no cover - resolve() guarantees this
        raise DashboardBuildError(f"Build input is not absolute: {resolved_path}")
    raw = resolved_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise DashboardBuildError(f"UTF-8 BOM is not allowed: {resolved_path}")
    if b"\r" in raw:
        raise DashboardBuildError(f"Only LF newlines are allowed: {resolved_path}")
    if not raw.endswith(b"\n"):
        raise DashboardBuildError(f"Build input must end with a newline: {resolved_path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DashboardBuildError(f"Build input is not UTF-8: {resolved_path}") from exc


def _is_allowed_inline_asset(reference: str) -> bool:
    normalized = reference.strip()
    return normalized.startswith("data:image/svg+xml,") or normalized.startswith("#")


def _is_allowed_link(relations: frozenset[str], reference: str) -> bool:
    if relations == frozenset({"manifest"}):
        return reference.strip() == "/manifest.webmanifest"
    if relations in (frozenset({"icon"}), frozenset({"apple-touch-icon"})):
        return _is_allowed_inline_asset(reference)
    return False


def _validate_shell(shell: str) -> None:
    for marker in (STYLES_MARKER, SCRIPT_MARKER):
        if shell.count(marker) != 1:
            raise DashboardBuildError(f"Shell must contain exactly one {marker} marker")
    if shell.count(_STYLES_MARKER_LINE) != 1 or shell.count(_SCRIPT_MARKER_LINE) != 1:
        raise DashboardBuildError("Dashboard markers must occupy their canonical indented lines")

    parser = _DashboardAuditParser()
    parser.feed(shell)
    parser.close()
    if parser.style_blocks != 1 or parser.script_blocks != 1:
        raise DashboardBuildError("Shell must contain exactly one style and one script block")

    style_open = shell.casefold().index("<style")
    style_close = shell.casefold().index("</style>")
    script_open = shell.casefold().index("<script")
    script_close = shell.casefold().index("</script>")
    if not style_open < shell.index(STYLES_MARKER) < style_close:
        raise DashboardBuildError("Styles marker must be inside the style block")
    if not script_open < shell.index(SCRIPT_MARKER) < script_close:
        raise DashboardBuildError("Script marker must be inside the script block")


def _validate_styles(styles: str) -> None:
    lowered = styles.casefold()
    if "<style" in lowered or "</style>" in lowered:
        raise DashboardBuildError("styles.css must not contain style tags")
    if "@font-face" in lowered or "data:font" in lowered:
        raise DashboardBuildError("Embedded fonts are prohibited in the dashboard build")
    if re.search(r"@import\b", styles, flags=re.IGNORECASE):
        raise DashboardBuildError("External CSS imports are prohibited")
    for match in _CSS_URL_RE.finditer(styles):
        reference = match.group("url").strip()
        if not _is_allowed_inline_asset(reference):
            raise DashboardBuildError(f"Unexpected CSS asset reference: {reference[:120]}")


def _validate_script(script: str) -> None:
    lowered = script.casefold()
    if "<script" in lowered or "</script>" in lowered:
        raise DashboardBuildError("app.js must not contain script tags")


def validate_dashboard(rendered: str) -> None:
    """Fail closed on structure that makes the generated artifact unsafe or ambiguous."""

    if unresolved := _PLACEHOLDER_RE.search(rendered):
        raise DashboardBuildError(f"Unresolved placeholder: {unresolved.group(0)}")

    parser = _DashboardAuditParser()
    parser.feed(rendered)
    parser.close()
    if parser.style_blocks != 1 or parser.script_blocks != 1:
        raise DashboardBuildError("Dashboard must contain exactly one style and one script block")

    duplicate_ids = sorted(
        element_id for element_id, count in Counter(parser.ids).items() if count > 1
    )
    if duplicate_ids:
        raise DashboardBuildError(f"Duplicate HTML ids: {', '.join(duplicate_ids)}")

    for relations, reference in parser.link_relations:
        if len(relations) != 1 or not relations <= _ALLOWED_LINK_RELS:
            relation_label = " ".join(sorted(relations)) or "<missing>"
            raise DashboardBuildError(f"Unexpected link relation: {relation_label}")
        if not _is_allowed_link(relations, reference):
            raise DashboardBuildError(f"Unexpected link asset reference: {reference[:120]}")

    for tag, attribute_name, reference in parser.asset_references:
        if tag == "script" or not _is_allowed_inline_asset(reference):
            raise DashboardBuildError(
                f"Unexpected {tag}[{attribute_name}] asset reference: {reference[:120]}"
            )


def render_dashboard() -> str:
    """Read the fixed input sequence and return the validated dashboard artifact."""

    shell, styles, script = tuple(_read_utf8(path) for path in INPUT_PATHS)
    _validate_shell(shell)
    _validate_styles(styles)
    _validate_script(script)

    rendered = shell.replace(_STYLES_MARKER_LINE, styles).replace(_SCRIPT_MARKER_LINE, script)
    validate_dashboard(rendered)
    if not rendered.endswith("\n"):
        raise DashboardBuildError("Rendered dashboard must end with a newline")
    return rendered


def build_metrics(rendered: str) -> BuildMetrics:
    raw = rendered.encode("utf-8")
    return BuildMetrics(
        raw_bytes=len(raw),
        gzip_bytes=len(gzip.compress(raw, compresslevel=9, mtime=0)),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def write_dashboard(rendered: str, output_path: Path = OUTPUT_PATH) -> bool:
    """Atomically write changed bytes; return ``False`` for an idempotent no-op."""

    resolved_output = output_path.expanduser().resolve()
    if not resolved_output.is_absolute():  # pragma: no cover - resolve() guarantees this
        raise DashboardBuildError(f"Build output is not absolute: {resolved_output}")
    output_bytes = rendered.encode("utf-8")
    if resolved_output.is_file() and resolved_output.read_bytes() == output_bytes:
        # NamedTemporaryFile defaults to 0600. Mode is part of the generated
        # artifact contract because the root-owned production image serves this
        # file as an unprivileged user.
        if stat.S_IMODE(resolved_output.stat().st_mode) != OUTPUT_MODE:
            resolved_output.chmod(OUTPUT_MODE)
        return False

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved_output.parent,
            prefix=f".{resolved_output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(output_bytes)
            temporary.flush()
            temporary_path = Path(temporary.name).resolve()
        temporary_path.chmod(OUTPUT_MODE)
        temporary_path.replace(resolved_output)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


def _check_output(rendered: str) -> bool:
    return (
        OUTPUT_PATH.is_file()
        and stat.S_IMODE(OUTPUT_PATH.stat().st_mode) == OUTPUT_MODE
        and OUTPUT_PATH.read_bytes() == rendered.encode("utf-8")
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when dashboard.html is not the exact deterministic build output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        rendered = render_dashboard()
    except (DashboardBuildError, FileNotFoundError) as exc:
        print(f"dashboard build failed: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if not _check_output(rendered):
            print(f"dashboard build is stale: {OUTPUT_PATH}", file=sys.stderr)
            return 1
        action = "verified"
    else:
        action = "updated" if write_dashboard(rendered) else "unchanged"

    metrics = build_metrics(rendered)
    print(
        f"dashboard {action}: raw={metrics.raw_bytes} gzip={metrics.gzip_bytes} "
        f"sha256={metrics.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
