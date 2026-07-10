#!/usr/bin/env bash
# Dashboard QA harness — Playwright sweep of the SignalDesk dashboard (GET /).
#
# Read-only against the live app (browser GETs only; safe while serving
# traffic). The dev container cannot run browsers (missing system libs) and
# cannot bind-mount its own paths (host-side docker daemon), so this spins up
# a throwaway mcr.microsoft.com/playwright/python container on the app's
# compose network, pipes scripts/dashboard_qa.py into it over stdin, and
# copies /out back with `docker cp`.
#
# Usage: bash scripts/dashboard_qa.sh [output-dir]   (default ./dashboard-qa-out)
# Exit: nonzero if any tab fails to render / has an empty root panel, any
#       console error or pageerror occurs, or 390px horizontal overflow.
#
# Project rules: no `&&` chains; no bare `rm`; all paths quoted.

set -u

OUT_DIR="${1:-./dashboard-qa-out}"
APP_CONTAINER="${DASHQA_APP_CONTAINER:-betting-ai-app-1}"
IMAGE="mcr.microsoft.com/playwright/python:v1.49.0-noble"
QA_NAME="dashqa-$$-$(date +%s)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1; pwd)"
SWEEP="${SCRIPT_DIR}/dashboard_qa.py"
if [ ! -f "${SWEEP}" ]; then
    echo "ERROR: sweep script not found: ${SWEEP}" >&2
    exit 2
fi

# Auto-detect the compose network from the running app container; fall back
# to betting-ai_default.
NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "${APP_CONTAINER}" 2>/dev/null | grep -m1 'betting-ai' || true)"
if [ -z "${NET}" ]; then
    NET="betting-ai_default"
    echo "WARN: could not detect network from ${APP_CONTAINER}; using ${NET}" >&2
fi
echo "network: ${NET}"
echo "output:  ${OUT_DIR}"

mkdir -p "${OUT_DIR}" || exit 2

cleanup() {
    docker rm -f "${QA_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "${QA_NAME}" --network "${NET}" "${IMAGE}" sleep 600 >/dev/null || exit 2
docker exec "${QA_NAME}" pip install -q "playwright==1.49.0" || exit 2
docker exec "${QA_NAME}" mkdir -p /out || exit 2

# Run the sweep (stdin-piped: no bind mounts available from this container).
docker exec -i \
    -e "DASHQA_BASE_URL=${DASHQA_BASE_URL:-http://app:8000}" \
    -e "DASHQA_OUT=/out" \
    "${QA_NAME}" python - < "${SWEEP}"
RC=$?

# Copy artifacts back even on failure (screenshots aid diagnosis).
docker cp "${QA_NAME}:/out/." "${OUT_DIR}/" || echo "WARN: docker cp of artifacts failed" >&2

echo ""
echo "report: ${OUT_DIR}/report.txt"
if [ "${RC}" -ne 0 ]; then
    echo "DASHBOARD QA: FAIL (exit ${RC})" >&2
else
    echo "DASHBOARD QA: PASS"
fi
exit "${RC}"
