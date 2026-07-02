"""scripts/safety_audit.sh — the structural no-autobet gate must stay green.

Runs the real script (pure local greps — no network) and asserts the hardened
checks from the WP4 audit are present, so a future edit cannot silently drop
them: scripts/ in the order-token scan, the extended banned-dependency list
(incl. uv.lock), the credential-field-name scan, the picks-only validator
function name, and the betfair_api.py runtime op-allowlist presence check.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "safety_audit.sh"


def test_safety_audit_script_passes() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"safety audit failed:\n{result.stdout}\n{result.stderr}"
    assert "SAFETY AUDIT: PASSED" in result.stdout


def test_safety_audit_script_contains_hardened_checks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    # (a) order-token scan covers scripts/ too (self-excluded, case-insensitive).
    assert "scripts/" in text
    assert "--exclude=safety_audit.sh" in text
    # (b) banned exchange-execution dependencies, incl. the lockfile.
    for name in ("betfairlightweight", "flumine", "betconnect", "betdaq"):
        assert name in text, f"banned dependency {name!r} missing from audit"
    assert "uv.lock" in text
    # (c) betfair credential field names may exist ONLY in app/config.py.
    for field in ("betfair_app_key", "betfair_read_only_username", "betfair_read_only_password"):
        assert field in text, f"credential field {field!r} missing from audit"
    # (d) the actual validator function name, not just the message string.
    assert "_enforce_picks_only" in text
    # (e) betfair_api.py runtime op-allowlist presence check.
    assert "_ALLOWED_OPS" in text
