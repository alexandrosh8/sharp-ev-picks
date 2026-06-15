---
name: python-fastapi
description: "Project Python/FastAPI conventions. Use when writing or reviewing any code under app/ — module boundaries, pydantic v2, async patterns, and uv workflow."
allowed_tools:
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Bash
---

# Python / FastAPI Conventions

## Purpose

Keep `app/` consistent: pure math isolated from IO, typed everywhere,
async-first, configured only at the composition root.

## Procedure

1. Place code by boundary: pure math → `app/probabilities|edge|risk`
   (numpy/stdlib only, no env/DB/HTTP/log side effects); IO →
   `app/ingestion|storage|notifications|api`.
2. Settings: read env ONLY in `app/config.py` (pydantic-settings). Pure
   modules receive frozen policy dataclasses built in `app/main.py` /
   `app/scheduler.py`.
3. Models: pydantic v2, `model_config = ConfigDict(frozen=True,
extra="forbid")` for internal models; `extra="ignore"` for upstream
   payloads; UTC-aware datetime validators.
4. Async: `httpx.AsyncClient` with explicit timeouts; async SQLAlchemy
   sessions; CPU-bound work via `asyncio.to_thread`/executors.
5. Routes thin: validate → delegate to service function → typed response
   model. No business logic in route bodies.
6. Run everything through uv: `uv sync`, `uv run pytest -q`,
   `.venv/bin/python -m ...` in scripts.

## Checklist

- [ ] Type hints on every function; mypy clean
- [ ] No `os.environ` outside app/config.py
- [ ] No IO imports inside pure-math packages
- [ ] pathlib (never os.path); f-strings; no bare except

## Gotchas

- **The project path contains a space** — shell snippets must quote paths;
  prefer `uv run` from the project root.
- **pydantic v2 `frozen=True` raises on mutation, not silently ignores** —
  build new instances with `model_copy(update=...)`.
- **`asyncio` + APScheduler**: jobs must be `async def` registered on the
  running loop's scheduler; a sync job will block every other poll.
- **Decimal vs float**: odds/money cross the API boundary as Decimal/str;
  floats are acceptable only inside numpy math kernels.

## Forbidden mistakes

- Adding an endpoint or job that could place a bet (decision-support only).
- Blocking IO (requests, time.sleep) inside the event loop.
- Module-level side effects (network/DB at import time).
