#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/data/Dahua_Pricing_Auto}"
RUNTIME_DIR="${RUNTIME_DIR:-/data/dahua_pricing_runtime}"
DOMAIN="${DOMAIN:-www.dahuafrance-auto-pricing.com}"
BACKEND_PORT="${BACKEND_PORT:-8000}"

log() {
  printf '[deploy] %s\n' "$*"
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run as root: sudo bash deploy/scripts/deploy_persistent.sh"
    exit 1
  fi
}

install_prerequisites() {
  log "Installing prerequisites (nginx, python venv)"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y nginx python3-venv
}

prepare_runtime_dirs() {
  log "Preparing runtime folders under ${RUNTIME_DIR}"
  mkdir -p \
    "${RUNTIME_DIR}/data" \
    "${RUNTIME_DIR}/mapping" \
    "${RUNTIME_DIR}/uploads" \
    "${RUNTIME_DIR}/outputs" \
    "${RUNTIME_DIR}/logs" \
    "${RUNTIME_DIR}/admin"
}

migrate_legacy_runtime_if_needed() {
  local old_runtime="/data/runtime"
  local has_new_data=0
  if [[ "${RUNTIME_DIR}" == "${old_runtime}" ]]; then
    return
  fi
  if [[ ! -d "${old_runtime}" ]]; then
    return
  fi
  if [[ -f "${RUNTIME_DIR}/data/FrancePrice.xlsx" || -f "${RUNTIME_DIR}/data/FrancePrice.xls" ]]; then
    has_new_data=1
  fi
  if [[ "${has_new_data}" -ne 1 ]]; then
    log "Migrating legacy runtime data from ${old_runtime} -> ${RUNTIME_DIR}"
    cp -a "${old_runtime}/." "${RUNTIME_DIR}/"
  fi
}

sync_mapping_files() {
  log "Syncing mapping CSV files to ${RUNTIME_DIR}/mapping"
  cp -f "${REPO_DIR}/mapping/productline_map_france_full.csv" "${RUNTIME_DIR}/mapping/"
  cp -f "${REPO_DIR}/mapping/productline_map_sys_full.csv" "${RUNTIME_DIR}/mapping/"
}

validate_data_files() {
  local fr_ok=0
  local sys_ok=0

  [[ -f "${RUNTIME_DIR}/data/FrancePrice.xlsx" || -f "${RUNTIME_DIR}/data/FrancePrice.xls" ]] && fr_ok=1
  [[ -f "${RUNTIME_DIR}/data/SysPrice.xlsx" || -f "${RUNTIME_DIR}/data/SysPrice.xls" ]] && sys_ok=1

  if [[ "${fr_ok}" -ne 1 || "${sys_ok}" -ne 1 ]]; then
    echo ""
    echo "Missing pricing source files in ${RUNTIME_DIR}/data:"
    echo "  - FrancePrice.xlsx (or FrancePrice.xls)"
    echo "  - SysPrice.xlsx (or SysPrice.xls)"
    echo "Please place these files first, then rerun deployment."
    exit 1
  fi
}

build_backend() {
  log "Setting up backend virtualenv and dependencies"
  cd "${REPO_DIR}"
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
}

build_frontend() {
  log "Installing frontend dependencies and building static assets"
  cd "${REPO_DIR}/frontend"
  if [[ -f package-lock.json ]]; then
    npm ci
  else
    npm install
  fi
  npm run build
}

install_systemd_service() {
  log "Installing systemd service"
  cp -f "${REPO_DIR}/deploy/systemd/dahua-pricing-backend.service" /etc/systemd/system/dahua-pricing-backend.service
  sed -i -E "s|^Environment=DAHUA_PRICING_RUNTIME_DIR=.*$|Environment=DAHUA_PRICING_RUNTIME_DIR=${RUNTIME_DIR}|g" /etc/systemd/system/dahua-pricing-backend.service
  sed -i "s|--port 8000|--port ${BACKEND_PORT}|g" /etc/systemd/system/dahua-pricing-backend.service
  systemctl daemon-reload
  systemctl enable --now dahua-pricing-backend
}

install_nginx_site() {
  log "Installing nginx site config"
  local bare_domain="${DOMAIN#www.}"
  cp -f "${REPO_DIR}/deploy/nginx/dahua-auto-pricing.conf" /etc/nginx/sites-available/dahua-auto-pricing
  sed -i -E "s|^[[:space:]]*server_name[[:space:]].*;|    server_name ${DOMAIN} ${bare_domain};|g" /etc/nginx/sites-available/dahua-auto-pricing
  sed -i "s|http://127.0.0.1:8000|http://127.0.0.1:${BACKEND_PORT}|g" /etc/nginx/sites-available/dahua-auto-pricing

  ln -sfn /etc/nginx/sites-available/dahua-auto-pricing /etc/nginx/sites-enabled/dahua-auto-pricing
  rm -f /etc/nginx/sites-enabled/default

  nginx -t
  systemctl enable --now nginx
  systemctl restart nginx
}

print_result() {
  local ip
  ip="$(hostname -I | awk '{print $1}')"
  log "Deployment completed."
  echo ""
  echo "Open in browser:"
  echo "  - http://${DOMAIN}"
  echo "  - http://${ip}    (if DNS not configured yet)"
  echo ""
  echo "Service status:"
  echo "  systemctl status dahua-pricing-backend --no-pager"
  echo "  systemctl status nginx --no-pager"
}

main() {
  require_root
  install_prerequisites
  prepare_runtime_dirs
  migrate_legacy_runtime_if_needed
  sync_mapping_files
  validate_data_files
  build_backend
  build_frontend
  install_systemd_service
  install_nginx_site
  print_result
}

main "$@"
