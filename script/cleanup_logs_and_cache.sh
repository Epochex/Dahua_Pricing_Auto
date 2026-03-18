#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${DAHUA_PRICING_RUNTIME_DIR:-/data/dahua_pricing_runtime}"
MODE="dry-run"

if [[ "${1:-}" == "--apply" ]]; then
  MODE="apply"
fi

run_cmd() {
  if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

echo "[info] repo: ${ROOT_DIR}"
echo "[info] runtime: ${RUNTIME_DIR}"
echo "[info] mode: ${MODE} (use --apply to execute)"

cleanup_dir_contents() {
  local dir="$1"
  local label="$2"
  if [[ ! -d "${dir}" ]]; then
    echo "[skip] ${label}: ${dir} not found"
    return 0
  fi

  if [[ "${MODE}" == "apply" ]]; then
    run_cmd find "${dir}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "[cleaned] ${label}: ${dir}"
  else
    local cnt
    cnt="$(find "${dir}" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')"
    echo "[dry-run] would clean ${label}: ${dir} (entries=${cnt})"
  fi
}

cleanup_path() {
  local path="$1"
  local label="$2"
  if [[ ! -e "${path}" ]]; then
    return 0
  fi
  if [[ "${MODE}" == "apply" ]]; then
    run_cmd rm -rf "${path}"
    echo "[removed] ${label}: ${path}"
  else
    echo "[dry-run] would remove ${label}: ${path}"
  fi
}

echo "[step] runtime logs and generated cache-like data"
cleanup_dir_contents "${RUNTIME_DIR}/logs" "runtime logs"
cleanup_dir_contents "${RUNTIME_DIR}/outputs" "runtime outputs cache"
cleanup_dir_contents "${RUNTIME_DIR}/uploads" "runtime uploads cache"

echo "[step] frontend cache/build artifacts"
cleanup_path "${ROOT_DIR}/frontend/.vite" "frontend vite cache"
cleanup_path "${ROOT_DIR}/frontend/dist" "frontend build dist"

echo "[step] python test/lint caches"
cleanup_path "${ROOT_DIR}/.pytest_cache" "pytest cache"
cleanup_path "${ROOT_DIR}/.mypy_cache" "mypy cache"
cleanup_path "${ROOT_DIR}/.ruff_cache" "ruff cache"

echo "[step] __pycache__ directories (excluding .git and frontend/node_modules)"
while IFS= read -r p; do
  cleanup_path "${p}" "__pycache__"
done < <(
  find "${ROOT_DIR}" -type d -name "__pycache__" \
    -not -path "${ROOT_DIR}/.venv/*" \
    -not -path "${ROOT_DIR}/.git/*" \
    -not -path "${ROOT_DIR}/frontend/node_modules/*" \
    | sort
)

echo "[step] loose .log files under repo (excluding .git and frontend/node_modules)"
while IFS= read -r p; do
  cleanup_path "${p}" "log file"
done < <(
  find "${ROOT_DIR}" -type f -name "*.log" \
    -not -path "${ROOT_DIR}/.git/*" \
    -not -path "${ROOT_DIR}/frontend/node_modules/*" \
    | sort
)

if [[ "${MODE}" == "apply" ]]; then
  echo "[done] cleanup applied"
else
  echo "[done] dry-run only, no files changed"
fi
