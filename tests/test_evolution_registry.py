from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.evolution.registry import (  # noqa: E402
    EvolutionRegistry,
    RegistryConflict,
    RegistryError,
    RegistryValidationError,
)


class EvolutionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.registry = EvolutionRegistry(self.runtime)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def register_workflow(self, version: str = "1.0.0", **overrides: object) -> dict:
        values = {
            "content": {
                "initial_state": "pricing",
                "states": ["pricing", "validating", "manual_review"],
            },
            "source": {"type": "git", "commit": "abc123"},
            "created_by": "developer@example.com",
            "json_schema": {
                "type": "object",
                "required": ["initial_state", "states"],
            },
            "business_constraints": {
                "hard": ["price_must_not_be_below_floor", "currency_is_required"]
            },
            "metadata": {"description": "pricing workflow"},
        }
        values.update(overrides)
        return self.registry.register("workflow", "enterprise-pricing", version, **values)

    def make_candidate(self, version: str = "1.0.0", **overrides: object) -> dict:
        registered = self.register_workflow(version, **overrides)
        if registered["status"] == "draft":
            return self.registry.transition(
                "workflow",
                "enterprise-pricing",
                version,
                "candidate",
                actor="reviewer@example.com",
                reason="offline evaluation passed",
            )
        return registered

    def test_register_is_versioned_hashed_immutable_and_idempotent(self) -> None:
        first = self.register_workflow()
        replay = self.register_workflow()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["artifact_id"], "workflow:enterprise-pricing@1.0.0")
        self.assertRegex(first["content_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(first["artifact_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first["json_schema"]["type"], "object")
        self.assertIn("price_must_not_be_below_floor", first["business_constraints"]["hard"])

        first["content"]["states"].append("tampered-by-caller")
        persisted = self.registry.get("workflow", "enterprise-pricing", "1.0.0")
        self.assertNotIn("tampered-by-caller", persisted["content"]["states"])

        with self.assertRaises(RegistryConflict):
            self.register_workflow(
                content={"initial_state": "submitting", "states": ["submitting"]}
            )

    def test_parent_must_exist_and_is_recorded(self) -> None:
        with self.assertRaises(RegistryValidationError):
            self.register_workflow("2.0.0", parent_version="1.0.0")

        self.register_workflow("1.0.0")
        child = self.register_workflow(
            "2.0.0",
            parent_version="1.0.0",
            content={"initial_state": "pricing", "states": ["pricing", "validating"]},
        )
        self.assertEqual(child["parent_version"], "1.0.0")
        self.assertEqual(len(self.registry.list(kind="workflow", name="enterprise-pricing")), 2)

    def test_illegal_status_transitions_and_direct_activation_are_blocked(self) -> None:
        self.register_workflow()
        with self.assertRaises(RegistryConflict):
            self.registry.transition(
                "workflow",
                "enterprise-pricing",
                "1.0.0",
                "retired",
                actor="reviewer",
                reason="skip review",
            )
        with self.assertRaises(RegistryValidationError):
            self.registry.transition(
                "workflow",
                "enterprise-pricing",
                "1.0.0",
                "active",
                actor="reviewer",
                reason="missing environment",
            )
        self.registry.transition(
            "workflow",
            "enterprise-pricing",
            "1.0.0",
            "candidate",
            actor="reviewer",
            reason="tests passed",
        )
        rejected = self.registry.transition(
            "workflow",
            "enterprise-pricing",
            "1.0.0",
            "rejected",
            actor="risk-owner",
            reason="price guard regressed",
        )
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(RegistryConflict):
            self.registry.transition(
                "workflow",
                "enterprise-pricing",
                "1.0.0",
                "candidate",
                actor="reviewer",
                reason="terminal means terminal",
            )

    def test_environment_activation_replacement_and_task_pins(self) -> None:
        self.make_candidate("1.0.0")
        activated_v1 = self.registry.activate(
            "workflow",
            "enterprise-pricing",
            "1.0.0",
            environment="production",
            actor="release-controller",
            reason="baseline accepted",
        )
        replay = self.registry.activate(
            "workflow",
            "enterprise-pricing",
            "1.0.0",
            environment="production",
            actor="release-controller",
            reason="replayed delivery",
        )
        self.assertFalse(activated_v1["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])

        task_bundle = self.registry.resolve_bundle("production")
        task_pins = task_bundle["pins"]
        self.assertEqual(task_pins, {"workflow:enterprise-pricing": "1.0.0"})

        self.make_candidate(
            "2.0.0",
            parent_version="1.0.0",
            content={
                "initial_state": "pricing",
                "states": ["pricing", "validating", "shadow_check", "manual_review"],
            },
        )
        self.registry.activate(
            "workflow",
            "enterprise-pricing",
            "2.0.0",
            environment="production",
            actor="release-controller",
            reason="canary gates passed",
        )

        current = self.registry.resolve_bundle("production")
        pinned = self.registry.resolve_bundle("production", pins=task_pins)
        self.assertEqual(current["pins"]["workflow:enterprise-pricing"], "2.0.0")
        self.assertEqual(pinned["pins"]["workflow:enterprise-pricing"], "1.0.0")
        self.assertEqual(
            pinned["artifacts"]["workflow:enterprise-pricing"]["status"], "retired"
        )
        self.assertEqual(
            self.registry.get("workflow", "enterprise-pricing", "1.0.0")["status"],
            "retired",
        )

    def test_replacement_does_not_retire_version_bound_in_another_environment(self) -> None:
        self.make_candidate("1.0.0")
        for environment in ("staging", "production"):
            self.registry.activate(
                "workflow",
                "enterprise-pricing",
                "1.0.0",
                environment=environment,
                actor="release-controller",
                reason="promote baseline",
            )
        self.make_candidate(
            "2.0.0",
            parent_version="1.0.0",
            content={"initial_state": "pricing", "states": ["pricing", "validating"]},
        )
        self.registry.activate(
            "workflow",
            "enterprise-pricing",
            "2.0.0",
            environment="staging",
            actor="release-controller",
            reason="staging canary",
        )
        self.assertEqual(
            self.registry.get("workflow", "enterprise-pricing", "1.0.0")["status"],
            "active",
        )
        self.assertEqual(
            self.registry.resolve_bundle("production")["pins"]["workflow:enterprise-pricing"],
            "1.0.0",
        )

    def test_bundle_resolves_all_kinds_and_has_stable_content_hash(self) -> None:
        artifacts = [
            ("workflow", "enterprise-pricing", {"states": ["pricing"]}),
            ("skill", "validate-price", {"steps": ["check_floor"]}),
            ("prompt", "extract-sheet-row", "Return JSON only"),
            ("policy", "price-boundary", {"minimum_margin": 0.05}),
        ]
        for kind, name, content in artifacts:
            self.registry.register(
                kind,
                name,
                "1.0.0",
                content,
                created_by="developer",
                status="candidate",
            )
            self.registry.activate(
                kind,
                name,
                "1.0.0",
                environment="shadow",
                actor="release-controller",
                reason="shadow bundle",
            )
        one = self.registry.resolve_bundle("shadow")
        two = self.registry.resolve_bundle("shadow")
        self.assertEqual(set(one["artifacts"]), {f"{kind}:{name}" for kind, name, _ in artifacts})
        self.assertEqual(one["bundle_hash"], two["bundle_hash"])

    def test_audit_is_chained_and_tampering_is_detected(self) -> None:
        candidate = self.make_candidate()
        self.registry.activate(
            "workflow",
            "enterprise-pricing",
            "1.0.0",
            environment="production",
            actor="release-controller",
            reason="approved",
        )
        audit = self.registry.list_audit(artifact_id=candidate["artifact_id"])
        self.assertEqual([item["seq"] for item in audit], [1, 2, 3, 4])
        self.assertIsNone(audit[0]["previous_hash"])
        self.assertEqual(audit[1]["previous_hash"], audit[0]["event_hash"])

        state = json.loads(self.registry.state_path.read_text(encoding="utf-8"))
        state["audit"][0]["reason"] = "rewritten history"
        self.registry.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(RegistryError):
            self.registry.list()

    def test_concurrent_registration_has_one_artifact_and_one_audit_event(self) -> None:
        results: list[dict] = []
        errors: list[Exception] = []

        def register() -> None:
            try:
                results.append(self.register_workflow())
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=register) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(not item["idempotent_replay"] for item in results), 1)
        self.assertEqual(len(self.registry.list()), 1)
        self.assertEqual(len(self.registry.list_audit()), 1)

    def test_bundle_activation_is_atomic_and_retired_bundle_can_rollback(self) -> None:
        for version in ("1.0.0", "2.0.0"):
            for kind, name in (("workflow", "pricing"), ("skill", "validate")):
                self.registry.register(
                    kind,
                    name,
                    version,
                    {"version": version},
                    created_by="test",
                    status="candidate",
                )
        first = self.registry.activate_bundle(
            {"workflow:pricing": "1.0.0", "skill:validate": "1.0.0"},
            environment="production",
            actor="release",
            reason="initial",
        )
        second = self.registry.activate_bundle(
            {"workflow:pricing": "2.0.0", "skill:validate": "2.0.0"},
            environment="production",
            actor="release",
            reason="promote",
        )
        self.assertNotEqual(first["bundle_hash"], second["bundle_hash"])
        self.assertEqual(self.registry.get("workflow", "pricing", "1.0.0")["status"], "retired")

        rolled_back = self.registry.activate_bundle(
            {"workflow:pricing": "1.0.0", "skill:validate": "1.0.0"},
            environment="production",
            actor="release",
            reason="rollback",
        )
        self.assertEqual(rolled_back["pins"]["workflow:pricing"], "1.0.0")
        self.assertEqual(self.registry.get("workflow", "pricing", "1.0.0")["status"], "active")
        self.assertEqual(self.registry.get("workflow", "pricing", "2.0.0")["status"], "retired")


if __name__ == "__main__":
    unittest.main()
