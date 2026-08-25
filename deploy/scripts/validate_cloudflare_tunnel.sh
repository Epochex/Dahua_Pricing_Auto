#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/data/Dahua_Pricing_Auto}"
ORIGIN_TEMPLATE="${REPO_DIR}/deploy/nginx/dahua-cloudflare-origin.conf"
TUNNEL_CONFIG="${1:-/etc/cloudflared/dahua-auto-pricing.yml}"
SERVICE_TEMPLATE="${REPO_DIR}/deploy/systemd/dahua-cloudflared.service"

failures=0

pass() {
  printf '[OK] %s\n' "$*"
}

fail() {
  printf '[FAIL] %s\n' "$*" >&2
  failures=$((failures + 1))
}

if [[ -f "${ORIGIN_TEMPLATE}" ]]; then
  pass "Nginx origin template exists"
else
  fail "Missing ${ORIGIN_TEMPLATE}"
fi

if grep -Eq 'listen[[:space:]]+127\.0\.0\.1:18083;' "${ORIGIN_TEMPLATE}"; then
  pass "Tunnel origin is bound to loopback only"
else
  fail "Tunnel origin is not explicitly bound to 127.0.0.1:18083"
fi

if grep -Eq 'location \^~ /api/agent/[[:space:]]*\{[[:space:]]*return 404;' "${ORIGIN_TEMPLATE}"; then
  pass "Internal /api/agent namespace has an explicit deny rule"
else
  fail "Missing explicit /api/agent deny rule"
fi

if grep -Eq 'location \^~ /api/evolution/[[:space:]]*\{[[:space:]]*return 404;' "${ORIGIN_TEMPLATE}"; then
  pass "Internal /api/evolution namespace has an explicit deny rule"
else
  fail "Missing explicit /api/evolution deny rule"
fi

if grep -Eq 'location \^~ /api/[[:space:]]*\{[[:space:]]*return 404;' "${ORIGIN_TEMPLATE}"; then
  pass "Unreviewed API paths are denied by default"
else
  fail "Missing default-deny rule for unreviewed API paths"
fi

if grep -Eq 'client_max_body_size[[:space:]]+100m;' "${ORIGIN_TEMPLATE}"; then
  pass "Origin accepts current 100 MiB upload limit"
else
  fail "Expected client_max_body_size 100m"
fi

if [[ -f "${TUNNEL_CONFIG}" ]]; then
  if grep -Eq '<TUNNEL-UUID>|<PRICING-HOSTNAME>' "${TUNNEL_CONFIG}"; then
    fail "Tunnel config still contains placeholders: ${TUNNEL_CONFIG}"
  else
    pass "Tunnel config placeholders have been replaced"
  fi

  if command -v cloudflared >/dev/null 2>&1; then
    if cloudflared tunnel --config "${TUNNEL_CONFIG}" ingress validate >/dev/null; then
      pass "cloudflared ingress configuration is valid"
    else
      fail "cloudflared rejected ${TUNNEL_CONFIG}"
    fi
  else
    printf '[SKIP] cloudflared is not installed; skipped ingress validation\n'
  fi
else
  printf '[SKIP] %s not found; pass its path as argument after installation\n' "${TUNNEL_CONFIG}"
fi

if command -v systemd-analyze >/dev/null 2>&1; then
  if systemd-analyze verify "${SERVICE_TEMPLATE}" >/dev/null 2>&1; then
    pass "systemd unit passes systemd-analyze verify"
  else
    fail "systemd unit failed systemd-analyze verify"
  fi
else
  printf '[SKIP] systemd-analyze is unavailable\n'
fi

if (( failures > 0 )); then
  printf '%d validation check(s) failed.\n' "${failures}" >&2
  exit 1
fi

printf 'Static validation completed successfully.\n'
