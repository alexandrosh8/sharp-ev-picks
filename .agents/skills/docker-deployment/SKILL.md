---
name: docker-deployment
description: "Docker and deployment conventions. Use when changing docker-compose.yml, Dockerfile, CI infrastructure, or preparing the Ubuntu/OpenClaw deployment."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Docker Deployment

## Purpose

One container story that runs identically on the Mac dev machine and the
Ubuntu/OpenClaw production VPS.

## Procedure

1. Local dev: `docker compose up -d postgres redis`; the app runs on the
   host via `uv run` against compose-exposed localhost ports.
2. Compose services: postgres:16-alpine + redis:7-alpine, healthchecks
   (pg_isready / redis-cli ping), named volumes, `env_file: .env`,
   `restart: unless-stopped`, ports bound to 127.0.0.1.
3. Dockerfile: python:3.11-slim, `ENV TZ=UTC`, non-root `app` user,
   uv-based dependency install (copy pyproject + lock first for layer
   caching), `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0"]`.
4. Production (Ubuntu): full compose (app + postgres + redis), secrets via
   `.env` on the host (0600), logs to stdout collected by Docker.
5. Verify every change with `docker compose config -q` before commit.
6. Deployment runbooks in `docs/deployment/` list commands one per line
   (no `&&` chains).

## Checklist

- [ ] `docker compose config -q` passes
- [ ] No secrets in images, compose, or CI logs
- [ ] Healthchecks present; restart policy set
- [ ] Works without any macOS-specific path or service

## Gotchas

- **The project directory name contains a space** — compose project name
  derives from it; set `name: betting-ai` at the top of compose to avoid
  volume/network names with spaces.
- **asyncpg connects to `localhost` from the host but `postgres` (service
  name) from inside the app container** — keep DATABASE_URL host
  overridable via env, don't hardcode either.
- **uv inside Docker**: copy `pyproject.toml` + `uv.lock` and run
  `uv sync --frozen` before copying source, or every code change busts the
  dependency layer.
- **OpenClaw coexistence**: bind app/postgres/redis ports to localhost and
  pick non-default host ports if OpenClaw already claims them.

## Forbidden mistakes

- Baking `.env` or any credential into an image layer.
- Exposing postgres/redis publicly in production compose.
- macOS-only mechanisms (launchd, /Users paths) in production configs.
