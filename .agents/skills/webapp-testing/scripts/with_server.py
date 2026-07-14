#!/usr/bin/env python3
"""Start local servers, wait for ports, run one command, then stop servers.

Examples (from the repository root):
    .venv/bin/python .agents/skills/webapp-testing/scripts/with_server.py \
      --server ".venv/bin/python -m uvicorn app.main:app --port 8000" \
      --server-cwd "$PWD" --port 8000 -- \
      .venv/bin/python /tmp/custom_qa.py

Repeat --server, --server-cwd, and --port in the same order for multiple
servers. Server strings are parsed as argument vectors; no shell syntax is
interpreted.
"""

from __future__ import annotations

import argparse
import shlex
import socket
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerSpec:
    argv: tuple[str, ...]
    cwd: Path
    port: int


def is_server_ready(process: subprocess.Popen[bytes], port: int, timeout: float) -> bool:
    """Poll a loopback port until ready, timeout, or early process exit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command while one or more local servers are ready"
    )
    parser.add_argument(
        "--server",
        action="append",
        dest="servers",
        required=True,
        help="Server command parsed with shlex; shell operators are unsupported",
    )
    parser.add_argument(
        "--server-cwd",
        action="append",
        dest="server_cwds",
        type=Path,
        help="Absolute/relative working directory; repeat once per server",
    )
    parser.add_argument(
        "--port",
        action="append",
        dest="ports",
        type=int,
        required=True,
        help="Loopback readiness port; repeat once per server",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Readiness timeout in seconds per server",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after the separator and server readiness",
    )
    return parser.parse_args(argv)


def build_specs(args: argparse.Namespace) -> tuple[ServerSpec, ...]:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise ValueError("no command specified after the server options")
    if len(args.servers) != len(args.ports):
        raise ValueError("--server and --port counts must match")

    raw_cwds = args.server_cwds or [Path.cwd()] * len(args.servers)
    if len(raw_cwds) != len(args.servers):
        raise ValueError("--server-cwd must be omitted or repeated once per server")

    specs: list[ServerSpec] = []
    for raw_command, raw_cwd, port in zip(args.servers, raw_cwds, args.ports, strict=True):
        cwd = raw_cwd.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError(f"server working directory is not a directory: {cwd}")
        argv = tuple(shlex.split(raw_command))
        if not argv:
            raise ValueError("server command cannot be empty")
        specs.append(ServerSpec(argv=argv, cwd=cwd, port=port))
    args.command = command
    return tuple(specs)


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        specs = build_specs(args)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"with_server: {exc}") from exc

    processes: list[subprocess.Popen[bytes]] = []
    try:
        for index, spec in enumerate(specs, start=1):
            print(f"Starting server {index}/{len(specs)}: {shlex.join(spec.argv)}")
            process = subprocess.Popen(spec.argv, cwd=spec.cwd)
            processes.append(process)
            if not is_server_ready(process, spec.port, args.timeout):
                raise RuntimeError(f"server failed before loopback port {spec.port} became ready")
            print(f"Server ready on 127.0.0.1:{spec.port}")

        print(f"Running: {shlex.join(args.command)}")
        return subprocess.run(args.command, check=False).returncode
    finally:
        for process in reversed(processes):
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
