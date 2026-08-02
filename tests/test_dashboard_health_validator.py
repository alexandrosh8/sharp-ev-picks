"""Behavioral contract for the dashboard's /health payload validator.

Executes ``validateHealthPayload`` (extracted verbatim from app.js) under
Node against the exact payload shapes ``GET /health`` emits (app/api/routes.py):

- authenticated body: full detail incl. ``polls``/interval/edge floors, with
  status "ok" (200), "partial" (200, some sports degraded), "degraded" (503);
- anonymous/redacted body: freshness metadata + ``has_completed_poll`` only.

Regression under test (audit 2026-07-27): the validator rejected "partial"
and the redacted anonymous body, failing the whole health card closed into a
false "Could not verify system health / odds data is stale" banner.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tools import build_dashboard

_HELPER_FUNCTIONS = (
    "isRecord",
    "expectObjectPayload",
    "responseError",
    "timestampMs",
    "validateHealthPayload",
)

_ANONYMOUS_OK = {
    "status": "ok",
    "mode": "picks-only",
    "newest_poll_age_seconds": 34.4,
    "poll_max_age_seconds": 3600.0,
    "max_odds_age_seconds": 600.0,
    "has_completed_poll": True,
}

_AUTHENTICATED_OK = {
    "status": "ok",
    "mode": "picks-only",
    "polls": {
        "soccer": {"finished_at": "2026-08-02T12:00:00Z", "per_market": {"h2h": 12}},
        "tennis": {"finished_at": None},
    },
    "newest_poll_age_seconds": 34.4,
    "poll_max_age_seconds": 3600.0,
    "poll_interval_seconds": 300.0,
    "max_odds_age_seconds": 600.0,
    "value_min_edge": 0.03,
    "value_volume_min_edge": 0.05,
}


def _case(name: str, payload: dict | list | None, http_status: int, valid: bool) -> dict:
    return {"name": name, "payload": payload, "httpStatus": http_status, "valid": valid}


_CASES = [
    _case("authenticated ok 200", _AUTHENTICATED_OK, 200, True),
    _case("authenticated partial 200", {**_AUTHENTICATED_OK, "status": "partial"}, 200, True),
    _case("authenticated degraded 503", {**_AUTHENTICATED_OK, "status": "degraded"}, 503, True),
    _case("anonymous ok 200", _ANONYMOUS_OK, 200, True),
    _case("anonymous partial 200", {**_ANONYMOUS_OK, "status": "partial"}, 200, True),
    _case("anonymous degraded 503", {**_ANONYMOUS_OK, "status": "degraded"}, 503, True),
    _case(
        "anonymous cold start (no completed poll)",
        {
            **_ANONYMOUS_OK,
            "status": "degraded",
            "newest_poll_age_seconds": None,
            "has_completed_poll": False,
        },
        503,
        True,
    ),
    # Fail-closed cases: mismatched status/HTTP pairs, unknown status, and
    # bodies that fit NEITHER the authenticated nor the redacted shape.
    _case(
        "degraded body on 200 disagrees", {**_AUTHENTICATED_OK, "status": "degraded"}, 200, False
    ),
    _case("ok body on 503 disagrees", {**_AUTHENTICATED_OK, "status": "ok"}, 503, False),
    _case("partial body on 503 disagrees", {**_AUTHENTICATED_OK, "status": "partial"}, 503, False),
    _case("unknown status", {**_AUTHENTICATED_OK, "status": "sideways"}, 200, False),
    _case("missing mode", {k: v for k, v in _ANONYMOUS_OK.items() if k != "mode"}, 200, False),
    _case(
        "neither polls nor has_completed_poll",
        {k: v for k, v in _ANONYMOUS_OK.items() if k != "has_completed_poll"},
        200,
        False,
    ),
    _case(
        "invalid poll record",
        {**_AUTHENTICATED_OK, "polls": {"soccer": {"finished_at": "not-a-time"}}},
        200,
        False,
    ),
    _case("negative poll age", {**_ANONYMOUS_OK, "newest_poll_age_seconds": -3}, 200, False),
    _case("non-numeric ceiling", {**_ANONYMOUS_OK, "poll_max_age_seconds": "soon"}, 200, False),
    _case("array payload", [], 200, False),
    _case("null payload", None, 200, False),
]

_RUNNER = """
"use strict";
%(functions)s
const cases = JSON.parse(require("fs").readFileSync(0, "utf-8"));
const results = cases.map((c) => {
  try {
    validateHealthPayload(c.payload, c.httpStatus);
    return { name: c.name, valid: true };
  } catch (e) {
    return { name: c.name, valid: false, error: String(e && e.message) };
  }
});
process.stdout.write(JSON.stringify(results));
"""


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unterminated function {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_health_validator_accepts_every_backend_shape_and_fails_closed() -> None:
    source = build_dashboard.SCRIPT_PATH.read_text(encoding="utf-8")
    script = _RUNNER % {
        "functions": "\n".join(_extract_function(source, name) for name in _HELPER_FUNCTIONS)
    }
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, "-e", script],
        input=json.dumps([{k: c[k] for k in ("name", "payload", "httpStatus")} for c in _CASES]),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    results = {r["name"]: r for r in json.loads(completed.stdout)}
    failures = [
        f"{c['name']}: expected valid={c['valid']} got {results[c['name']]}"
        for c in _CASES
        if results[c["name"]]["valid"] is not c["valid"]
    ]
    assert not failures, "\n".join(failures)
