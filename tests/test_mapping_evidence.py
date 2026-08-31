from __future__ import annotations

from backend.app.mapping_evidence import (
    EvidenceContextAssembler,
    EvidenceHit,
    EvidenceRecord,
    TieredEvidenceRetriever,
)


def _record(
    evidence_id: str,
    *,
    subject_ref: str,
    family_ref: str,
    product_line: str,
    authority: str,
    content: str,
    version: str = "v1",
    approved: bool = False,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        source_type="test",
        source_version=version,
        authority=authority,
        subject_ref=subject_ref,
        family_ref=family_ref,
        content=content,
        attributes={"product_line": product_line},
        approved=approved,
    )


def test_tiered_retrieval_prefers_exact_and_filters_data_version() -> None:
    exact = _record(
        "evidence:catalog:1",
        subject_ref="product:1",
        family_ref="family:ipc",
        product_line="IPC",
        authority="product_catalog",
        content="official IPC product",
    )
    family = _record(
        "evidence:case:2",
        subject_ref="product:2",
        family_ref="family:ipc",
        product_line="IPC",
        authority="approved_human_case",
        content="approved similar model",
        approved=True,
    )
    stale = _record(
        "evidence:stale:3",
        subject_ref="product:1",
        family_ref="family:ipc",
        product_line="PTZ",
        authority="price_data",
        content="stale record",
        version="v0",
    )

    hits = TieredEvidenceRetriever([family, stale, exact]).retrieve(
        subject_ref="product:1",
        family_ref="family:ipc",
        query="IPC approved model",
        source_version="v1",
    )

    assert hits[0].record.evidence_id == "evidence:catalog:1"
    assert {item.record.evidence_id for item in hits} == {
        "evidence:catalog:1",
        "evidence:case:2",
    }


def test_context_assembler_preserves_support_and_counter_evidence() -> None:
    support = _record(
        "evidence:catalog:1",
        subject_ref="product:1",
        family_ref="family:ipc",
        product_line="IPC",
        authority="official_product_document",
        content="official product document says IPC",
    )
    counter = _record(
        "evidence:price:2",
        subject_ref="product:1",
        family_ref="family:ipc",
        product_line="PTZ",
        authority="price_data",
        content="price data maps the product to PTZ",
    )

    bundle = EvidenceContextAssembler().assemble(
        case_id="case-1",
        subject_ref="product:1",
        selected_category="PTZ",
        candidate_category="IPC",
        anomaly_codes=["source_conflict"],
        hits=[
            EvidenceHit(support, "exact_subject", 105.0),
            EvidenceHit(counter, "exact_subject", 103.0),
        ],
        remaining_tool_budget=4,
    )

    assert [item["evidence_id"] for item in bundle["supporting_evidence"]] == [
        "evidence:catalog:1"
    ]
    assert [item["evidence_id"] for item in bundle["counter_evidence"]] == [
        "evidence:price:2"
    ]
    assert bundle["missing_evidence"] == []
