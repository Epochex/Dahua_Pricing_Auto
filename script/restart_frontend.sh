#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"
WEB_SERVICE="${FRONTEND_WEB_SERVICE:-nginx.service}"

run_cmd() {
  if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

echo "[info] building frontend in ${FRONTEND_DIR}"
cd "${FRONTEND_DIR}"
npm run build

echo "[info] restarting web service: ${WEB_SERVICE}"
run_cmd systemctl restart "${WEB_SERVICE}"
echo "[info] service state:"
run_cmd systemctl is-active "${WEB_SERVICE}"
echo "[info] service status:"
run_cmd systemctl status "${WEB_SERVICE}" --no-pager | sed -n '1,20p'

