#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[info] restarting frontend + backend"
"${SCRIPT_DIR}/restart_frontend.sh"
"${SCRIPT_DIR}/restart_backend.sh"
echo "[done] frontend and backend restarted"

