from __future__ import annotations

import json
from pathlib import Path

from backend.app.historical_agent_benchmark import run_temporal_history_benchmark
from backend.app.historical_pricing_evidence import HistoricalPricingEvidenceIndex


def _write_state(runtime: Path, job: str, timestamp: str, item: dict) -> None:
    path = runtime / "outputs" / job / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "created_at": timestamp,
                "status": "done",
                "report": {"count_total": 1, "items": [item]},
            }
        ),
        encoding="utf-8",
    )


def _item(pn: str, category: str, *, status: str = "ok", warnings: list[str] | None = None) -> dict:
    return {
        "pn": pn,
        "internal_model": f"MODEL-{pn}",
        "status": status,
        "category": category,
        "price_group": category,
        "warnings": warnings or [],
    }


class _AdaptiveReplayPlanner:
    def __call__(self, context: dict) -> dict:
        observations = context.get("observations") or []
        investigation = context["investigation"]
        base_args = {
            "pn": investigation["pn"],
            "as_of": investigation["as_of"],
        }
        if not observations:
            return {
                "type": "tool",
                "tool_name": "history.compare_prior_classifications",
                "arguments": base_args,
                "hypothesis": "earlier outcomes may contain a stable category",
                "reason": "aggregation is the cheapest discriminator",
            }
        summary = observations[0]["result"]
        if summary["summary_code"] == "prior_classification_consensus":
            return {
                "type": "complete",
                "candidate_category": summary["selected_category"],
                "recommended_action": "use_candidate_for_current_request",
                "stop_reason": "strict_prior_consensus",
                "evidence_refs": summary["evidence_refs"],
                "counter_evidence_refs": [],
                "unresolved_codes": [],
            }
        if summary["summary_code"] == "prior_classification_conflict" and len(observations) == 1:
            return {
                "type": "tool",
                "tool_name": "history.search_pricing_results",
                "arguments": {**base_args, "limit": 20},
                "hypothesis": "the conflict may be explained by recency or warnings",
                "reason": "individual outcomes expose counter-evidence",
            }
        refs = [
            ref
            for observation in observations
            for ref in observation["result"].get("evidence_refs") or []
        ]
        return {
            "type": "complete",
            "candidate_category": None,
            "recommended_action": "retain_current_hold",
            "stop_reason": "strict_prior_evidence_unresolved",
            "evidence_refs": [],
            "counter_evidence_refs": refs,
            "unresolved_codes": ["strict_prior_evidence_unresolved"],
        }


def test_real_temporal_benchmark_measures_memory_gain_and_path_adaptation(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    # Consistent history.
    _write_state(runtime, "c1", "2026-01-01T00:00:00+00:00", _item("PN-C", "IPC"))
    _write_state(runtime, "c2", "2026-02-01T00:00:00+00:00", _item("PN-C", "IPC"))
    # Historical conflict at the held-out third occurrence.
    _write_state(runtime, "x1", "2026-01-01T00:00:00+00:00", _item("PN-X", "IPC"))
    _write_state(runtime, "x2", "2026-02-01T00:00:00+00:00", _item("PN-X", "PTZ"))
    _write_state(runtime, "x3", "2026-03-01T00:00:00+00:00", _item("PN-X", "PTZ"))
    # Earlier missing result gives no category evidence.
    _write_state(
        runtime,
        "r1",
        "2026-01-01T00:00:00+00:00",
        _item("PN-R", "UNKNOWN", status="not_found"),
    )
    _write_state(runtime, "r2", "2026-02-01T00:00:00+00:00", _item("PN-R", "NVR"))
    # Warning-bearing consensus is kept as a separate episode type.
    _write_state(
        runtime,
        "w1",
        "2026-01-01T00:00:00+00:00",
        _item("PN-W", "NVR", warnings=["base match"]),
    )
    _write_state(runtime, "w2", "2026-02-01T00:00:00+00:00", _item("PN-W", "NVR"))

    report = run_temporal_history_benchmark(
        index=HistoricalPricingEvidenceIndex(runtime),
        planner=_AdaptiveReplayPlanner(),
        per_type=1,
        work_dir=tmp_path / "benchmark",
    )

    assert report["selection"]["episode_counts"] == {
        "consistent_history": 1,
        "historical_conflict": 1,
        "recovered_after_not_found": 1,
        "warning_history": 1,
    }
    assert report["with_history"]["pass_rate"] == 1.0
    assert report["without_history"]["pass_rate"] == 0.5
    assert report["history_gain"]["pass_rate_delta"] == 0.5
    assert report["with_history"]["distinct_tool_sequences"] == 2
