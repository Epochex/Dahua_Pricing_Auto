from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.sheet_workflow_ingest import (  # noqa: E402
    SheetWorkflowIngestCoordinator,
    plan_sheet_workflow_backfill,
    plan_sheet_workflow_ingest,
    sheet_backfill_manifest_hash,
    sheet_row_business_hash,
    sheet_row_idempotency_key,
    sheet_row_locator,
)
from backend.app.pricing_workflow import PricingWorkflowStore  # noqa: E402


SPREADSHEET_ID = "1-test-google-sheet"


def task(**overrides: object) -> dict:
    value = {
        "sheet": "2026.07",
        "row_index": 12,
        "requester": "Lucie",
        "description": "释放+定价",
        "product_line": "IPC",
        "pn": "DH-IPC-001",
        "internal_model": "IPC-HFW",
        "price_level": "Country",
        "customer_name": "TEVAH",
        "deadline": "2026-07-20",
        "pla_no": "",
        "pla_numbers": [],
        "owner": "JIANKE",
        "status": "待处理",
        "stage": "",
        "note": "",
        "normalized_status": "pending",
    }
    value.update(overrides)
    return value


class SheetWorkflowIngestTests(unittest.TestCase):
    def test_first_snapshot_is_baseline_and_never_creates(self) -> None:
        plan = plan_sheet_workflow_ingest(
            {"tasks": [task()]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows=None,
            allow_new_rows=True,
        )
        self.assertEqual(plan.create_requests, [])
        self.assertEqual(plan.decisions[0].action, "baseline")
        self.assertEqual(plan.decisions[0].reason, "initial_snapshot_never_auto_creates")

    def test_valid_new_row_builds_pricing_only_request(self) -> None:
        plan = plan_sheet_workflow_ingest(
            {"tasks": [task()]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        )
        self.assertEqual(len(plan.create_requests), 1)
        request = plan.create_requests[0]
        self.assertEqual(request.pns, ["DH-IPC-001"])
        self.assertEqual(request.source, "google_sheet_row")
        self.assertFalse(request.notify)
        self.assertFalse(request.submission_authorized)
        self.assertEqual(request.gsp_payload, {})

    def test_candidate_stops_at_manual_review_and_cannot_be_claimed_for_submit(self) -> None:
        request = plan_sheet_workflow_ingest(
            {"tasks": [task()]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        ).create_requests[0]

        def successful_pricing(pns: list[str], apply_black_markup: bool) -> dict:
            _ = apply_black_markup
            return {
                "rows": [{"pn": pn, "status": "ok"} for pn in pns],
                "report": {"not_found": []},
            }

        with tempfile.TemporaryDirectory() as tmp:
            store = PricingWorkflowStore(Path(tmp))
            created = store.create(request, successful_pricing)
            self.assertEqual(created["state"], "manual_review")
            self.assertFalse(created["submission"]["authorized"])
            claim = store.claim(
                worker_id="windows-test",
                capabilities=["submit_gsp"],
                lease_seconds=120,
            )
            self.assertFalse(claim["claimed"])

    def test_allow_new_rows_is_an_explicit_safety_gate(self) -> None:
        plan = plan_sheet_workflow_ingest(
            {"tasks": [task()]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
        )
        self.assertEqual(plan.create_requests, [])
        self.assertEqual(plan.decisions[0].reason, "automatic_creation_not_enabled")

    def test_same_row_is_ignored_and_business_change_is_quarantined(self) -> None:
        original = task()
        locator = sheet_row_locator(SPREADSHEET_ID, "2026.07", 12)
        previous = {locator: sheet_row_business_hash(original)}

        unchanged = plan_sheet_workflow_ingest(
            {"tasks": [original]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows=previous,
            allow_new_rows=True,
        )
        self.assertEqual(unchanged.decisions[0].reason, "row_already_seen")

        changed = plan_sheet_workflow_ingest(
            {"tasks": [task(customer_name="A DIFFERENT CUSTOMER")]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows=previous,
            allow_new_rows=True,
        )
        self.assertEqual(changed.create_requests, [])
        self.assertEqual(changed.decisions[0].action, "manual_review")
        self.assertEqual(changed.decisions[0].reason, "row_business_fields_changed")

    def test_empty_or_placeholder_pn_is_never_created(self) -> None:
        for pn in ("", "unknown", "待补充", None):
            with self.subTest(pn=pn):
                plan = plan_sheet_workflow_ingest(
                    {"tasks": [task(pn=pn)]},
                    spreadsheet_id=SPREADSHEET_ID,
                    previous_rows={},
                    allow_new_rows=True,
                )
                self.assertEqual(plan.create_requests, [])
                self.assertEqual(plan.decisions[0].reason, "pn_missing_or_placeholder")

    def test_completed_decision_running_and_existing_pla_are_rejected(self) -> None:
        cases = [
            (task(status="已完成", normalized_status="completed"), "row_completed"),
            (task(status="待决策", normalized_status="blocked"), "row_requires_decision_or_review"),
            (task(status="进行中", normalized_status="running"), "row_already_started"),
            (
                task(pla_no="PLA202607160001", pla_numbers=["PLA202607160001"]),
                "pla_already_exists",
            ),
        ]
        for row, reason in cases:
            with self.subTest(reason=reason):
                plan = plan_sheet_workflow_ingest(
                    {"tasks": [row]},
                    spreadsheet_id=SPREADSHEET_ID,
                    previous_rows={},
                    allow_new_rows=True,
                )
                self.assertEqual(plan.create_requests, [])
                self.assertEqual(plan.decisions[0].reason, reason)

    def test_unfinished_text_overrides_upstream_completed_substring_bug(self) -> None:
        plan = plan_sheet_workflow_ingest(
            {"tasks": [task(status="未完成", normalized_status="completed")]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        )
        self.assertEqual(len(plan.create_requests), 1)

    def test_idempotency_key_is_deterministic_and_bound_to_business_fields(self) -> None:
        first = plan_sheet_workflow_ingest(
            {"tasks": [task()]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        ).create_requests[0]
        second = plan_sheet_workflow_ingest(
            {"tasks": [task()]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        ).create_requests[0]
        changed = plan_sheet_workflow_ingest(
            {"tasks": [task(price_level="Customer Group")]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        ).create_requests[0]

        self.assertEqual(first.idempotency_key, second.idempotency_key)
        self.assertNotEqual(first.idempotency_key, changed.idempotency_key)
        self.assertLessEqual(len(first.idempotency_key or ""), 200)

        expected = sheet_row_idempotency_key(
            SPREADSHEET_ID,
            "2026.07",
            12,
            sheet_row_business_hash(task()),
        )
        self.assertEqual(first.idempotency_key, expected)

    def test_pn_cell_can_contain_deduplicated_line_or_comma_separated_values(self) -> None:
        plan = plan_sheet_workflow_ingest(
            {"tasks": [task(pn="PN-A, pn-a\nPN-B")]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        )
        self.assertEqual(plan.create_requests[0].pns, ["PN-A", "PN-B"])

    def test_duplicate_physical_row_in_one_payload_only_yields_one_decision(self) -> None:
        plan = plan_sheet_workflow_ingest(
            {"tasks": [task(), task(pn="PN-SECOND")]},
            spreadsheet_id=SPREADSHEET_ID,
            previous_rows={},
            allow_new_rows=True,
        )
        self.assertEqual(len(plan.decisions), 1)
        self.assertEqual(len(plan.create_requests), 1)

    def test_coordinator_bootstraps_then_creates_only_after_explicit_enable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = SheetWorkflowIngestCoordinator(Path(tmp) / "state.json")
            calls: list[object] = []

            def create(request: object) -> dict:
                calls.append(request)
                return {"task_id": "task-1", "state": "manual_review"}

            baseline = coordinator.process(
                {"tasks": [task()]},
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=True,
                create_workflow=create,
            )
            self.assertEqual(baseline["counts"], {"baseline": 1})
            self.assertEqual(calls, [])

            disabled = coordinator.process(
                {"tasks": [task(row_index=13, pn="PN-NEW")]},
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=False,
                create_workflow=create,
            )
            self.assertEqual(disabled["created"], [])

            enabled = coordinator.process(
                {"tasks": [task(row_index=13, pn="PN-NEW")]},
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=True,
                create_workflow=create,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(enabled["created"][0]["task_id"], "task-1")

            replay = coordinator.process(
                {"tasks": [task(row_index=13, pn="PN-NEW")]},
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=True,
                create_workflow=create,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(replay["counts"], {"ignore": 1})

    def test_coordinator_does_not_ack_failed_create_or_accept_another_spreadsheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = SheetWorkflowIngestCoordinator(Path(tmp) / "state.json")
            coordinator.process(
                {"tasks": [task()]},
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=False,
                create_workflow=lambda _request: {},
            )
            attempts = 0

            def fail(_request: object) -> dict:
                nonlocal attempts
                attempts += 1
                raise RuntimeError("pricing unavailable")

            candidate = {"tasks": [task(row_index=14, pn="PN-RETRY")]}
            first = coordinator.process(
                candidate,
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=True,
                create_workflow=fail,
            )
            second = coordinator.process(
                candidate,
                spreadsheet_id=SPREADSHEET_ID,
                allow_new_rows=True,
                create_workflow=fail,
            )
            self.assertFalse(first["ok"])
            self.assertFalse(second["ok"])
            self.assertEqual(attempts, 2)

            blocked = coordinator.process(
                candidate,
                spreadsheet_id="another-sheet",
                allow_new_rows=True,
                create_workflow=lambda _request: self.fail("must not create"),
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["blocked"], "spreadsheet_identity_changed")

    def test_existing_pending_rows_require_preview_hash_before_safe_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = SheetWorkflowIngestCoordinator(Path(tmp) / "state.json")
            parsed = {
                "tasks": [
                    task(row_index=10, pn="PN-PENDING"),
                    task(row_index=11, pn="PN-DONE", status="已完成", normalized_status="completed"),
                ]
            }
            plan = plan_sheet_workflow_backfill(parsed, spreadsheet_id=SPREADSHEET_ID)
            manifest = sheet_backfill_manifest_hash(plan)
            calls: list[object] = []

            preview = coordinator.backfill(
                parsed,
                spreadsheet_id=SPREADSHEET_ID,
                expected_manifest_hash=None,
                apply=False,
                limit=100,
                create_workflow=lambda request: calls.append(request),
            )
            self.assertEqual(preview["manifest_hash"], manifest)
            self.assertEqual(preview["candidate_count"], 1)
            self.assertEqual(calls, [])

            blocked = coordinator.backfill(
                parsed,
                spreadsheet_id=SPREADSHEET_ID,
                expected_manifest_hash="sha256:" + "0" * 64,
                apply=True,
                limit=100,
                create_workflow=lambda request: calls.append(request),
            )
            self.assertEqual(blocked["blocked"], "manifest_hash_mismatch")
            self.assertEqual(calls, [])

            def create(request: object) -> dict:
                calls.append(request)
                self.assertFalse(request.submission_authorized)
                self.assertFalse(request.notify)
                self.assertEqual(request.gsp_payload, {})
                return {"task_id": "backfill-1", "state": "manual_review"}

            applied = coordinator.backfill(
                parsed,
                spreadsheet_id=SPREADSHEET_ID,
                expected_manifest_hash=manifest,
                apply=True,
                limit=100,
                create_workflow=create,
            )
            self.assertTrue(applied["ok"])
            self.assertEqual(applied["created"][0]["task_id"], "backfill-1")
            self.assertFalse(applied["windows_agent_called"])


if __name__ == "__main__":
    unittest.main()
