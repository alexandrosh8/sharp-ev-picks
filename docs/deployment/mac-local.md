# Mac Local Development

Prereqs: uv, Docker (colima or Docker Desktop), gitleaks.

```bash
cd "/Users/alexis/code/Betting Picks Bot"
cp .env.example .env
chmod 600 .env
```

Start infrastructure (host ports 5433/6380 — 5432/6379 belong to the
weatherbot project):

```bash
colima start
docker compose up -d postgres redis
```

Install and migrate:

```bash
uv sync
uv run alembic upgrade head
```

Run:

```bash
uv run uvicorn app.main:app --reload
```

Verify:

```bash
curl -s http://127.0.0.1:8000/health
uv run pytest
uvx ruff check app tests
uv run mypy app
bash scripts/safety_audit.sh
gitleaks dir . --no-banner
```

Notes:

- The app runs on the host in dev; only postgres/redis are containerized.
- Without Odds API keys in `.env` the poll job does not schedule (logged) —
  everything else works.
- The project path contains a space: quote it in every shell command.
