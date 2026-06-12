# Manual-betting +EV picks platform — decision-support only, never places bets.
#
# Base choice: python:3.12-slim + `playwright install --with-deps chromium`
# (NOT mcr.microsoft.com/playwright/python). The mcr tag must match the
# playwright version pinned in uv.lock EXACTLY (1.60.0 today) and would need a
# lockstep bump every time scripts/upgrade_deps.sh moves oddsharvester /
# playwright. Installing the browser from the synced venv's own playwright CLI
# keeps the browser version coupled to uv.lock automatically.
#
# Arch: compose builds from source on the target host, so the image is native
# linux/amd64 on the Ubuntu VPS and native linux/arm64 on a Mac — Playwright
# publishes Linux Chromium for both. No cross-build needed unless images are
# ever pre-built on a Mac and pushed (then: docker buildx --platform linux/amd64).
FROM python:3.12-slim

# PLAYWRIGHT_BROWSERS_PATH is mandatory: without it the browser installs to
# /root/.cache/ms-playwright and is INVISIBLE to the non-root appuser at
# runtime ("Executable doesn't exist" only when the first scrape launches).
ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv/betting-ai

# Dependency layer first (cache-friendly). The extras are REQUIRED in
# production: ODDS_SOURCE=oddsportal (the default) imports oddsharvester
# (backfill extra) lazily per poll cycle, and the football model imports
# penaltyblog (football extra) — without them the app starts cleanly and then
# every cycle dies with ModuleNotFoundError.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra football --extra backfill

# Chromium + its Linux shared libraries, version-matched to the playwright in
# uv.lock. Must run as root (apt) and AFTER the dependency sync (needs the
# venv's playwright CLI). a+rX so the non-root runtime user can read/execute.
RUN /srv/betting-ai/.venv/bin/playwright install --with-deps chromium
RUN chmod -R a+rX /ms-playwright

# Application code (README.md is project metadata — hatchling needs it to
# build the wheel during the second sync).
COPY README.md alembic.ini ./
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
RUN uv sync --frozen --no-dev --extra football --extra backfill

# Non-root user. Chromium sandbox note: running headless Chromium as non-root
# normally needs sandbox configuration, but oddsharvester 0.3.0 detects Docker
# via /.dockerenv (utils/utils.py is_running_in_docker) and launches Chromium
# with `--no-sandbox --disable-dev-shm-usage` (utils/constants.py
# PLAYWRIGHT_BROWSER_ARGS_DOCKER), so no shm_size hack or seccomp profile is
# required. RE-VERIFY this upstream behavior whenever oddsharvester is bumped
# past 0.3.0 (scripts/upgrade_deps.sh).
RUN useradd --create-home --uid 1000 appuser
RUN chown -R appuser:appuser /srv/betting-ai
USER appuser

EXPOSE 8000

# Entrypoint runs `alembic upgrade head` (idempotent) then execs uvicorn via
# the venv binaries directly — NEVER `uv run` without --no-sync here: a plain
# `uv run` re-syncs the venv at container start and would UNINSTALL the
# build-time extras (uv sync removes packages not requested).
ENTRYPOINT ["bash", "/srv/betting-ai/scripts/docker_entrypoint.sh"]
