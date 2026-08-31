from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app import main as app_main
from backend.app.mapping_agent import ToolDefinition, ToolRegistry


class _Agent:
    def _check_desktop_agent_token(self, token: str | None) -> None:
        if token != "secret":
            raise HTTPException(status_code=403, detail="invalid desktop agent token")


class _Engine:
    def verify_mapping(self, pn: str, *, family_categories: list[str]) -> dict:
        return {
            "pn": pn,
            "status": "WARN",
            "selected_category": "PTZ",
            "selected_price_group": "PTZ",
            "data_version": "v1",
            "signals": [{"code": "family_category_outlier", "severity": "warning"}],
            "matches_by_source": {},
            "recommended_action": "request_human_triage",
            "row_resolution": {},
        }


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_main, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(app_main, "_agent", _Agent())
    monkeypatch.setattr(app_main, "_engine", _Engine())
    monkeypatch.setattr(app_main, "_mapping_investigations", None)
    monkeypatch.setattr(app_main, "_mapping_case_memory", None)
    monkeypatch.setattr(app_main, "_mapping_documents", None)


def test_mapping_verify_requires_agent_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    with pytest.raises(HTTPException) as exc:
        app_main.mapping_verify(app_main.MappingVerifyReq(token="wrong", pn="PN-1"))

    assert exc.value.status_code == 403


def test_mapping_verify_creates_case_and_supports_one_triage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    result = app_main.mapping_verify(
        app_main.MappingVerifyReq(
            token="secret",
            pn="PN-1",
            subject_ref="pricing-request:req-1:row:1",
        )
    )
    case = result["case"]

    assert result["verification"]["status"] == "WARN"
    assert case["state"] == "awaiting_triage"

    investigating = app_main.mapping_investigation_triage(
        case["case_id"],
        app_main.MappingTriageReq(
            token="secret",
            action="investigate",
            reviewer="user:owner",
            rationale_code="needs_evidence",
            expected_revision=case["revision"],
        ),
    )
    assert investigating["state"] == "investigating"

    listed = app_main.mapping_investigations(token="secret")
    assert listed["count"] == 1


def test_reference_investigation_endpoint_runs_retrieval_and_records_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    tools = ToolRegistry(
        [
            ToolDefinition(
                "mapping.get_candidates",
                "candidates",
                lambda _args: {
                    "summary_code": "mapping_candidates_loaded",
                    "verification": {"signals": []},
                    "evidence_refs": [],
                },
            ),
            ToolDefinition(
                "catalog.get_product_attributes",
                "attributes",
                lambda _args: {
                    "summary_code": "product_attributes_loaded",
                    "selected_category": "IPC",
                    "evidence_refs": ["catalog:PN-1"],
                },
            ),
            ToolDefinition(
                "price_data.compare_sources",
                "sources",
                lambda _args: {"summary_code": "price_sources_consistent", "evidence_refs": []},
            ),
            ToolDefinition(
                "history.search_approved_cases",
                "memory",
                lambda _args: {
                    "summary_code": "approved_cases_found",
                    "entries": [{"entry_id": "9", "resolved_category": "IPC"}],
                    "evidence_refs": ["approved-case:9"],
                },
            ),
            ToolDefinition(
                "documents.search_product_documents",
                "documents",
                lambda _args: {"summary_code": "document_evidence_not_found", "hits": [], "evidence_refs": []},
            ),
            ToolDefinition(
                "catalog.search_similar_products",
                "similar",
                lambda _args: {"summary_code": "similar_products_not_found", "items": [], "evidence_refs": []},
            ),
        ]
    )
    monkeypatch.setattr(app_main, "build_mapping_domain_tools", lambda **_kwargs: tools)

    created = app_main.mapping_verify(
        app_main.MappingVerifyReq(token="secret", pn="PN-1", subject_ref="product:PN-1")
    )["case"]
    investigating = app_main.mapping_investigation_triage(
        created["case_id"],
        app_main.MappingTriageReq(
            token="secret",
            action="investigate",
            reviewer="user:owner",
            rationale_code="needs_evidence",
            expected_revision=created["revision"],
        ),
    )
    completed = app_main.mapping_investigation_run_reference(
        created["case_id"],
        app_main.MappingAgentRunReq(
            token="secret",
            pn="PN-1",
            subject_ref="product:PN-1",
            expected_revision=investigating["revision"],
        ),
    )

    assert completed["state"] == "investigation_complete"
    assert completed["agent_result"]["candidate_category"] == "IPC"
    assert completed["agent_result"]["metrics"]["tool_calls"] == 3


def test_document_evidence_api_registers_immutable_versioned_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    registered = app_main.mapping_document_register(
        app_main.MappingDocumentReq(
            token="secret",
            evidence_id="doc:PN-1:v1",
            source_type="product_manual",
            source_version="v1",
            authority="official_product_document",
            subject_ref="product:PN-1",
            family_ref="family:IPC",
            content="PN-1 is an IPC product.",
            attributes={"product_line": "IPC"},
            approved=True,
        )
    )

    assert registered["content_hash"].startswith("sha256:")
    assert app_main.mapping_documents(subject_ref="product:PN-1", token="secret")["count"] == 1
