from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.evolution.evaluation import (  # noqa: E402
    AdapterResult,
    EvaluationCase,
    EvaluationDataset,
    EvaluationError,
    EvaluationPolicy,
    EvaluationRunner,
)


def case(case_id: str, *, region: str, expected: str = "approved") -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        input={"pn": f"SECRET-{case_id}", "price": 100},
        input_ref=f"vault://pricing-cases/{case_id}",
        expected_terminal_state=expected,
        expected_action="submit_gsp",
        strata={"region": region, "channel": "distribution"},
    )


def result(
    state: str = "approved",
    *,
    latency: float = 100,
    success: bool = True,
    violations: dict[str, int] | None = None,
) -> AdapterResult:
    return AdapterResult(
        terminal_state=state,
        action="submit_gsp",
        success=success,
        latency_ms=latency,
        hard_violations=violations or {},
    )


class EvolutionEvaluationTests(unittest.TestCase):
    def policy(self, **overrides: object) -> EvaluationPolicy:
        values: dict[str, object] = {
            "min_cases": 4,
            "min_stratum_cases": 2,
            "max_accuracy_drop": 0,
            "max_manual_rate_increase": 0,
            "max_success_rate_drop": 0,
            "max_p95_latency_increase_ratio": 0.20,
        }
        values.update(overrides)
        return EvaluationPolicy(**values)  # type: ignore[arg-type]

    def dataset(self) -> EvaluationDataset:
        return EvaluationDataset(
            dataset_id="pricing-regression",
            version="2026-07-21",
            cases=(
                case("eu-1", region="EU"),
                case("eu-2", region="EU"),
                case("apac-1", region="APAC"),
                case("apac-2", region="APAC"),
            ),
        )

    def test_candidate_passes_paired_replay_and_writes_redacted_audit(self) -> None:
        dataset = self.dataset()
        seen_baseline: list[str] = []
        seen_candidate: list[str] = []

        def baseline(item: EvaluationCase) -> AdapterResult:
            seen_baseline.append(item.case_id)
            return result(latency=100)

        def candidate(item: EvaluationCase) -> dict[str, object]:
            seen_candidate.append(item.case_id)
            return {
                "terminal_state": "approved",
                "action": "submit_gsp",
                "success": True,
                "latency_ms": 80,
                "hard_violations": {},
            }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "eval.json"
            artifact = EvaluationRunner(self.policy()).evaluate(
                dataset,
                baseline,
                candidate,
                baseline_version="wf-1",
                candidate_version="wf-2",
                output_path=output,
            )
            persisted_text = output.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)

        expected_order = [item.case_id for item in dataset.cases]
        self.assertEqual(seen_baseline, expected_order)
        self.assertEqual(seen_candidate, expected_order)
        self.assertEqual(artifact["decision"], "promote")
        self.assertEqual(artifact["paired_delta"]["business_accuracy"], 0)
        self.assertEqual(artifact["paired_delta"]["latency_ms"]["p95"], -20)
        self.assertTrue(EvaluationRunner.verify_artifact(artifact))
        self.assertEqual(persisted["audit_digest"], artifact["audit_digest"])
        self.assertTrue(EvaluationRunner.verify_artifact(persisted))
        self.assertNotIn("SECRET-eu-1", persisted_text)
        self.assertEqual(
            artifact["dataset"]["manifest"]["cases"][0]["input_ref"],
            "vault://pricing-cases/eu-1",
        )
        self.assertEqual(len(artifact["dataset"]["manifest"]["cases"][0]["input_hash"]), 64)

    def test_any_hard_violation_rejects_even_when_quality_improves(self) -> None:
        dataset = self.dataset()

        def baseline(_: EvaluationCase) -> AdapterResult:
            return result(latency=200)

        def candidate(item: EvaluationCase) -> AdapterResult:
            violations = {"price_boundary": 1} if item.case_id == "eu-1" else {}
            return result(latency=10, violations=violations)

        artifact = EvaluationRunner(self.policy()).evaluate(
            dataset,
            baseline,
            candidate,
            baseline_version="wf-1",
            candidate_version="wf-2",
        )

        self.assertEqual(artifact["decision"], "reject")
        self.assertIn("overall:hard_gate:price_boundary", artifact["reasons"])
        self.assertEqual(artifact["candidate"]["hard_violations"]["price_boundary"], 1)

    def test_stratum_gate_catches_regression_hidden_by_overall_average(self) -> None:
        dataset = self.dataset()

        def baseline(item: EvaluationCase) -> AdapterResult:
            # Baseline is wrong in two APAC cases.
            return result("manual_review" if item.case_id.startswith("apac") else "approved")

        def candidate(item: EvaluationCase) -> AdapterResult:
            # Candidate fixes APAC but regresses exactly two EU cases: overall accuracy is unchanged.
            return result("manual_review" if item.case_id.startswith("eu") else "approved")

        artifact = EvaluationRunner(self.policy()).evaluate(
            dataset,
            baseline,
            candidate,
            baseline_version="wf-1",
            candidate_version="wf-2",
        )

        self.assertEqual(artifact["paired_delta"]["business_accuracy"], 0)
        self.assertNotIn("overall:business_accuracy_regressed", artifact["reasons"])
        self.assertIn("stratum:region=EU:business_accuracy_regressed", artifact["reasons"])
        eu = artifact["strata"]["stratum:region=EU"]
        apac = artifact["strata"]["stratum:region=APAC"]
        self.assertTrue(eu["eligible_for_gate"])
        self.assertEqual(eu["paired_delta"]["business_accuracy"], -1)
        self.assertEqual(apac["paired_delta"]["business_accuracy"], 1)
        self.assertEqual(artifact["decision"], "reject")

    def test_minimum_sample_and_threshold_regression_reject(self) -> None:
        dataset = EvaluationDataset(
            dataset_id="too-small",
            version="1",
            cases=(case("only", region="EU"),),
        )
        artifact = EvaluationRunner(self.policy()).evaluate(
            dataset,
            lambda _: result(latency=100),
            lambda _: result("manual_review", latency=150, success=False),
            baseline_version="wf-1",
            candidate_version="wf-2",
        )
        self.assertIn("overall:minimum_sample_not_met", artifact["reasons"])
        self.assertIn("overall:business_accuracy_regressed", artifact["reasons"])
        self.assertIn("overall:manual_review_rate_regressed", artifact["reasons"])
        self.assertIn("overall:success_rate_regressed", artifact["reasons"])
        self.assertIn("overall:p95_latency_regressed", artifact["reasons"])

    def test_cases_are_deeply_immutable_and_dataset_digest_changes_with_expectation(self) -> None:
        original = case("immutable", region="EU")
        with self.assertRaises(TypeError):
            original.input["price"] = 200  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            original.case_id = "changed"  # type: ignore[misc]

        first = EvaluationDataset("set", "1", (original,))
        second = EvaluationDataset("set", "1", (case("immutable", region="EU", expected="rejected"),))
        self.assertNotEqual(first.digest, second.digest)

    def test_llm_self_score_cannot_be_used_as_a_gate(self) -> None:
        dataset = self.dataset()

        def self_judging_adapter(_: EvaluationCase) -> dict[str, object]:
            return {
                "terminal_state": "approved",
                "action": "submit_gsp",
                "success": True,
                "latency_ms": 1,
                "llm_score": 1.0,
            }

        artifact = EvaluationRunner(self.policy()).evaluate(
            dataset,
            lambda _: result(),
            self_judging_adapter,
            baseline_version="wf-1",
            candidate_version="wf-2",
        )
        self.assertEqual(artifact["decision"], "reject")
        self.assertEqual(artifact["gate_authority"], "deterministic_metrics_only")
        self.assertEqual(artifact["candidate"]["success_rate"], 0)
        self.assertTrue(all(row["candidate"]["action"] == "adapter_error" for row in artifact["paired_cases"]))

    def test_invalid_or_tampered_case_and_artifact_are_detected(self) -> None:
        with self.assertRaises(EvaluationError):
            EvaluationCase(case_id="bad", input={"pn": "x"})
        with self.assertRaises(EvaluationError):
            EvaluationCase(
                case_id="bad-hash",
                input={"pn": "x"},
                input_hash="0" * 64,
                expected_action="submit_gsp",
            )

        artifact = EvaluationRunner(self.policy()).evaluate(
            self.dataset(),
            lambda _: result(),
            lambda _: result(),
            baseline_version="wf-1",
            candidate_version="wf-2",
        )
        artifact["decision"] = "reject"
        self.assertFalse(EvaluationRunner.verify_artifact(artifact))


if __name__ == "__main__":
    unittest.main()
