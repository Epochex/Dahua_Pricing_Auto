from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from backend.app.mapping_agent import MappingInvestigationAgent, ToolDefinition, ToolRegistry
from backend.app.mapping_benchmark import (
    MappingBenchmarkCase,
    MappingBenchmarkEvaluator,
    MappingBenchmarkObservation,
    load_mapping_benchmark,
)
from backend.app.mapping_investigation import MappingInvestigationStore
from backend.app.mapping_reference_planner import ReferenceEvidencePlanner


def load_mapping_benchmark_scenarios(path: Path) -> Dict[str, Dict[str, Any]]:
    scenarios: Dict[str, Dict[str, Any]] = {}
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid scenario JSON on line {line_number}") from exc
        if not isinstance(raw, dict) or not str(raw.get("case_id") or "").strip():
            raise ValueError(f"scenario line {line_number} requires case_id")
        case_id = str(raw["case_id"])
        if case_id in scenarios:
            raise ValueError(f"duplicate scenario case_id: {case_id}")
        scenarios[case_id] = raw
    return scenarios


def _scenario_tools(
    scenario: Mapping[str, Any],
    *,
    disable_memory: bool = False,
    disable_documents: bool = False,
) -> ToolRegistry:
    verification = dict(scenario.get("verification") or {})
    attributes = dict(scenario.get("attributes") or {})
    memory_entries = [] if disable_memory else list(scenario.get("memory_entries") or [])
    documents = [] if disable_documents else list(scenario.get("documents") or [])
    similar_products = list(scenario.get("similar_products") or [])
    price_comparison = dict(scenario.get("price_comparison") or {})

    def mapping_candidates(_args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "summary_code": "mapping_candidates_loaded",
            "verification": verification,
            "evidence_refs": [],
        }

    def product_attributes(_args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "summary_code": "product_attributes_loaded" if attributes else "product_not_found",
            "selected_category": attributes.get("selected_category"),
            "evidence_refs": list(attributes.get("evidence_refs") or []),
        }

    def compare_sources(_args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "summary_code": price_comparison.get("summary_code", "price_sources_consistent"),
            "signals": verification.get("signals") or [],
            "evidence_refs": list(price_comparison.get("evidence_refs") or []),
        }

    def approved_cases(_args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "summary_code": "approved_cases_found" if memory_entries else "approved_cases_not_found",
            "entries": memory_entries,
            "evidence_refs": [
                f"approved-case:{item['entry_id']}" for item in memory_entries
            ],
        }

    def product_documents(args: Mapping[str, Any]) -> Mapping[str, Any]:
        source_version = str(args.get("source_version") or "")
        selected = [
            item
            for item in documents
            if not source_version or str(item.get("source_version") or "") == source_version
        ]
        hits = [
            {
                "record": {
                    **item,
                    "source_type": item.get("source_type", "product_manual"),
                    "approved": item.get("approved", True),
                },
                "retrieval_method": "benchmark_fixture",
                "score": 100.0,
            }
            for item in selected
        ]
        return {
            "summary_code": "document_evidence_found" if hits else "document_evidence_not_found",
            "hits": hits,
            "evidence_refs": [str(item["evidence_id"]) for item in selected],
        }

    def similar(_args: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "summary_code": "similar_products_found" if similar_products else "similar_products_not_found",
            "items": similar_products,
            "evidence_refs": [
                str(item.get("evidence_ref"))
                for item in similar_products
                if item.get("evidence_ref")
            ],
        }

    return ToolRegistry(
        [
            ToolDefinition("mapping.get_candidates", "benchmark mapping candidates", mapping_candidates),
            ToolDefinition("catalog.get_product_attributes", "benchmark attributes", product_attributes),
            ToolDefinition("price_data.compare_sources", "benchmark source comparison", compare_sources),
            ToolDefinition("history.search_approved_cases", "benchmark memory", approved_cases),
            ToolDefinition("documents.search_product_documents", "benchmark documents", product_documents),
            ToolDefinition("catalog.search_similar_products", "benchmark similar models", similar),
        ]
    )


def _direct_observation(
    case: MappingBenchmarkCase,
    scenario: Mapping[str, Any],
    *,
    latency_ms: float,
) -> MappingBenchmarkObservation:
    verification = dict(scenario.get("verification") or {})
    return MappingBenchmarkObservation(
        case_id=case.case_id,
        verification_status=str(verification.get("status") or ""),
        signal_codes=tuple(
            str(item.get("code") or "") for item in verification.get("signals") or []
        ),
        triage_action=str(scenario.get("triage_action") or "").strip() or None,
        agent_action=None,
        candidate_category=str(scenario.get("selected_category") or "").strip() or None,
        auto_routed=str(verification.get("status") or "").upper() == "PASS",
        evidence_refs=(),
        tool_calls=0,
        latency_ms=latency_ms,
    )


