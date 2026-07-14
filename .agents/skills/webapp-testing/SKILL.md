---
name: webapp-testing
description: "Repository-local Playwright workflow for deterministic dashboard QA, responsive interaction checks, screenshots, console/page errors, and optional managed local servers."
license: Complete terms in LICENSE.txt
---

# Web Application Testing

Use this skill for dashboard browser QA after loading
`vanilla-dashboard-architecture`. Run every command from the repository root
with the locked project environment.

## Primary project workflow

The committed dashboard is self-contained and the existing harness mocks API
responses without a live server:

```bash
.venv/bin/python tools/build_dashboard.py --check
node --check app/api/dashboard_src/app.js
DASHQA_HTML="$PWD/app/api/dashboard.html" \
DASHQA_MOCK_ONLY=1 \
DASHQA_OUT="${TMPDIR:-/tmp}/sharp-dashboard-qa" \
  .venv/bin/python scripts/dashboard_qa.py
```

`bash scripts/verify_codex_workspace.sh` runs this browser regression plus the
full test/coverage, lint, type, dependency, safety, and secret gates.

## Managed-server helper

The optional helper is versioned inside this skill, not at repository-root
`scripts/`:

```bash
.venv/bin/python .agents/skills/webapp-testing/scripts/with_server.py --help
```

It accepts argument-vector server commands (no shell evaluation) and a separate
absolute working directory. Example:

```bash
.venv/bin/python .agents/skills/webapp-testing/scripts/with_server.py \
  --server ".venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000" \
  --server-cwd "$PWD" \
  --port 8000 \
  -- .venv/bin/python /tmp/sharp-dashboard-custom-qa.py
```

For multiple servers, repeat `--server`, `--server-cwd`, and `--port` in the
same order. Never embed directory changes or chained shell commands in a server
string.

## Reconnaissance then assertions

1. Wait for the required load state and explicit application-ready marker.
2. Record console errors, page errors, failed requests, status codes, and
   unexpected external requests.
3. Inspect semantic roles/labels and rendered text before choosing selectors.
4. Exercise keyboard, focus, mobile sheet/navigation, authentication, loading,
   empty, degraded, and error states.
5. Measure width/overflow, CLS, payload bytes, requests, and interaction timing.
6. Save screenshots and machine-readable results outside the repository.

Prefer role/label/test-id selectors over visual coordinates. Untrusted values
must enter the DOM through text nodes or validated properties, never unsafe
HTML sinks.

## References

- Project harness: `scripts/dashboard_qa.py`
- Dashboard contract: `tests/test_dashboard_contract.py`
- Frontend checklist: `docs/frontend-qa-checklist.md`
- Skill examples: `.agents/skills/webapp-testing/examples/`
