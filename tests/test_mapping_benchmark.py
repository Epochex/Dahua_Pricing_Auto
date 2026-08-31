from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from backend.app.mapping_benchmark import (
    BENCHMARK_STRATA,
    MappingBenchmarkEvaluator,
    MappingBenchmarkObservation,
    load_mapping_benchmark,
)


DATASET = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "mapping_investigation_v1.jsonl"
)


def _perfect_observations() -> list[MappingBenchmarkObservation]:
    rows = []
    for case in load_mapping_benchmark(DATASET):
        rows.append(
            MappingBenchmarkObservation(
                case_id=case.case_id,
                verification_status=case.expected_verification_status,
                signal_codes=case.expected_signal_codes,
                triage_action=case.expected_triage_action,
                agent_action=case.expected_agent_action,
                candidate_category=(
                    None if case.candidate_must_be_null else case.expected_candidate_category
                ),
                auto_routed=case.expected_verification_status == "PASS",
                evidence_refs=case.allowed_evidence_refs,
                tool_calls=min(case.max_tool_calls, 2 if case.expected_agent_action else 0),
                latency_ms=min(case.max_latency_ms, 500.0),
            )
        )
    return rows


def test_versioned_dataset_covers_every_required_stratum() -> None:
    cases = load_mapping_benchmark(DATASET)

    assert len(cases) == 12
    assert {case.stratum for case in cases} == BENCHMARK_STRATA
    assert {case.split for case in cases} == {"baseline", "regression", "holdout", "shadow"}


def test_perfect_result_passes_release_gates() -> None:
    cases = load_mapping_benchmark(DATASET)
    result = MappingBenchmarkEvaluator().evaluate(cases, _perfect_observations())

    assert result["case_pass_rate"] == 1.0
    assert result["required_signal_recall"] == 1.0
    assert result["eligible_for_release"] is True


def test_unsafe_auto_route_is_a_hard_release_failure() -> None:
    cases = load_mapping_benchmark(DATASET)
    observations = _perfect_observations()
    target_index = next(
        index
        for index, item in enumerate(observations)
        if item.case_id == "refuse-no-evidence-009"
    )
    observations[target_index] = replace(observations[target_index], auto_routed=True)

    result = MappingBenchmarkEvaluator().evaluate(cases, observations)

    assert result["unsafe_auto_route_count"] == 1
    assert result["gates"]["zero_unsafe_auto_routes"] is False
    assert result["eligible_for_release"] is False


def test_unsupported_evidence_and_budget_overrun_are_reported() -> None:
    cases = load_mapping_benchmark(DATASET)
    observations = _perfect_observations()
    target_index = next(
        index
        for index, item in enumerate(observations)
        if item.case_id == "unseen-suffix-007"
    )
    observations[target_index] = replace(
        observations[target_index],
        evidence_refs=("evidence:invented",),
        tool_calls=99,
    )

    result = MappingBenchmarkEvaluator().evaluate(cases, observations)

    assert result["unsupported_evidence_count"] == 1
    assert result["tool_budget_violation_count"] == 1
    assert result["eligible_for_release"] is False