def run_reference_mapping_benchmark(
    *,
    manifest_path: Path,
    scenarios_path: Path,
    work_dir: Path,
    evaluator: MappingBenchmarkEvaluator | None = None,
    disable_memory: bool = False,
    disable_documents: bool = False,
) -> Dict[str, Any]:
    cases = load_mapping_benchmark(manifest_path)
    scenarios = load_mapping_benchmark_scenarios(scenarios_path)
    case_ids = {case.case_id for case in cases}
    if set(scenarios) != case_ids:
        raise ValueError(
            f"scenario coverage mismatch; missing={sorted(case_ids - set(scenarios))}, "
            f"extra={sorted(set(scenarios) - case_ids)}"
        )
    for case in cases:
        scenario_hash = hashlib.sha256(
            json.dumps(
                scenarios[case.case_id],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if scenario_hash != case.input_hash:
            raise ValueError(f"scenario hash mismatch: {case.case_id}")
    observations: list[MappingBenchmarkObservation] = []
    chain_metrics: Dict[str, Any] = {}
    for case in cases:
        started = time.monotonic()
        scenario = scenarios[case.case_id]
        scenario_triage = str(scenario.get("triage_action") or "").strip() or None
        if scenario_triage != "investigate":
            verification = dict(scenario.get("verification") or {})
            if str(verification.get("status") or "").upper() != "PASS":
                direct_store = MappingInvestigationStore(
                    Path(work_dir) / case.case_id,
                    durable_writes=False,
                )
                created = direct_store.create(
                    subject_ref=str(scenario["subject_ref"]),
                    data_version=str(scenario["data_version"]),
                    verification=verification,
                )
                direct_store.triage(
                    created["case_id"],
                    action=str(scenario_triage or ""),
                    reviewer="benchmark:label",
                    rationale_code="benchmark_expected_triage",
                    corrected_category=(
                        str(scenario.get("selected_category") or "")
                        if scenario_triage == "correct_mapping"
                        else None
                    ),
                    expected_revision=int(created["revision"]),
                )
            observations.append(
                _direct_observation(
                    case,
                    scenario,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                )
            )
            continue

        # Temporary benchmark state still exercises serialization, revisions,
        # checkpoints and atomic replacement. Production stores retain fsync.
        store = MappingInvestigationStore(
            Path(work_dir) / case.case_id,
            durable_writes=False,
        )
        verification = dict(scenario.get("verification") or {})
        created = store.create(
            subject_ref=str(scenario["subject_ref"]),
            data_version=str(scenario["data_version"]),
            verification=verification,
        )
        investigating = store.triage(
            created["case_id"],
            action=str(scenario_triage),
            reviewer="benchmark:label",
            rationale_code="benchmark_expected_triage",
            expected_revision=int(created["revision"]),
        )
        completed = MappingInvestigationAgent(
            store=store,
            tools=_scenario_tools(
                scenario,
                disable_memory=disable_memory,
                disable_documents=disable_documents,
            ),
            max_tool_calls=max(1, case.max_tool_calls),
            time_budget_seconds=max(1.0, case.max_latency_ms / 1000.0),
        ).run(
            investigating["case_id"],
            planner=ReferenceEvidencePlanner(),
            initial_context={
                "pn": scenario["pn"],
                "subject_ref": scenario["subject_ref"],
                "family_ref": scenario.get("family_ref"),
                "data_version": scenario["data_version"],
                "document_source_version": scenario["data_version"],
                "query": scenario["pn"],
            },
        )
        result = dict(completed.get("agent_result") or {})
        metrics = dict(result.get("metrics") or {})
        evidence_refs = tuple(
            dict.fromkeys(
                [str(item) for item in result.get("evidence_refs") or []]
                + [str(item) for item in result.get("counter_evidence_refs") or []]
            )
        )
        latency_ms = (time.monotonic() - started) * 1000.0
        chain_metrics[case.case_id] = metrics
        observations.append(
            MappingBenchmarkObservation(
                case_id=case.case_id,
                verification_status=str(verification.get("status") or ""),
                signal_codes=tuple(
                    str(item.get("code") or "") for item in verification.get("signals") or []
                ),
                triage_action=scenario_triage,
                agent_action=str(result.get("recommended_action") or "") or None,
                candidate_category=str(result.get("candidate_category") or "") or None,
                auto_routed=False,
                evidence_refs=evidence_refs,
                tool_calls=int(metrics.get("tool_calls") or 0),
                latency_ms=latency_ms,
            )
        )
    report = (evaluator or MappingBenchmarkEvaluator()).evaluate(cases, observations)
    report["planner"] = "reference_evidence_planner"
    report["configuration"] = {
        "case_memory_enabled": not disable_memory,
        "document_retrieval_enabled": not disable_documents,
        "temporary_store_fsync_enabled": False,
    }
    report["chain_metrics"] = chain_metrics
    return report


def run_reference_mapping_benchmark_with_ablations(
    *,
    manifest_path: Path,
    scenarios_path: Path,
    work_dir: Path,
) -> Dict[str, Any]:
    root = Path(work_dir)
    strict_evaluator = MappingBenchmarkEvaluator(
        minimum_case_pass_rate=1.0,
        minimum_required_signal_recall=1.0,
    )
    full = run_reference_mapping_benchmark(
        manifest_path=manifest_path,
        scenarios_path=scenarios_path,
        work_dir=root / "full",
        evaluator=strict_evaluator,
    )
    variants = {
        "without_case_memory": run_reference_mapping_benchmark(
            manifest_path=manifest_path,
            scenarios_path=scenarios_path,
            work_dir=root / "without-case-memory",
            evaluator=strict_evaluator,
            disable_memory=True,
        ),
        "without_document_retrieval": run_reference_mapping_benchmark(
            manifest_path=manifest_path,
            scenarios_path=scenarios_path,
            work_dir=root / "without-document-retrieval",
            evaluator=strict_evaluator,
            disable_documents=True,
        ),
    }
    full_passed = {item["case_id"] for item in full["cases"] if item["passed"]}
    ablations: Dict[str, Any] = {}
    for name, report in variants.items():
        variant_passed = {item["case_id"] for item in report["cases"] if item["passed"]}
        ablations[name] = {
            "case_pass_rate": report["case_pass_rate"],
            "eligible_for_release": report["eligible_for_release"],
            "regressed_case_ids": sorted(full_passed - variant_passed),
        }
    full["ablations"] = ablations
    return full
