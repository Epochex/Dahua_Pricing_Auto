#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app.mapping_benchmark_runner import (  # noqa: E402
    run_reference_mapping_benchmark_with_ablations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the mapping investigation benchmark chain")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "mapping_investigation_v1.jsonl",
    )
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "mapping_investigation_scenarios_v1.jsonl",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    memory_temp = Path("/dev/shm")
    temp_parent = memory_temp if memory_temp.is_dir() and os.access(memory_temp, os.W_OK) else None
    with tempfile.TemporaryDirectory(prefix="mapping-benchmark-", dir=temp_parent) as temporary:
        report = run_reference_mapping_benchmark_with_ablations(
            manifest_path=args.manifest,
            scenarios_path=args.scenarios,
            work_dir=Path(temporary),
        )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["eligible_for_release"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
