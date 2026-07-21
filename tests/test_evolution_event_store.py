from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.evolution.event_store import (  # noqa: E402
    EventConflict,
    EventCorruption,
    EventStore,
    EventValidationError,
    ReplayValidationError,
    sha256_digest,
)


def make_event(event_id: str, task_id: str = "task-1", **overrides: object) -> dict:
    event = {
        "event_id": event_id,
        "run_id": "run-1",
        "session_id": "session-1",
        "task_id": task_id,
        "workflow_version": "pricing-1.0.0",
        "skill_version": "validate-1.0.0",
        "prompt_version": "prompt-1.0.0",
        "pre_state": "pricing",
        "action": "calculate_price",
        "post_state": "validating",
        "input_hash": sha256_digest({"pn": "secret-pn"}),
        "evidence_hash": sha256_digest({"rule": "secret-rule"}),
        "effect_hash": None,
        "receipt_hash": None,
        "evidence_refs": ["sheet:pricing!row-17", "policy:pricing-v3"],
        "reason": "PRICING_COMPLETE",
    }
    event.update(overrides)
    return event


def append_in_process(runtime: str, worker: int, count: int) -> None:
    store = EventStore(Path(runtime))
    for index in range(count):
        task_id = f"task-worker-{worker}"
        store.append(
            make_event(
                f"event-{worker}-{index}",
                task_id,
                pre_state=f"state-{index}",
                post_state=f"state-{index + 1}",
                action="advance",
                reason="ADVANCE",
            )
        )


class EvolutionEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.store = EventStore(self.runtime)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_is_durable_queryable_and_event_id_is_idempotent(self) -> None:
        first = self.store.append(make_event("event-1"))
        second = self.store.append(make_event("event-1"))

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["event"], second["event"])
        self.assertEqual(self.store.query(task_id="task-1"), [first["event"]])
        self.assertEqual(self.store.query(run_id="run-1", action="calculate_price"), [first["event"]])

        changed = make_event("event-1", reason="DIFFERENT")
        with self.assertRaises(EventConflict):
            self.store.append(changed)

    def test_schema_accepts_only_hashes_and_references(self) -> None:
        unsafe = make_event("event-unsafe")
        unsafe["price"] = 123.45
        with self.assertRaises(EventValidationError):
            self.store.append(unsafe)

        unsafe_hash = make_event("event-unsafe-hash", input_hash="customer raw value")
        with self.assertRaises(EventValidationError):
            self.store.append(unsafe_hash)

        unsafe_reason = make_event("event-unsafe-reason", reason="customer email is alice@example.test")
        with self.assertRaises(EventValidationError):
            self.store.append(unsafe_reason)

    def test_rebuild_and_export_are_deterministic(self) -> None:
        self.store.append(make_event("event-1"))
        self.store.append(
            make_event(
                "event-2",
                pre_state="validating",
                action="validate_price",
                post_state="submitting",
                reason="VALIDATION_PASSED",
            )
        )
        transitions = {"pricing": {"validating"}, "validating": {"submitting"}}
        replay = self.store.rebuild_task("task-1", allowed_transitions=transitions)
        self.assertEqual(replay["current_state"], "submitting")
        self.assertEqual(replay["last_seq"], 2)

        path_one = self.runtime / "bundle-one.json"
        path_two = self.runtime / "bundle-two.json"
        first = self.store.export_replay_bundle("task-1", path_one, allowed_transitions=transitions)
        second = self.store.export_replay_bundle("task-1", path_two, allowed_transitions=transitions)
        self.assertEqual(first, second)
        self.assertEqual(path_one.read_bytes(), path_two.read_bytes())
        payload = dict(first)
        bundle_hash = payload.pop("bundle_hash")
        self.assertEqual(bundle_hash, sha256_digest(payload))
        serialized = path_one.read_text(encoding="utf-8")
        self.assertNotIn("secret-pn", serialized)
        self.assertNotIn("secret-rule", serialized)

    def test_replay_detects_state_chain_and_transition_violation(self) -> None:
        self.store.append(make_event("event-1"))
        self.store.append(
            make_event(
                "event-2",
                pre_state="unrelated",
                action="submit_gsp",
                post_state="approved",
                reason="INVALID_CHAIN",
            )
        )
        with self.assertRaisesRegex(ReplayValidationError, "state chain breaks"):
            self.store.rebuild_task("task-1")

        clean = EventStore(self.runtime / "clean")
        clean.append(make_event("event-clean"))
        with self.assertRaisesRegex(ReplayValidationError, "illegal transition"):
            clean.rebuild_task("task-1", allowed_transitions={"pricing": {"manual_review"}})

    def test_replay_detects_illegal_sequence_in_persisted_record(self) -> None:
        first = self.store.append(make_event("event-1"))["event"]
        second = make_event(
            "event-3",
            pre_state="validating",
            post_state="submitting",
            reason="VALIDATION_PASSED",
            seq=3,
            occurred_at=first["occurred_at"],
        )
        normalized_record = self.store._record_for(second)
        with self.store.log_path.open("ab") as handle:
            handle.write(
                json.dumps(normalized_record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        with self.assertRaisesRegex(ReplayValidationError, "illegal sequence"):
            self.store.rebuild_task("task-1")

    def test_damaged_tail_is_ignored_then_repaired_on_append(self) -> None:
        self.store.append(make_event("event-1"))
        with self.store.log_path.open("ab") as handle:
            handle.write(b'{"interrupted":')

        self.assertEqual(len(self.store.query(task_id="task-1")), 1)
        result = self.store.append(
            make_event(
                "event-2",
                pre_state="validating",
                post_state="submitting",
                reason="VALIDATION_PASSED",
            )
        )
        self.assertTrue(result["tail_repaired"])
        self.assertEqual([event["seq"] for event in self.store.query(task_id="task-1")], [1, 2])

    def test_corruption_before_tail_is_not_hidden(self) -> None:
        self.store.ensure_dirs()
        self.store.log_path.write_bytes(b"broken\nmore-broken\n")
        with self.assertRaises(EventCorruption):
            self.store.query()

    def test_cross_process_append_uses_unique_contiguous_task_sequences(self) -> None:
        process_count = 4
        events_per_process = 12
        processes = [
            multiprocessing.Process(
                target=append_in_process,
                args=(str(self.runtime), worker, events_per_process),
            )
            for worker in range(process_count)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)

        events = self.store.query()
        self.assertEqual(len(events), process_count * events_per_process)
        for worker in range(process_count):
            replay = self.store.rebuild_task(f"task-worker-{worker}")
            self.assertEqual(replay["event_count"], events_per_process)
            self.assertEqual(replay["last_seq"], events_per_process)


if __name__ == "__main__":
    unittest.main()
