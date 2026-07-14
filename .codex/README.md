# Project Codex configuration

This directory is portable across macOS, Linux, and WSL and is versioned. It contains only repository-scoped
configuration—never user auth, MCP credentials, session state, or secret values.

## Discovery

- Root instructions: `../AGENTS.md`
- Project skills: `../.agents/skills/*/SKILL.md`
- Specialist agents: `agents/*.toml`
- Lifecycle hooks: `hooks.json` and `hooks/*.sh`
- Project feature/concurrency settings: `config.toml`

Codex discovers the skills and agents directly from the clone. Do not copy or
symlink them into a new device's home directory.

## New device

```bash
bash scripts/bootstrap_codex.sh --check
bash scripts/bootstrap_codex.sh
```

Use WSL rather than native Windows. Start Codex from the repository root,
mark the project trusted, and inspect
`/hooks`. Hook trust is intentionally per device. GitHub plugin/MCP login is
also per device; never copy `~/.codex/config.toml` or auth state.

## Validation

```bash
bash scripts/verify_codex_workspace.sh
```

See `../docs/CODEX_DEVICE_HANDOFF.md` for the exact continuation branch,
non-Git state, and production boundary.
