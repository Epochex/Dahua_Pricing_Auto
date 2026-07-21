from __future__ import annotations

from pathlib import Path

from backend.app.evolution.control_plane import BASELINE_PINS, EvolutionControlPlane
from backend.app.pricing_workflow import PricingWorkflowCreateReq, PricingWorkflowStore


def _pricing(pns: list[str], _apply: bool) -> dict:
    return {
        "rows": [{"pn": pn, "status": "ok"} for pn in pns],
        "report": {"not_found": []},
    }


def _result() -> dict:
    return {
        "terminal_state": "manual_review",
        "action": "route_terminal",
        "success": True,
        "latency_ms": 10,
        "hard_violations": {},
    }


def test_workflow_pins_bundle_and_captures_replayable_events(tmp_path: Path) -> None:
    control = EvolutionControlPlane(tmp_path)
    bundle = control.ensure_baseline()
    context = control.resolve_execution_context(session_id="session-sheet-1")
    task = PricingWorkflowStore(tmp_path).create(
        PricingWorkflowCreateReq(
            pns=["PN-1"],
            source="google_sheet_row",
            notify=False,
            submission_authorized=False,
            idempotency_key="sheet-row-1",
        ),
        _pricing,
        execution_context=context,
    )
    capture = control.capture_workflow(task)
    replay = control.events.rebuild_task(task["task_id"])

    assert context["bundle_hash"] == bundle["bundle_hash"]
    assert task["state"] == "manual_review"
    assert capture["captured"] == 3
    assert replay["current_state"] == "manual_review"
    assert replay["events"][0]["session_id"] == "session-sheet-1"
    assert "PN-1" not in control.events.log_path.read_text(encoding="utf-8")


def test_evaluation_shadow_canary_promote_and_rollback_bundle(tmp_path: Path) -> None:
    control = EvolutionControlPlane(tmp_path)
    baseline = control.ensure_baseline()
    control.registry.register(
        "workflow",
        "enterprise-pricing",
        "1.1.0",
        {"states": ["pricing", "validating", "manual_review"], "change": "candidate"},
        parent_version="1.0.0",
        created_by="developer",
        status="candidate",
    )
    candidate_pins = {**BASELINE_PINS, "workflow:enterprise-pricing": "1.1.0"}
    candidate = control.registry.resolve_bundle("production", pins=candidate_pins, allow_candidate=True)
    assert candidate["bundle_hash"] != baseline["bundle_hash"]

    cases = [
        {
            "case_id": f"case-{index}",
            "input": {"pn": f"SECRET-{index}"},
            "input_ref": f"vault://case-{index}",
            "expected_terminal_state": "manual_review",
            "expected_action": "route_terminal",
            "strata": {"region": "EU"},
        }
        for index in range(2)
    ]
    results = {item["case_id"]: _result() for item in cases}
    evaluation = control.run_precomputed_evaluation(
        {
            "dataset_id": "pricing",
            "dataset_version": "1",
            "cases": cases,
            "baseline_results": results,
            "candidate_results": results,
            "baseline_version": baseline["bundle_hash"],
            "candidate_version": candidate["bundle_hash"],
            "policy": {"min_cases": 2, "min_stratum_cases": 2},
        }
    )
    assert evaluation["decision"] == "promote"

    release = control.propose_release(
        evaluation_id=evaluation["evaluation_id"],
        candidate_pins=candidate_pins,
        baseline_pins=BASELINE_PINS,
        environment="production",
        actor="developer",
    )
    release = control.releases.start_shadow(release["release_id"], actor="developer")
    release = control.releases.record_window(
        release["release_id"],
        window_id="shadow-1",
        metrics={"sample_count": 20, "manual_review_delta": 0, "p95_latency_ratio": 1},
    )
    release = control.releases.start_canary(
        release["release_id"], fraction=0.05, actor="developer", approved_by="reviewer"
    )
    assert release["effects_allowed"] is False
    assert release["windows_agent_allowed"] is False
    release = control.releases.record_window(
        release["release_id"],
        window_id="canary-1",
        metrics={"sample_count": 20, "manual_review_delta": 0, "p95_latency_ratio": 1},
    )
    promoted = control.promote_release(
        release["release_id"], actor="developer", approved_by="reviewer"
    )
    assert promoted["phase"] == "active"
    assert control.registry.resolve_bundle("production")["pins"]["workflow:enterprise-pricing"] == "1.1.0"

    restarted = EvolutionControlPlane(tmp_path)
    assert restarted.ensure_baseline()["pins"]["workflow:enterprise-pricing"] == "1.1.0"

    rolled_back = restarted.record_release_window(
        release["release_id"],
        window_id="production-bad",
        metrics={"sample_count": 20, "constraint_violations": 1},
        actor="metrics-evaluator",
    )
    assert rolled_back["phase"] == "rolled_back"
    assert restarted.registry.resolve_bundle("production")["pins"] == BASELINE_PINS
