#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${BACKEND_SERVICE:-dahua-pricing-backend.service}"
READY_URL="${BACKEND_READY_URL:-http://127.0.0.1:8000/api/meta}"
READY_TIMEOUT="${BACKEND_READY_TIMEOUT:-90}"

run_cmd() {
  if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

echo "[info] preparing price data before backend restart"
"${SCRIPT_DIR}/prepare_price_data.sh"

echo "[info] restarting backend service: ${SERVICE_NAME}"
run_cmd systemctl restart "${SERVICE_NAME}"

echo "[info] waiting for backend readiness: ${READY_URL}"
deadline=$((SECONDS + READY_TIMEOUT))
while (( SECONDS < deadline )); do
  if response="$(curl --fail --silent --show-error --max-time 5 "${READY_URL}" 2>/dev/null)" \
    && grep -q '"loaded":true' <<<"${response}"; then
    echo "[info] backend is ready"
    break
  fi
  sleep 1
done

if (( SECONDS >= deadline )); then
  echo "[error] backend did not become ready within ${READY_TIMEOUT}s"
  run_cmd systemctl status "${SERVICE_NAME}" --no-pager || true
  run_cmd journalctl -u "${SERVICE_NAME}" -n 100 --no-pager || true
  exit 1
fi

echo "[info] service state:"
run_cmd systemctl is-active "${SERVICE_NAME}"
echo "[info] service status:"
run_cmd systemctl status "${SERVICE_NAME}" --no-pager | sed -n '1,20p'
