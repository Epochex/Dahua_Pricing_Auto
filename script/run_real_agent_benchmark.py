#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app.historical_agent_benchmark import run_temporal_history_benchmark  # noqa: E402
from backend.app.historical_pricing_evidence import HistoricalPricingEvidenceIndex  # noqa: E402
from backend.app.mapping_llm_planner import OpenAICompatibleMappingPlanner  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run leakage-free Agent replay against persisted pricing history."
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path("/data/dahua_pricing_runtime"),
    )
    parser.add_argument(
        "--endpoint",
        default="https://api.deepseek.com/chat/completions",
    )
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument(
        "--api-key-path",
        type=Path,
        default=Path("/data/dahua_pricing_runtime/agent/ds-api.key"),
    )
    parser.add_argument("--per-type", type=int, default=3)
    parser.add_argument("--max-tool-calls", type=int, default=4)
    parser.add_argument("--time-budget-seconds", type=float, default=60.0)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    api_key = args.api_key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise SystemExit("model API key is empty")
    planner = OpenAICompatibleMappingPlanner(
        endpoint=args.endpoint,
        model=args.model,
        api_key=api_key,
        timeout_seconds=min(120.0, args.time_budget_seconds),
        max_output_tokens=800,
    )
    report = run_temporal_history_benchmark(
        index=HistoricalPricingEvidenceIndex(args.runtime_dir),
        planner=planner,
        per_type=args.per_type,
        max_tool_calls=args.max_tool_calls,
        time_budget_seconds=args.time_budget_seconds,
    )
    output = args.output
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = args.runtime_dir / "agent" / "evals" / f"historical_agent_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "with_history"}, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "with_history": {
                    key: value
                    for key, value in report["with_history"].items()
                    if key != "cases"
                },
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
