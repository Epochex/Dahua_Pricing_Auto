#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[info] restarting backend + frontend"
"${SCRIPT_DIR}/restart_backend.sh"
echo "[info] backend is healthy; publishing frontend"
"${SCRIPT_DIR}/restart_frontend.sh"
echo "[done] frontend and backend restarted"
