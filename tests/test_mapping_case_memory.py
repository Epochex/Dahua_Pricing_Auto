from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.mapping_case_memory import MappingCaseMemory, MappingCaseMemoryError
from backend.app.mapping_investigation import MappingInvestigationStore


def _base_case(store: MappingInvestigationStore, suffix: str) -> dict:
    return store.create(
        subject_ref=f"product:{suffix}",
        data_version="v1",
        verification={
            "status": "WARN",
            "selected_category": "PTZ",
            "signals": [{"code": "family_category_outlier"}],
        },
    )


def test_human_correction_becomes_scoped_retrievable_memory(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _base_case(store, "1")
    corrected = store.triage(
        case["case_id"],
        action="correct_mapping",
        reviewer="user:owner",
        rationale_code="known_family",
        corrected_category="IPC",
        expected_revision=case["revision"],
    )
    memory = MappingCaseMemory(tmp_path)

    entry = memory.remember(corrected, family_ref="family:ipc-hfw")

    assert entry["scope"] == "exact_subject"
    assert entry["resolved_category"] == "IPC"
    assert entry["automatic_publish"] is False
    assert memory.search(subject_ref="product:1")["count"] == 1
    evidence = memory.as_evidence_records()[0]
    assert evidence.approved is True
    assert evidence.product_line == "IPC"


def test_agent_result_requires_writeback_receipt_before_memory(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _base_case(store, "2")
    investigating = store.triage(
        case["case_id"],
        action="investigate",
        reviewer="user:owner",
        rationale_code="needs_evidence",
        expected_revision=case["revision"],
    )
    completed = store.complete_agent_result(
        case["case_id"],
        candidate_category="IPC",
        recommended_action="use_candidate_for_current_request",
        stop_reason="official_document_found",
        evidence_refs=["evidence:official:2"],
        counter_evidence_refs=[],
        unresolved_codes=[],
        expected_revision=investigating["revision"],
    )
    memory = MappingCaseMemory(tmp_path)

    with pytest.raises(MappingCaseMemoryError, match="resolved or written-back"):
        memory.remember(completed, family_ref="family:ipc-hfw")

    written = store.record_writeback(
        case["case_id"],
        writeback_kind="current_request",
        target_ref="pricing-request:17:row:2",
        action_receipt_ref="receipt:17",
        written_by="user:owner",
        expected_revision=completed["revision"],
    )
    entry = memory.remember(written, family_ref="family:ipc-hfw")
    assert entry["decision_source"] == "agent_result_written_back"
    assert entry["evidence_refs"] == ["evidence:official:2"]


def test_repeated_family_cases_create_rule_suggestion_without_auto_publish(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    memory = MappingCaseMemory(tmp_path)
    for suffix in ("a", "b", "c"):
        case = _base_case(store, suffix)
        corrected = store.triage(
            case["case_id"],
            action="correct_mapping",
            reviewer="user:owner",
            rationale_code="known_family",
            corrected_category="IPC",
            expected_revision=case["revision"],
        )
        memory.remember(corrected, family_ref="family:ipc-hfw", scope="model_family")

    suggestion = memory.suggestions(minimum_occurrences=3)["suggestions"][0]
    assert suggestion["eligible_for_rule_proposal"] is True
    assert suggestion["automatic_publish"] is False
