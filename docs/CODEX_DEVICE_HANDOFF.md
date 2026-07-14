# Codex device handoff — sharp-ev-picks

Living continuation record. Update this file when the source branch, production
revision, bootstrap contract, or unresolved work changes.

## Source of truth

- Repository: `https://github.com/alexandrosh8/sharp-ev-picks`
- Continuation branch: `chore/codex-device-handoff`
- Audited application base: `e800f94e8a076c4c6516e0b43d33f83eaa0fcc48`
  (`fix/full-audit-remediation`)
- `origin/main` is behind this application base. Do not resume from `main`
  unless the continuation branch has been squash-merged there.
- Production at `https://cactusbets.cloud/` was healthy on the audited
  application base as of 2026-07-14. This handoff commit changes local Codex
  portability/configuration, not application runtime behavior.

## New-device bootstrap

On macOS, Linux, or WSL, install Codex 0.142.5 or newer plus `git`, `uv`,
Docker Compose, `jq`, Node.js, `gitleaks`, and a trash utility (`trash`,
`trash-put`, or `gio trash`). On Debian/Ubuntu/WSL, install `trash-cli`; older
macOS systems can install `trash` with Homebrew. Native Windows requires WSL.
Then:

```bash
git clone --branch chore/codex-device-handoff \
  https://github.com/alexandrosh8/sharp-ev-picks.git
cd sharp-ev-picks
bash scripts/bootstrap_codex.sh --check
bash scripts/bootstrap_codex.sh
```

The repository already contains every required project skill under
`.agents/skills/` and every specialist agent under `.codex/agents/`. Codex
discovers them from the clone; there is no global skill-copy step.

The bootstrap deliberately does not create or import `.env`. For local-only
development, create it from the safe template and add private values out of
band:

```bash
test -e .env || install -m 0600 .env.example .env
bash scripts/bootstrap_codex.sh
bash scripts/verify_codex_workspace.sh
```

Open Codex at the repository root, trust the project, and inspect `/hooks`.
Hook trust is per device. The portable commands resolve the repository at
runtime; no committed config contains a user-home path.

## GitHub MCP/plugin on the new device

Authenticate independently on the new device. If the curated GitHub plugin is
not already installed:

```bash
codex plugin add github@openai-curated
```

Complete its login through Codex. Never copy the old device's
`~/.codex/config.toml`, auth files, credential helper output, session database,
or logs. Repository research must detect and use whichever authenticated GitHub
connector/MCP is actually available.

## Read in this order

1. `AGENTS.md` — non-negotiable project rules and routing.
2. `docs/architecture.md` — current system flow and boundaries.
3. `README.md` — product status and evidence caveats.
4. `docs/adr/adr-0019-sharp-vs-soft-optimization-preregistration.md` and
   `docs/adr/adr-0022-evidence-scoped-tiering.md` — sharp/soft and spent-holdout
   contracts.
5. `docs/adr/adr-0026-portable-codex-workspace.md` — hook and device-portability decision.
6. `.claude/memory/MEMORY.md` — tracked cross-harness memory index.
7. Relevant `.agents/skills/*/SKILL.md` before changing that subsystem.

`docs/HANDOFF-2026-07-03.md` is an immutable historical snapshot. Its branch,
paths, credentials workflow, and deployment assumptions are superseded.

## Rebuilt versus transferred state

Rebuild from Git:

- `.venv` through the frozen uv lock.
- Playwright Chromium through the bootstrap.
- PostgreSQL/Redis local containers and schema migrations.
- Generated dashboard verification through `tools/build_dashboard.py --check`.

Transfer only through an approved private channel when actually required:

- local research datasets and model artifacts under ignored `data/`/`models/`;
- database backups;
- service/API credentials.

Never put any of these in GitHub:

- `.env` or production configuration values;
- SSH hosts/users/passwords/private keys;
- API, Telegram, Betfair, dashboard, proxy, database, or GitHub credentials;
- Codex user config/auth/session/log state;
- production databases, backups, datasets, or model binaries.

The AH 2425/2526 one-shot consumption record is protocol metadata, not a data
artifact, and is versioned at
`docs/backtesting/consumption/ah-2425-2526.json`. It must remain present so a
fresh clone cannot accidentally reuse a spent evaluation domain.

## Current application shape

- Picks-only/manual betting; automatic bet execution is structurally forbidden.
- FastAPI + async SQLAlchemy/PostgreSQL + Redis + APScheduler.
- OddsPortal/OddsHarvester and other read-only ingestion; allowlisted read-only
  Betfair JSON-RPC operations are the only POST transport exception.
- Sharp/soft value pipeline with provenance, liquidity/freshness gates,
  fractional-Kelly informational sizing, settlement, ROI, and CLV.
- Deterministically built self-contained dashboard with browser QA.
- HTML/embedded-JSON ingestion, dashboard, and sharp/soft work have dedicated
  repository skills and Codex agents.

## Production boundary

Production secrets remain only in the server-side `.env` (mode 0600). Any
future deploy must:

1. make a server-side backup/checkpoint;
2. preserve `.env` in place—never replace the deploy directory blindly;
3. build/recreate only the intended services;
4. allow the entrypoint to run Alembic migrations;
5. verify migration head, `/live`, authenticated readiness/health detail, logs,
   and the public dashboard;
6. retain an immediate rollback target.

Use `docs/deployment/` for commands. Host addresses and credentials are
intentionally absent from this repository.

## Resume checklist

```bash
git status --short --branch
git log --oneline --decorate -12
bash scripts/bootstrap_codex.sh --check
bash scripts/verify_codex_workspace.sh
```

Before new work: create a feature branch from the continuation branch, keep one
concern per commit, and never commit before the relevant tests and security
gates pass.
