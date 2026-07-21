from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.evolution.case_memory import CaseMemory, CaseMemoryError


def _task(task_id: str) -> dict:
    return {
        "task_id": task_id,
        "state": "manual_review",
        "input": {"pn": "SECRET-PN", "source": "google_sheet_row"},
        "execution_context": {
            "run_id": f"run-{task_id}",
            "session_id": "session-1",
            "bundle_hash": "sha256:bundle",
            "pins": {"workflow:pricing": "1.0.0"},
        },
        "trace": [{"event": "price_boundary_review"}],
    }


def test_intervention_requires_review_and_never_auto_publishes(tmp_path: Path) -> None:
    memory = CaseMemory(tmp_path)
    observed = memory.observe(_task("task-1"))
    replayed = memory.observe(_task("task-1"))
    assert observed is not None
    assert replayed["idempotent_replay"] is True
    assert "SECRET-PN" not in memory.state_path.read_text(encoding="utf-8")
    assert memory.suggestions()["suggestions"] == []

    accepted = memory.review(
        observed["case_id"],
        disposition="accepted",
        reviewer="business-owner",
        rationale="boundary case is reusable",
        expected_terminal_state="manual_review",
        expected_action="route_terminal",
        strata={"region": "EU"},
    )
    assert accepted["status"] == "accepted"
    exported = memory.evaluation_cases()
    assert exported["count"] == 1
    assert len(exported["cases"][0]["input_hash"]) == 64
    suggestion = memory.suggestions(minimum_occurrences=2)["suggestions"][0]
    assert suggestion["eligible_for_candidate_proposal"] is False
    assert suggestion["automatic_publish"] is False


def test_review_is_immutable_and_accepted_case_needs_expectation(tmp_path: Path) -> None:
    memory = CaseMemory(tmp_path)
    observed = memory.observe(_task("task-2"))
    with pytest.raises(CaseMemoryError):
        memory.review(
            observed["case_id"],
            disposition="accepted",
            reviewer="owner",
            rationale="missing expectation",
        )
    memory.review(
        observed["case_id"],
        disposition="rejected",
        reviewer="owner",
        rationale="one-off exception",
    )
    with pytest.raises(CaseMemoryError):
        memory.review(
            observed["case_id"],
            disposition="accepted",
            reviewer="owner",
            rationale="changed mind",
            expected_action="advance",
        )
