from __future__ import annotations

from pathlib import Path

from backend.app.mapping_agent import (
    MappingInvestigationAgent,
    ToolDefinition,
    ToolRegistry,
)
from backend.app.mapping_investigation import MappingInvestigationStore
from backend.app.mapping_reference_planner import ReferenceEvidencePlanner


def _case(store: MappingInvestigationStore) -> dict:
    case = store.create(
        subject_ref="product:PN-1",
        data_version="v1",
        verification={"selected_category": "PTZ", "status": "WARN", "signals": []},
    )
    return store.triage(
        case["case_id"],
        action="investigate",
        reviewer="user:owner",
        rationale_code="needs_evidence",
        expected_revision=case["revision"],
    )


def _tools(*, history: bool, document: bool) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                "mapping.get_candidates",
                "mapping candidates",
                lambda _args: {
                    "summary_code": "mapping_candidates_loaded",
                    "verification": {"signals": []},
                    "evidence_refs": ["mapping-rule:country:1"],
                },
            ),
            ToolDefinition(
                "catalog.get_product_attributes",
                "product attributes",
                lambda _args: {
                    "summary_code": "product_attributes_loaded",
                    "selected_category": "IPC",
                    "evidence_refs": ["evidence:catalog:1"],
                },
            ),
            ToolDefinition(
                "price_data.compare_sources",
                "source comparison",
                lambda _args: {"summary_code": "price_sources_consistent", "evidence_refs": []},
            ),
            ToolDefinition(
                "history.search_approved_cases",
                "approved cases",
                lambda _args: {
                    "summary_code": "approved_cases_found" if history else "approved_cases_not_found",
                    "entries": (
                        [{"entry_id": "17", "resolved_category": "IPC"}] if history else []
                    ),
                    "evidence_refs": ["approved-case:17"] if history else [],
                },
            ),
            ToolDefinition(
                "documents.search_product_documents",
                "documents",
                lambda _args: {
                    "summary_code": "document_evidence_found" if document else "document_evidence_not_found",
                    "hits": (
                        [
                            {
                                "record": {
                                    "evidence_id": "document:1",
                                    "authority": "official_product_document",
                                    "attributes": {"product_line": "IPC"},
                                    "content": "irrelevant manual text " * 4000,
                                }
                            }
                        ]
                        if document
                        else []
                    ),
                    "evidence_refs": ["document:1"] if document else [],
                },
            ),
            ToolDefinition(
                "catalog.search_similar_products",
                "similar products",
                lambda _args: {
                    "summary_code": "similar_products_not_found",
                    "items": [],
                    "evidence_refs": [],
                },
            ),
        ]
    )


def _context() -> dict:
    return {
        "pn": "PN-1",
        "subject_ref": "product:PN-1",
        "family_ref": "family:ipc",
        "data_version": "v1",
        "query": "PN-1 fixed camera",
    }


def test_memory_shortens_investigation_before_document_retrieval(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)

    completed = MappingInvestigationAgent(store=store, tools=_tools(history=True, document=True)).run(
        case["case_id"],
        planner=ReferenceEvidencePlanner(),
        initial_context=_context(),
    )

    assert completed["agent_result"]["candidate_category"] == "IPC"
    assert completed["agent_result"]["stop_reason"] == "approved_case_and_current_attributes_agree"
    assert [item["tool_name"] for item in completed["checkpoints"]] == [
        "mapping.get_candidates",
        "catalog.get_product_attributes",
        "history.search_approved_cases",
    ]


def test_document_retrieval_resolves_case_when_memory_misses(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)

    completed = MappingInvestigationAgent(
        store=store,
        tools=_tools(history=False, document=True),
        max_planner_context_chars=2000,
    ).run(
        case["case_id"],
        planner=ReferenceEvidencePlanner(),
        initial_context=_context(),
    )

    assert completed["agent_result"]["candidate_category"] == "IPC"
    assert completed["agent_result"]["stop_reason"] == "document_evidence_sufficient"
    assert len(completed["checkpoints"]) == 4


def test_insufficient_evidence_holds_current_request(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)

    completed = MappingInvestigationAgent(store=store, tools=_tools(history=False, document=False)).run(
        case["case_id"],
        planner=ReferenceEvidencePlanner(),
        initial_context=_context(),
    )

    assert completed["agent_result"]["candidate_category"] is None
    assert completed["agent_result"]["recommended_action"] == "retain_current_hold"
    assert completed["agent_result"]["stop_reason"] == "insufficient_or_conflicting_evidence"
    assert len(completed["checkpoints"]) == 5
