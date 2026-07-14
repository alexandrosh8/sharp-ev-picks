# Mac local development

Run from the repository root. Prerequisites: current Codex, uv, Docker Desktop
or Colima, jq, Node.js, and gitleaks.

```bash
bash scripts/bootstrap_codex.sh --check
test -e .env || install -m 0600 .env.example .env
bash scripts/bootstrap_codex.sh
```

The `.env.example` values are loopback-only local fixtures. Add optional private
API/alert values out of band; never copy a production `.env` to the development
workspace or Git.

The bootstrap installs the exact lock (`--frozen --all-extras --all-groups`),
installs Playwright Chromium, starts PostgreSQL/Redis on loopback ports
5433/6380 when `.env` exists, and applies Alembic migrations.

Run the app:

```bash
bash scripts/run_app.sh
```

Or with reload:

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Verify:

```bash
curl -s http://127.0.0.1:8000/live
bash scripts/verify_codex_workspace.sh
```

Notes:

- The app runs on the host; PostgreSQL and Redis run in containers.
- Free OddsPortal ingestion is the default and needs no Odds API key.
- Quote paths. Project scripts derive and use their absolute repository root.
