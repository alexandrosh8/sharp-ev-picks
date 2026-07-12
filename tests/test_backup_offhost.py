"""Off-host copy hook of scripts/backup_db.sh.

No network and no docker in tests: rsync/rclone are stubbed with recording
fakes prepended to PATH, and the copy step is exercised through the explicit
``--offhost-copy`` entry (the nightly path calls the same ``copy_offhost``
function on the just-created dump). Contract under test:

- ``user@host:/path`` targets ship via ``rsync -e ssh``;
- ``remote:path`` targets (no ``user@host`` form) ship via ``rclone copyto``;
- a set target with a FAILING copy exits non-zero (fail loud);
- the explicit subcommand refuses to run without a target;
- no credentials ever appear on the command line (ssh keys / rclone config
  live outside this script).
"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path("/workspace/scripts/backup_db.sh")
DUMP_NAME = "betting_2026-07-11T000000Z.dump"


def _env(tmp_path: Path, fake_bin: Path, target: str | None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["BACKUP_DIR"] = str(tmp_path / "backups")
    if target is None:
        env.pop("OFFHOST_BACKUP_TARGET", None)
    else:
        env["OFFHOST_BACKUP_TARGET"] = target
    return env


def _seed_dump(tmp_path: Path) -> Path:
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    dump = backups / DUMP_NAME
    dump.write_bytes(b"PGDMP-fake-archive")
    return dump


def _fake_tool(fake_bin: Path, name: str, exit_code: int = 0) -> Path:
    """Install a recording fake for `name`; returns the call-log path."""
    fake_bin.mkdir(exist_ok=True)
    calls = fake_bin / f"{name}.calls"
    tool = fake_bin / name
    tool.write_text(
        "#!/usr/bin/env bash\n" + f'printf \'%s\\n\' "$*" >> "{calls}"\n' + f"exit {exit_code}\n"
    )
    tool.chmod(0o755)
    return calls


def _run(tmp_path: Path, fake_bin: Path, target: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--offhost-copy"],
        env=_env(tmp_path, fake_bin, target),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_script_syntax_is_valid() -> None:
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_rsync_form_ships_newest_dump_over_ssh(tmp_path: Path) -> None:
    dump = _seed_dump(tmp_path)
    fake_bin = tmp_path / "bin"
    calls = _fake_tool(fake_bin, "rsync")
    proc = _run(tmp_path, fake_bin, "backup@offhost:/srv/betting-backups")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = calls.read_text()
    assert "-e ssh" in recorded
    assert str(dump) in recorded
    assert "backup@offhost:/srv/betting-backups/" in recorded
    # never a credential on the command line
    assert "password" not in recorded.lower()


def test_rclone_form_uses_copyto_with_basename(tmp_path: Path) -> None:
    dump = _seed_dump(tmp_path)
    fake_bin = tmp_path / "bin"
    calls = _fake_tool(fake_bin, "rclone")
    proc = _run(tmp_path, fake_bin, "offsite:betting-backups")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = calls.read_text()
    assert recorded.startswith("copyto ")
    assert str(dump) in recorded
    assert f"offsite:betting-backups/{DUMP_NAME}" in recorded


def test_failed_copy_exits_nonzero_and_loud(tmp_path: Path) -> None:
    _seed_dump(tmp_path)
    fake_bin = tmp_path / "bin"
    _fake_tool(fake_bin, "rsync", exit_code=1)
    proc = _run(tmp_path, fake_bin, "backup@offhost:/srv/betting-backups")
    assert proc.returncode != 0
    assert "FAILED" in proc.stdout + proc.stderr


def test_explicit_subcommand_requires_target(tmp_path: Path) -> None:
    _seed_dump(tmp_path)
    fake_bin = tmp_path / "bin"
    _fake_tool(fake_bin, "rsync")
    proc = _run(tmp_path, fake_bin, None)
    assert proc.returncode != 0
    assert "OFFHOST_BACKUP_TARGET" in proc.stdout + proc.stderr


def test_malformed_target_is_rejected(tmp_path: Path) -> None:
    # neither user@host:/path nor remote:path — refuse rather than guess
    _seed_dump(tmp_path)
    fake_bin = tmp_path / "bin"
    _fake_tool(fake_bin, "rsync")
    _fake_tool(fake_bin, "rclone")
    proc = _run(tmp_path, fake_bin, "/just/a/local/path")
    assert proc.returncode != 0
