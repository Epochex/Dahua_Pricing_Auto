from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.evolution.release import ReleaseConflict, ReleaseController, ReleaseValidationError


def _evaluation(**overrides: object) -> dict:
    value = {
        "passed": True,
        "constraint_violations": 0,
        "illegal_transitions": 0,
        "duplicate_effects": 0,
        "sample_count": 100,
    }
    value.update(overrides)
    return value


def _controller(tmp_path: Path, *, execution_enabled: bool = False) -> ReleaseController:
    return ReleaseController(
        tmp_path,
        execution_enabled=execution_enabled,
        min_shadow_cases=2,
        min_canary_cases=2,
        max_canary_fraction=0.10,
    )


def test_release_requires_offline_hard_gates(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    with pytest.raises(ReleaseValidationError):
        controller.propose(
            candidate_bundle_id="candidate",
            baseline_bundle_id="baseline",
            evaluation_id="eval-1",
            evaluation_summary=_evaluation(duplicate_effects=1),
        )


def test_shadow_canary_promote_is_durable_and_external_effects_default_off(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    release = controller.propose(
        candidate_bundle_id="candidate",
        baseline_bundle_id="baseline",
        evaluation_id="eval-1",
        evaluation_summary=_evaluation(),
        release_key="stable-request",
    )
    replay = controller.propose(
        candidate_bundle_id="candidate",
        baseline_bundle_id="baseline",
        evaluation_id="eval-1",
        evaluation_summary=_evaluation(),
        release_key="stable-request",
    )
    assert replay["release_id"] == release["release_id"]
    assert replay["idempotent_replay"] is True

    release = controller.start_shadow(release["release_id"])
    assert release["phase"] == "shadow"
    release = controller.record_window(
        release["release_id"],
        window_id="shadow-window",
        metrics={"sample_count": 2, "manual_review_delta": 0.0, "p95_latency_ratio": 1.0},
    )
    assert release["phase"] == "shadow_ready"
    release = controller.start_canary(
        release["release_id"], fraction=0.05, actor="developer", approved_by="reviewer"
    )
    assert release["phase"] == "canary"
    assert release["effects_allowed"] is False
    assert release["windows_agent_allowed"] is False
    assert release["notifications_allowed"] is False

    release = controller.record_window(
        release["release_id"],
        window_id="canary-window",
        metrics={"sample_count": 2, "manual_review_delta": 0.01, "p95_latency_ratio": 1.1},
    )
    assert release["phase"] == "canary_ready"
    release = controller.promote(release["release_id"], actor="developer", approved_by="reviewer")
    assert release["phase"] == "active"
    assert _controller(tmp_path).get(release["release_id"])["phase"] == "active"


def test_hard_violation_trips_kill_switch_and_rolls_back(tmp_path: Path) -> None:
    controller = _controller(tmp_path, execution_enabled=True)
    release = controller.propose(
        candidate_bundle_id="candidate",
        baseline_bundle_id="baseline",
        evaluation_id="eval-1",
        evaluation_summary=_evaluation(),
    )
    controller.start_shadow(release["release_id"])
    controller.record_window(release["release_id"], window_id="s", metrics={"sample_count": 2})
    controller.start_canary(release["release_id"], fraction=0.05, actor="a", approved_by="b")
    rolled_back = controller.record_window(
        release["release_id"],
        window_id="bad",
        metrics={"sample_count": 2, "constraint_violations": 1},
    )
    assert rolled_back["phase"] == "rolled_back"
    assert rolled_back["kill_switch"] is True
    assert rolled_back["effects_allowed"] is False


def test_release_rejects_self_approval_and_out_of_order_transition(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    release = controller.propose(
        candidate_bundle_id="candidate",
        baseline_bundle_id="baseline",
        evaluation_id="eval-1",
        evaluation_summary=_evaluation(),
    )
    with pytest.raises(ReleaseConflict):
        controller.start_canary(release["release_id"], fraction=0.05, actor="a", approved_by="b")
    controller.start_shadow(release["release_id"])
    controller.record_window(release["release_id"], window_id="s", metrics={"sample_count": 2})
    with pytest.raises(ReleaseValidationError):
        controller.start_canary(release["release_id"], fraction=0.05, actor="same", approved_by="same")
