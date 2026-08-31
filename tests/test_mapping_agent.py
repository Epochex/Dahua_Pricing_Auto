from __future__ import annotations

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
