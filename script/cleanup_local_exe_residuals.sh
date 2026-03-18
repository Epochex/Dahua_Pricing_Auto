#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${ROOT_DIR}/.cleanup_backup"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/exe_cleanup_${STAMP}"
MODE="dry-run"

if [[ "${1:-}" == "--apply" ]]; then
  MODE="apply"
fi

echo "[info] repo: ${ROOT_DIR}"
echo "[info] mode: ${MODE} (use --apply to execute)"

TARGET_FILES=(
  "gui_app.py"
  "main.py"
  "config.py"
  "export.py"
  "readme_bak.md"
)

TARGET_GLOBS=(
  "*.spec"
  "*.exe"
)

REQ_FILE="${ROOT_DIR}/requirements.txt"

print_remove() {
  local p="$1"
  if [[ "${MODE}" == "apply" ]]; then
    mkdir -p "${BACKUP_DIR}"
    if [[ -f "${ROOT_DIR}/${p}" ]]; then
      cp -a "${ROOT_DIR}/${p}" "${BACKUP_DIR}/"
    fi
    rm -f "${ROOT_DIR}/${p}"
    echo "[removed] ${p}"
  else
    echo "[dry-run] would remove: ${p}"
  fi
}

print_update_req() {
  if [[ ! -f "${REQ_FILE}" ]]; then
    return 0
  fi
  if ! grep -qiE '^[[:space:]]*pyinstaller([<>=!~].*)?[[:space:]]*$' "${REQ_FILE}"; then
    echo "[info] requirements.txt has no pyinstaller line"
    return 0
  fi

  if [[ "${MODE}" == "apply" ]]; then
    mkdir -p "${BACKUP_DIR}"
    cp -a "${REQ_FILE}" "${BACKUP_DIR}/requirements.txt"
    awk '
      BEGIN { IGNORECASE=1 }
      $0 ~ /^[[:space:]]*pyinstaller([<>=!~].*)?[[:space:]]*$/ { next }
      { print }
    ' "${REQ_FILE}" > "${REQ_FILE}.tmp"
    mv "${REQ_FILE}.tmp" "${REQ_FILE}"
    echo "[updated] requirements.txt (removed pyinstaller dependency)"
  else
    echo "[dry-run] would update requirements.txt (remove pyinstaller dependency line)"
  fi
}

echo "[step] scanning fixed target files"
for rel in "${TARGET_FILES[@]}"; do
  if [[ -f "${ROOT_DIR}/${rel}" ]]; then
    print_remove "${rel}"
  fi
done

echo "[step] scanning root-level exe packaging artifacts"
for pat in "${TARGET_GLOBS[@]}"; do
  while IFS= read -r path; do
    rel="${path#${ROOT_DIR}/}"
    print_remove "${rel}"
  done < <(find "${ROOT_DIR}" -maxdepth 1 -type f -name "${pat}" | sort)
done

echo "[step] sanitizing requirements.txt"
print_update_req

if [[ "${MODE}" == "apply" ]]; then
  echo "[done] cleanup applied"
  echo "[info] backup dir: ${BACKUP_DIR}"
else
  echo "[done] dry-run only, no files changed"
fi

