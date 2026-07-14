#!/usr/bin/env bash
# Idempotent macOS/Linux/WSL bootstrap. It never imports, replaces, prints, or
# commits secret values. If a local .env already exists, it is preserved and
# consumed only by local Compose/Alembic commands.

set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: bootstrap_codex.sh [--check]

  --check  Validate prerequisites and repository Codex assets without changes.
  default  Sync the lock, install Chromium, and initialize local infrastructure
           only when an existing .env is already present.

Native Windows is not supported by these Bash hooks; use WSL.
USAGE
}

mode="bootstrap"
case "${1:-}" in
  "") ;;
  --check) mode="check" ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1; pwd -P)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" || {
  printf '%s\n' 'bootstrap_codex: cannot resolve the Git repository root' >&2
  exit 1
}

platform="$(uname -s)"
case "$platform" in
  Darwin|Linux) ;;
  *)
    printf 'Unsupported platform: %s. Use macOS, Linux, or WSL.\n' "$platform" >&2
    exit 1
    ;;
esac

required_commands=(git codex uv docker jq node gitleaks)
missing=0
for command_name in "${required_commands[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'MISSING: %s\n' "$command_name" >&2
    missing=1
  fi
done
trash_utility=""
for candidate in trash trash-put gio; do
  if command -v "$candidate" >/dev/null 2>&1; then
    trash_utility="$candidate"
    break
  fi
done
if [ -z "$trash_utility" ]; then
  printf '%s\n' 'MISSING: trash utility (trash, trash-put, or gio trash)' >&2
  missing=1
fi
if [ "$missing" -ne 0 ]; then
  printf '%s\n' 'Install the missing prerequisites, then rerun with --check.' >&2
  exit 1
fi

codex_version="$(codex --version | awk '{print $2}')"
if [[ ! "$codex_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
  printf 'Cannot parse Codex version: %s\n' "$codex_version" >&2
  exit 1
fi
codex_major="${BASH_REMATCH[1]}"
codex_minor="${BASH_REMATCH[2]}"
codex_patch="${BASH_REMATCH[3]}"
codex_too_old=0
if [ "$codex_major" -eq 0 ]; then
  if [ "$codex_minor" -lt 142 ]; then
    codex_too_old=1
  elif [ "$codex_minor" -eq 142 ]; then
    if [ "$codex_patch" -lt 5 ]; then
      codex_too_old=1
    fi
  fi
fi
if [ "$codex_too_old" -eq 1 ]; then
  printf 'Codex %s is too old; install 0.142.5 or newer.\n' "$codex_version" >&2
  exit 1
fi

required_paths=(
  "$repo_root/AGENTS.md"
  "$repo_root/.python-version"
  "$repo_root/.codex/config.toml"
  "$repo_root/.codex/hooks.json"
  "$repo_root/.codex/README.md"
  "$repo_root/.agents/skills"
  "$repo_root/.codex/agents"
  "$repo_root/docs/CODEX_DEVICE_HANDOFF.md"
  "$repo_root/tests/test_codex_workspace_contract.py"
  "$repo_root/.env.example"
  "$repo_root/pyproject.toml"
  "$repo_root/uv.lock"
)
for required_path in "${required_paths[@]}"; do
  if [ ! -e "$required_path" ]; then
    printf 'MISSING: %s\n' "$required_path" >&2
    exit 1
  fi
done

jq -e '
  type == "object"
  and (keys == ["hooks"])
  and (.hooks | type == "object")
  and (.hooks | keys == ["PostToolUse", "PreToolUse", "Stop"])
' "$repo_root/.codex/hooks.json" >/dev/null

grep -q '^hooks = true$' "$repo_root/.codex/config.toml"
grep -q '^multi_agent = true$' "$repo_root/.codex/config.toml"

hook_count=0
for hook_script in "$repo_root"/.codex/hooks/*.sh; do
  [ -f "$hook_script" ] || continue
  [ -x "$hook_script" ] || {
    printf 'NOT EXECUTABLE: %s\n' "$hook_script" >&2
    exit 1
  }
  bash -n "$hook_script"
  hook_count=$((hook_count + 1))
done
if [ "$hook_count" -ne 4 ]; then
  printf 'Expected 4 executable project hooks, found %s.\n' "$hook_count" >&2
  exit 1
fi

skill_count=0
for skill_file in "$repo_root"/.agents/skills/*/SKILL.md; do
  [ -f "$skill_file" ] || continue
  [ "$(head -n 1 "$skill_file")" = '---' ] || {
    printf 'INVALID SKILL FRONTMATTER: %s\n' "$skill_file" >&2
    exit 1
  }
  grep -q '^name:' "$skill_file"
  grep -q '^description:' "$skill_file"
  skill_count=$((skill_count + 1))
done

agent_count=0
for agent_file in "$repo_root"/.codex/agents/*.toml; do
  [ -f "$agent_file" ] || continue
  grep -q '^name = "' "$agent_file"
  grep -q '^description = "' "$agent_file"
  grep -q '^developer_instructions = """' "$agent_file"
  agent_count=$((agent_count + 1))
done

if [ "$skill_count" -ne 20 ]; then
  printf 'Expected 20 repository skills, found %s.\n' "$skill_count" >&2
  exit 1
fi
if [ "$agent_count" -ne 18 ]; then
  printf 'Expected 18 project agents, found %s.\n' "$agent_count" >&2
  exit 1
fi

jq empty "$repo_root/.codex/hooks.json"
git -C "$repo_root" diff --check
git -C "$repo_root" diff --cached --check
docker compose \
  --env-file "$repo_root/.env.example" \
  -f "$repo_root/docker-compose.yml" \
  config --quiet

printf 'Repository: %s\n' "$repo_root"
printf 'Platform: %s\n' "$platform"
printf 'Codex: %s\n' "$(codex --version)"
printf 'uv: %s\n' "$(uv --version)"
printf 'Docker Compose: %s\n' "$(docker compose version --short)"
printf 'Node: %s\n' "$(node --version)"
printf 'gitleaks: %s\n' "$(gitleaks version)"
printf 'Trash utility: %s\n' "$trash_utility"
printf 'Project assets: %s skills, %s agents, %s hooks\n' "$skill_count" "$agent_count" "$hook_count"

if [ "$mode" = "check" ]; then
  printf '%s\n' 'Codex workspace preflight passed.'
  exit 0
fi

cd "$repo_root"
uv sync --frozen --all-extras --all-groups
if [ "$platform" = "Linux" ]; then
  "$repo_root/.venv/bin/playwright" install --with-deps chromium
else
  "$repo_root/.venv/bin/playwright" install chromium
fi

if [ -f "$repo_root/.env" ]; then
  chmod 0600 "$repo_root/.env"
  docker compose \
    --env-file "$repo_root/.env" \
    -f "$repo_root/docker-compose.yml" \
    up -d --wait postgres redis
  "$repo_root/.venv/bin/alembic" upgrade head
else
  cat <<EOF_NEXT
Python and browser dependencies are ready.
Local infrastructure was not started because $repo_root/.env does not exist.
Create it from the safe template only when absent, keep mode 0600, and add
private values out of band:

  test -e "$repo_root/.env" || install -m 0600 "$repo_root/.env.example" "$repo_root/.env"
  bash "$repo_root/scripts/bootstrap_codex.sh"
EOF_NEXT
fi

"$repo_root/.venv/bin/python" -m pytest \
  "$repo_root/tests/test_codex_workspace_contract.py" \
  "$repo_root/tests/test_ci_contract.py" \
  -q

printf '%s\n' 'Bootstrap complete. Open Codex at the repository root and review /hooks.'
