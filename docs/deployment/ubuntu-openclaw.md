# Moved

This runbook was superseded when the app was containerized (entrypoint
migrations, healthchecks, resource caps, profile switch, backup cron).

**Canonical runbook: [openclaw-ubuntu.md](openclaw-ubuntu.md)** — do not
follow older copies of the instructions that used to live here (notably the
manual `docker compose exec app uv run alembic upgrade head` step, which is
now automatic on boot, and the `docker-compose-plugin` apt package, which
does not exist in stock Ubuntu repos).
