from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.mapping_agent import (
    MappingInvestigationAgent,
    ToolDefinition,
    ToolNotAllowed,
    ToolRegistry,
)
from backend.app.mapping_investigation import MappingInvestigationStore


def _investigating_case(store: MappingInvestigationStore) -> dict:
    case = store.create(
        subject_ref="pricing-request:req-7:row:2",
        data_version="price-data:v1",
        verification={"status": "WARN", "signals": [{"code": "source_conflict"}]},
    )
    return store.triage(
        case["case_id"],
        action="investigate",
        reviewer="user:owner",
        rationale_code="cannot_resolve",
        expected_revision=case["revision"],
    )


def test_agent_calls_custom_read_only_tool_and_completes_with_evidence(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _investigating_case(store)
    tools = ToolRegistry(
        [
            ToolDefinition(
                name="catalog.get_product_attributes",
                description="Read official product attributes",
                handler=lambda args: {
                    "summary_code": "official_category_found",
                    "product_line": "IPC",
                    "evidence_refs": [f"evidence:catalog:{args['subject_ref']}"],
                },
            )
        ]
    )

    def planner(context: dict) -> dict:
        if not context["observations"]:
            return {
                "type": "tool",
                "tool_name": "catalog.get_product_attributes",
                "arguments": {"subject_ref": "product:1832"},
            }
        return {
            "type": "complete",
            "candidate_category": "IPC",
            "recommended_action": "use_candidate_for_current_request",
            "stop_reason": "strong_evidence_found",
            "evidence_refs": ["evidence:catalog:product:1832"],
            "counter_evidence_refs": [],
            "unresolved_codes": [],
        }

    completed = MappingInvestigationAgent(store=store, tools=tools).run(
        case["case_id"],
        planner=planner,
        initial_context={"anomaly_codes": ["source_conflict"]},
    )

    assert completed["state"] == "investigation_complete"
    assert completed["agent_result"]["candidate_category"] == "IPC"
    assert completed["checkpoints"][0]["tool_name"] == "catalog.get_product_attributes"
    assert completed["agent_result"]["metrics"]["tool_calls"] == 1
    assert completed["agent_result"]["metrics"]["planner_calls"] == 2


def test_agent_stops_at_tool_budget(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _investigating_case(store)
    tools = ToolRegistry(
        [
            ToolDefinition(
                name="documents.search",
                description="Search documents",
                handler=lambda _args: {"summary_code": "no_evidence", "evidence_refs": []},
            )
        ]
    )
    agent = MappingInvestigationAgent(store=store, tools=tools, max_tool_calls=1)

    completed = agent.run(
        case["case_id"],
        planner=lambda _context: {
            "type": "tool",
            "tool_name": "documents.search",
            "arguments": {"query": "unknown product"},
        },
        initial_context={},
    )

    assert completed["state"] == "investigation_complete"
    assert completed["agent_result"]["candidate_category"] is None
    assert completed["agent_result"]["stop_reason"] == "tool_budget_exhausted"
    assert len(completed["checkpoints"]) == 1


def test_registry_refuses_mutating_tool() -> None:
    tools = ToolRegistry(
        [
            ToolDefinition(
                name="rules.publish",
                description="Publish a mapping rule",
                handler=lambda _args: {"published": True},
                read_only=False,
            )
        ]
    )

    with pytest.raises(ToolNotAllowed, match="not read-only"):
        tools.invoke_read_only("rules.publish", {})


def test_agent_bounds_large_tool_results_before_planner_call(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _investigating_case(store)
    tools = ToolRegistry(
        [
            ToolDefinition(
                name="catalog.large_result",
                description="Return a deliberately large source record",
                handler=lambda _args: {
                    "summary_code": "large_result",
                    "selected_category": "IPC",
                    "evidence_refs": ["evidence:large:1"],
                    "irrelevant_payload": "x" * 50000,
                },
            )
        ]
    )
    observed_context_sizes: list[int] = []

    def planner(context: dict) -> dict:
        observed_context_sizes.append(len(json.dumps(context, ensure_ascii=False, sort_keys=True)))
        if not context["observations"]:
            return {"type": "tool", "tool_name": "catalog.large_result", "arguments": {}}
        return {
            "type": "complete",
            "candidate_category": "IPC",
            "recommended_action": "use_candidate_for_current_request",
            "stop_reason": "bounded_context_has_decision_fields",
            "evidence_refs": ["evidence:large:1"],
            "counter_evidence_refs": [],
            "unresolved_codes": [],
        }

    completed = MappingInvestigationAgent(
        store=store,
        tools=tools,
        max_planner_context_chars=2000,
    ).run(case["case_id"], planner=planner, initial_context={})

    assert max(observed_context_sizes) <= 2000
    assert completed["agent_result"]["candidate_category"] == "IPC"
    assert completed["agent_result"]["metrics"]["tool_result_chars_total"] > 50000
    assert completed["agent_result"]["metrics"]["planner_context_chars_max"] <= 2000


def test_agent_holds_historical_conflict_without_independent_evidence(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _investigating_case(store)
    tools = ToolRegistry(
        [
            ToolDefinition(
                name="history.compare_prior_classifications",
                description="Return a historical conflict",
                handler=lambda _args: {
                    "summary_code": "prior_classification_conflict",
                    "selected_category": None,
                    "signals": [{"code": "historical_category_conflict"}],
                    "evidence_refs": ["history:ipc", "history:ptz"],
                },
            )
        ]
    )

    def planner(context: dict) -> dict:
        if not context["observations"]:
            return {
                "type": "tool",
                "tool_name": "history.compare_prior_classifications",
                "arguments": {"pn": "PN-1"},
            }
        return {
            "type": "complete",
            "candidate_category": "IPC",
            "recommended_action": "use_candidate_for_current_request",
            "stop_reason": "model_forced_choice",
            "evidence_refs": ["history:ipc"],
            "counter_evidence_refs": ["history:ptz"],
            "unresolved_codes": [],
        }

    completed = MappingInvestigationAgent(store=store, tools=tools).run(
        case["case_id"],
        planner=planner,
        initial_context={"pn": "PN-1"},
    )

    result = completed["agent_result"]
    assert result["candidate_category"] is None
    assert result["recommended_action"] == "retain_current_hold"
    assert result["stop_reason"] == "historical_conflict_requires_current_corroboration"
    assert result["counter_evidence_refs"] == ["history:ptz", "history:ipc"]
    assert result["metrics"]["decision_guard_interventions"] == 1
