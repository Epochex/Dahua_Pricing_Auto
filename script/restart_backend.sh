#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="${BACKEND_SERVICE:-dahua-pricing-backend.service}"

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
echo "[info] service state:"
run_cmd systemctl is-active "${SERVICE_NAME}"
echo "[info] service status:"
run_cmd systemctl status "${SERVICE_NAME}" --no-pager | sed -n '1,20p'
