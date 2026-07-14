#!/usr/bin/env bash
# Comprehensive local gate aligned with CI for a bootstrapped Codex workspace.
# DB-backed migration checks run when a local .env is present; CI always runs
# them against throwaway services.

set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1; pwd -P)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" || {
  printf '%s\n' 'verify_codex_workspace: cannot resolve the Git repository root' >&2
  exit 1
}
python_bin="$repo_root/.venv/bin/python"

if [ ! -x "$python_bin" ]; then
  printf '%s\n' "Missing $python_bin; run scripts/bootstrap_codex.sh first." >&2
  exit 1
fi
for command_name in git uv docker node gitleaks jq; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$command_name" >&2
    exit 1
  fi
done

cd "$repo_root"

compose_env="$repo_root/.env.example"
if [ -f "$repo_root/.env" ]; then
  compose_env="$repo_root/.env"
  "$repo_root/.venv/bin/alembic" upgrade head
  "$repo_root/.venv/bin/alembic" check
else
  printf '%s\n' 'NOTE: .env absent; DB-backed migration verification is deferred to CI.' >&2
fi

docker compose \
  --env-file "$compose_env" \
  -f "$repo_root/docker-compose.yml" \
  config --quiet

"$python_bin" "$repo_root/tools/build_dashboard.py" --check
node --check "$repo_root/app/api/dashboard_src/app.js"
DASHQA_HTML="$repo_root/app/api/dashboard.html" \
DASHQA_MOCK_ONLY=1 \
DASHQA_OUT="${TMPDIR:-/tmp}/sharp-dashboard-qa" \
  "$python_bin" "$repo_root/scripts/dashboard_qa.py"

"$python_bin" -m pytest -n 2 --dist loadfile \
  --cov=app --cov-report=term-missing --cov-fail-under=85
"$python_bin" -m ruff check \
  "$repo_root/app" "$repo_root/tests" "$repo_root/scripts" \
  "$repo_root/alembic" "$repo_root/tools"
"$python_bin" -m ruff format --check \
  "$repo_root/app" "$repo_root/tests" "$repo_root/scripts" \
  "$repo_root/alembic" "$repo_root/tools"
"$python_bin" -m mypy "$repo_root/app" "$repo_root/tests"

uv export --all-extras --all-groups --no-hashes --no-emit-project \
  --output-file "${TMPDIR:-/tmp}/sharp-ev-picks-requirements-audit.txt" \
  >/dev/null
uvx pip-audit \
  -r "${TMPDIR:-/tmp}/sharp-ev-picks-requirements-audit.txt" \
  --no-deps --disable-pip

bash "$repo_root/scripts/safety_audit.sh"
gitleaks git "$repo_root" --pre-commit --no-banner --redact
while IFS= read -r -d '' relative_path; do
  gitleaks dir "$repo_root/$relative_path" --no-banner --redact
done < <(git -C "$repo_root" ls-files --others --exclude-standard -z)
gitleaks git "$repo_root" --pre-commit --staged --no-banner --redact
gitleaks git "$repo_root" --no-banner --redact
git -C "$repo_root" diff --check
git -C "$repo_root" diff --cached --check

printf '%s\n' 'Codex workspace verification passed.'
