#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"
WEB_SERVICE="${FRONTEND_WEB_SERVICE:-nginx.service}"
RELEASES_DIR="${FRONTEND_DIR}/releases"
RELEASE_ID="$(date +%Y%m%dT%H%M%S)-$$"
RELEASE_DIR="${RELEASES_DIR}/${RELEASE_ID}"
CURRENT_LINK="${FRONTEND_DIR}/current"
NEXT_LINK="${FRONTEND_DIR}/.current.next.$$"

run_cmd() {
  if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

cleanup_failed_release() {
  rm -f -- "${NEXT_LINK}"
  if [[ -d "${RELEASE_DIR}" ]] && [[ "$(readlink -f "${CURRENT_LINK}" 2>/dev/null || true)" != "${RELEASE_DIR}" ]]; then
    rm -rf -- "${RELEASE_DIR}"
  fi
}
trap cleanup_failed_release EXIT

echo "[info] building frontend release in ${RELEASE_DIR}"
mkdir -p "${RELEASES_DIR}"
cd "${FRONTEND_DIR}"
npm run build -- --outDir "${RELEASE_DIR}" --emptyOutDir

if [[ ! -s "${RELEASE_DIR}/index.html" ]]; then
  echo "[error] frontend build did not produce index.html"
  exit 1
fi

echo "[info] atomically activating frontend release ${RELEASE_ID}"
ln -s "${RELEASE_DIR}" "${NEXT_LINK}"
mv -Tf "${NEXT_LINK}" "${CURRENT_LINK}"

echo "[info] validating and reloading web service: ${WEB_SERVICE}"
run_cmd nginx -t
run_cmd systemctl reload "${WEB_SERVICE}"
echo "[info] service state:"
run_cmd systemctl is-active "${WEB_SERVICE}"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1/ >/dev/null
echo "[info] service status:"
run_cmd systemctl status "${WEB_SERVICE}" --no-pager | sed -n '1,20p'

echo "[info] removing superseded frontend releases"
for candidate in "${RELEASES_DIR}"/*; do
  [[ -d "${candidate}" ]] || continue
  [[ "${candidate}" == "${RELEASE_DIR}" ]] && continue
  rm -rf -- "${candidate}"
done

trap - EXIT
