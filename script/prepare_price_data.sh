#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${DAHUA_PRICING_RUNTIME_DIR:-/data/dahua_pricing_runtime}"
DATA_DIR="${RUNTIME_DIR}/data"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
export DAHUA_PRICING_RUNTIME_DIR="${RUNTIME_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

echo "[info] preparing price data in ${DATA_DIR}"
cd "${ROOT_DIR}"
"${PYTHON_BIN}" - <<'PY'
import os
from pathlib import Path

from backend.engine.core.loader import prepare_price_data_files

runtime_dir = Path(os.getenv("DAHUA_PRICING_RUNTIME_DIR", "/data/dahua_pricing_runtime"))
data_dir = runtime_dir / "data"
france_path, sys_path = prepare_price_data_files(data_dir)

print(f"[info] France price file ready: {france_path}")
print(f"[info] Sys price file ready: {sys_path}")
PY
