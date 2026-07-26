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

# OCI image metadata (repo hygiene; no licenses label — repo is private).
LABEL org.opencontainers.image.title="betting-ai" \
      org.opencontainers.image.description="Manual-betting +EV picks decision-support — picks-only, read-only market data, never places bets." \
      org.opencontainers.image.source="https://github.com/alexandrosh8/sharp-ev-picks" \
      org.opencontainers.image.vendor="betting-ai"

# PLAYWRIGHT_BROWSERS_PATH is mandatory: without it the browser installs to
# /root/.cache/ms-playwright and is INVISIBLE to the non-root appuser at
# runtime ("Executable doesn't exist" only when the first scrape launches).
ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# uv pinned for reproducible builds — bump in lockstep with
# scripts/upgrade_deps.sh when the project uv version moves.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv

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
# Value-filter ML artifacts (ADOPT'd v1) — baked into the image because bind
# mounts resolve on the Docker daemon's host fs, not this checkout
# (.dockerignore whitelists exactly these two files out of data/).
COPY data/ml/value_filter_manifest.json data/ml/value_filter_model.txt ./data/ml/
# --extra ml: lightgbm + pandas so the baked value-filter artifacts actually
# score (without it the loader logs 'lightgbm is not installed' and disables).
# libgomp1 is lightgbm's OpenMP runtime — absent from slim base images; without
# it the import raises OSError(libgomp.so.1) at startup.
RUN apt-get update; apt-get install -y --no-install-recommends libgomp1; rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen --no-dev --extra football --extra backfill --extra ml
# The deterministic builder enforces this mode too. Keep a build-layer guard
# so an older/local 0600 artifact can never make the root-owned dashboard
# unreadable by the unprivileged runtime process.
RUN chmod 0644 /srv/betting-ai/app/api/dashboard.html

# Non-root runtime. Application code and the virtualenv deliberately stay
# root-owned: appuser can read/execute them but cannot persist a page exploit or
# dependency mutation. app.ingestion.oddsportal removes upstream `--no-sandbox`
# and disabled-isolation flags on every scrape admission; compose supplies the
# Playwright seccomp profile needed for Chromium's user-namespace sandbox.
RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

# Entrypoint runs `alembic upgrade head` (idempotent) then execs uvicorn via
# the venv binaries directly — NEVER `uv run` without --no-sync here: a plain
# `uv run` re-syncs the venv at container start and would UNINSTALL the
# build-time extras (uv sync removes packages not requested).
# Process liveness against the unauthenticated /live endpoint, via the venv
# python (slim base has no curl). Dependency and poll readiness deliberately
# live on /ready and must not make Docker kill-loop during a long first scrape.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["/srv/betting-ai/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/live', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["bash", "/srv/betting-ai/scripts/docker_entrypoint.sh"]
