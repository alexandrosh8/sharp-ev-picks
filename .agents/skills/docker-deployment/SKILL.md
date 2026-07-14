---
name: docker-deployment
description: "Docker and deployment conventions. Use when changing docker-compose.yml, Dockerfile, CI infrastructure, or the deployed Ubuntu production stack."
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

Keep local Compose infrastructure and the deployed Ubuntu production stack
reproducible, least-privileged, migration-safe, and free of secret leakage.

## Procedure

1. Local dev: `docker compose up -d --wait postgres redis`; run the app on the
   host with `.venv/bin/python -m uvicorn ...` against loopback ports.
2. Compose: PostgreSQL 16 + Redis 7 healthchecks, named volumes,
   `restart: unless-stopped`, bounded logs, and loopback-only published ports.
3. Dockerfile: `python:3.12-slim`, pinned uv, frozen production extras,
   Playwright Chromium with Linux dependencies, and non-root `appuser`.
4. Preserve `scripts/docker_entrypoint.sh`: it runs Alembic migrations before
   execing uvicorn. Never replace it with a direct Dockerfile CMD.
5. Production is already deployed on Ubuntu. Preserve the server-side `.env`
   in place, build/recreate only intended services, and keep a rollback target.
6. Verify with `docker compose --env-file .env.example config --quiet`, image
   build/smoke tests, migration head, health endpoints, and the safety audit.

## Checklist

- [ ] Compose configuration and service healthchecks pass
- [ ] No secrets in images, compose, Git, or logs
- [ ] Runtime user remains `appuser`; application/venv stay root-owned
- [ ] Entrypoint migration ordering and one-app-instance invariant remain intact
- [ ] PostgreSQL, Redis, and app ports remain loopback-only in production
- [ ] No macOS-only runtime paths or mechanisms enter production

## Gotchas

- Host tools dial loopback ports; containers dial Compose service names. Keep
  the intentional Compose URL override rather than hard-coding either shape.
- Docker dependency layers copy `pyproject.toml` + `uv.lock` before source and
  use frozen syncs so source edits do not invalidate dependency layers.
- The in-process scheduler and exposure ledger require exactly one app replica;
  never scale the app service horizontally without redesigning those contracts.
- Chromium requires the hardened seccomp profile and must retain its process
  sandbox; never restore upstream `--no-sandbox` switches.

## Forbidden mistakes

- Baking `.env` or credentials into an image layer.
- Replacing, deleting, printing, or committing the server `.env`.
- Exposing PostgreSQL/Redis publicly or binding the production app directly to
  a public interface.
- Skipping migrations, health checks, or rollback preparation during deploy.
