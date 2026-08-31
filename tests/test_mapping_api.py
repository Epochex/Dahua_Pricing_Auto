from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app import main as app_main


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
