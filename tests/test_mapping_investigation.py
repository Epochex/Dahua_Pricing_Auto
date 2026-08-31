from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.mapping_investigation import (
    InvestigationConflict,
    InvestigationValidationError,
    MappingInvestigationStore,
)


def _case(store: MappingInvestigationStore, suffix: str = "1") -> dict:
    return store.create(
        subject_ref=f"pricing-request:req-1:row:{suffix}",
        data_version="price-data:v1",
        verification={
            "status": "WARN",
            "signals": [{"code": "family_category_outlier", "severity": "warning"}],
        },
        input_hash="sha256:" + "a" * 64,
    )


def test_human_triage_occurs_once_and_obvious_correction_skips_agent(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)

    corrected = store.triage(
        case["case_id"],
        action="correct_mapping",
        reviewer="user:pricing-owner",
        rationale_code="known_product_family",
        corrected_category="IPC",
        expected_revision=case["revision"],
    )

    assert corrected["state"] == "resolved_corrected"
    assert corrected["triage"]["corrected_category"] == "IPC"
    with pytest.raises(InvestigationConflict, match="exactly once"):
        store.triage(
            case["case_id"],
            action="investigate",
            reviewer="user:pricing-owner",
            rationale_code="changed_mind",
            expected_revision=corrected["revision"],
        )


def test_agent_investigation_has_checkpoints_and_no_second_confirmation(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)
    investigating = store.triage(
        case["case_id"],
        action="investigate",
        reviewer="user:pricing-owner",
        rationale_code="needs_evidence",
        expected_revision=case["revision"],
    )
    with_checkpoint = store.append_checkpoint(
        case["case_id"],
        tool_name="catalog.get_product_attributes",
        status="succeeded",
        summary_code="official_category_found",
        input_refs=["pricing-request:req-1:row:1"],
        output_refs=["evidence:catalog:1832"],
        idempotency_key="case-1-catalog-v1",
        expected_revision=investigating["revision"],
    )

    completed = store.complete_agent_result(
        case["case_id"],
        candidate_category="IPC",
        recommended_action="use_candidate_for_current_request",
        stop_reason="strong_evidence_found",
        evidence_refs=["evidence:catalog:1832"],
        counter_evidence_refs=["evidence:price-source:92"],
        unresolved_codes=["stale_price_source"],
        expected_revision=with_checkpoint["revision"],
    )

    assert completed["state"] == "investigation_complete"
    assert completed["agent_result"]["candidate_category"] == "IPC"
    assert [item["kind"] for item in completed["events"]] == [
        "case_created",
        "human_triage",
        "checkpoint_recorded",
        "agent_investigation_completed",
    ]

    written = store.record_writeback(
        case["case_id"],
        writeback_kind="current_request",
        target_ref="pricing-request:req-1:row:1",
        action_receipt_ref="receipt:pricing-workflow:551",
        written_by="user:pricing-owner",
        expected_revision=completed["revision"],
    )
    assert written["state"] == "written_back"

    reopened = store.correct_checkpoint(
        case["case_id"],
        checkpoint_id="checkpoint-1",
        reviewer="user:pricing-owner",
        reason_code="post_writeback_evidence_correction",
        corrected_output_refs=["evidence:catalog:1833"],
        expected_revision=written["revision"],
    )
    assert reopened["state"] == "investigating"
    assert reopened["reconciliation_required"] is True


def test_checkpoint_can_be_corrected_and_retried_without_rewriting_history(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)
    investigating = store.triage(
        case["case_id"],
        action="investigate",
        reviewer="user:pricing-owner",
        rationale_code="needs_evidence",
        expected_revision=case["revision"],
    )
    with_checkpoint = store.append_checkpoint(
        case["case_id"],
        tool_name="history.search_approved_cases",
        status="succeeded",
        summary_code="candidate_history_found",
        input_refs=["product-family:IPC-HFW"],
        output_refs=["evidence:case:17"],
        idempotency_key="case-1-history-v1",
        expected_revision=investigating["revision"],
    )

    corrected = store.correct_checkpoint(
        case["case_id"],
        checkpoint_id="checkpoint-1",
        reviewer="user:pricing-owner",
        reason_code="evidence_scope_too_broad",
        corrected_output_refs=["evidence:case:19"],
        expected_revision=with_checkpoint["revision"],
    )
    retried = store.retry_from_checkpoint(
        case["case_id"],
        checkpoint_id="checkpoint-1",
        requested_by="user:pricing-owner",
        reason_code="rerun_with_exact_family",
        expected_revision=corrected["revision"],
    )

    assert retried["state"] == "investigating"
    assert retried["checkpoints"][0]["output_refs"] == ["evidence:case:17"]
    assert retried["checkpoint_corrections"][0]["corrected_output_refs"] == ["evidence:case:19"]
    assert retried["retries"][0]["checkpoint_id"] == "checkpoint-1"


def test_candidate_without_evidence_is_rejected(tmp_path: Path) -> None:
    store = MappingInvestigationStore(tmp_path)
    case = _case(store)
    investigating = store.triage(
        case["case_id"],
        action="investigate",
        reviewer="user:pricing-owner",
        rationale_code="needs_evidence",
        expected_revision=case["revision"],
    )

    with pytest.raises(InvestigationValidationError, match="supporting evidence"):
        store.complete_agent_result(
            case["case_id"],
            candidate_category="IPC",
            recommended_action="use_candidate",
            stop_reason="model_guess",
            evidence_refs=[],
            counter_evidence_refs=[],
            unresolved_codes=[],
            expected_revision=investigating["revision"],
        )
