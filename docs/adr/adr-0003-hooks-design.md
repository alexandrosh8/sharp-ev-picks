# ADR-0003: Project Hooks Design

- **Status:** accepted
- **Date:** 2026-06-10
- **Deciders:** GodFather (Alexis) + Claude

## Context

The master requirements ask for safe hooks: pre-command safety review, secret
scanning before commit, formatting after edits, test execution after code
changes, memory updates after major decisions, and dependency review before
install. Global hooks already provide prettier formatting (PostToolUse) and
bash-history logging (PreToolUse). The project path contains a space, so every
hook command must quote `"$CLAUDE_PROJECT_DIR"`.

## Decision

Five project-local hooks in `.claude/hooks/`, wired via project
`.claude/settings.json`:

| Hook                        | Event                            | Behavior                                                                                                                                                                                                | Failure mode                                         |
| --------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| `pre_bash_guard.sh`         | PreToolUse(Bash)                 | Block (exit 2): `rm -rf`, bare `rm`, `curl\|sh`, `wget\|sh`, `pip install <URL>`, `&&` chains, force-push to main. Warn only: `uv add`/`pip install` (dependency-review reminder), redirect into `.env` | fail closed on listed patterns                       |
| `pre_commit_secret_scan.sh` | PreToolUse(Bash) on `git commit` | `gitleaks git --pre-commit --staged`; leaks → exit 2                                                                                                                                                    | **fail closed** (gitleaks missing → warn, fail open) |
| `post_edit_format.sh`       | PostToolUse(Write\|Edit)         | `uvx ruff format` on `*.py` only                                                                                                                                                                        | best-effort (`\|\| true`)                            |
| `post_edit_pytest.sh`       | PostToolUse(Write\|Edit)         | run pytest only when the edited file matches `app/.*\.py` or `tests/.*\.py` AND `.venv` + `tests/` exist; 120 s timeout                                                                                 | non-blocking (PostToolUse cannot block)              |
| `stop_memory_reminder.sh`   | Stop                             | print reminder to update `.claude/memory/` + `docs/adr/`                                                                                                                                                | informational                                        |

Stays **manual** (documented in CLAUDE.md, not hooked): ADR authoring, memory
content writing, `git commit -m "checkpoint"` before refactors, full pre-merge
test runs.

## Justification

Hooks only block what is mechanically checkable and always wrong (destructive
shell, leaked secrets). Judgment calls (dependencies, memory content) get
reminders, not automation — per the "safe hooks only, no dangerous
auto-execution" requirement.

## Alternatives considered

- Wiring tdd-guard/husky pre-commit — rejected: Node toolchain for a Python
  repo; gitleaks via Claude hook covers the secret gate with less machinery.
- Auto-running mypy on every edit — rejected: too slow per-edit; runs in CI
  and on demand instead.

## Consequences

- Commits with secrets are blocked at the tool layer before git history exists.
- Test feedback arrives within one edit cycle once `tests/` exists; zero noise
  before Phase D (the hook no-ops when paths don't match).
