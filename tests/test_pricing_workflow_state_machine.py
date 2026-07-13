from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.pricing_workflow import (  # noqa: E402
    PricingWorkflowCreateReq,
    PricingWorkflowStore,
    WorkflowConflict,
)


def successful_pricing(pns: list[str], apply_black_markup: bool) -> dict:
    return {
        "rows": [
            {
                "pn": pn,
                "status": "ok",
                "final_values": {"Suggested Reseller(EUR)": 100.0},
                "meta": {"black_markup_requested": apply_black_markup},
            }
            for pn in pns
        ],
        "report": {"count_total": len(pns), "count_not_found": 0, "not_found": []},
    }


class PricingWorkflowStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.store = PricingWorkflowStore(self.runtime)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def create_task(self, **overrides: object) -> dict:
        payload = {
            "pns": ["DH-IPC-001"],
            "source": "test",
            "idempotency_key": "request-001",
            "max_submission_attempts": 2,
            "max_verification_attempts": 3,
            "gsp_payload": {"application": {"countryCode": "FR"}},
            "submission_authorized": True,
        }
        payload.update(overrides)
        return self.store.create(PricingWorkflowCreateReq(**payload), successful_pricing)

    def claim(self, *capabilities: str) -> dict:
        return self.store.claim(
            worker_id="windows-1",
            capabilities=list(capabilities) or ["submit_gsp", "verify_gsp_submission", "check_gsp_approval"],
            lease_seconds=120,
        )

    def report(self, claim: dict, outcome: str, *, report_id: str, pla_no: str | None = None) -> dict:
        return self.store.report(
            claim["task_id"],
            worker_id="windows-1",
            lease_id=claim["lease"]["lease_id"],
            report_id=report_id,
            outcome=outcome,
            pla_no=pla_no,
        )

    def test_unknown_submit_must_verify_then_found_reaches_approval(self) -> None:
        task = self.create_task()
        self.assertEqual(task["state"], "submitting")

        submit = self.claim("submit_gsp")
        self.report(submit, "timeout", report_id="submit-timeout")
        self.assertEqual(self.store.read(task["task_id"])["state"], "verifying")

        verify = self.claim("verify_gsp_submission")
        self.report(verify, "found", report_id="verify-found", pla_no="PLA202607130001")
        self.assertEqual(self.store.read(task["task_id"])["state"], "under_approval")

        approval = self.claim("check_gsp_approval")
        final = self.report(approval, "approved", report_id="approval-approved")
        self.assertEqual(final["state"], "approved")
        persisted = self.store.read(task["task_id"])
        self.assertEqual(persisted["submission"]["attempts"], 1)
        self.assertEqual(persisted["submission"]["pla_no"], "PLA202607130001")
        self.assertEqual([step["seq"] for step in persisted["trace"]], list(range(1, len(persisted["trace"]) + 1)))

    def test_expired_submit_lease_is_never_reclaimed_as_submit(self) -> None:
        task = self.create_task()
        submit = self.claim("submit_gsp")
        path = self.store._state_path(task["task_id"])
        state = json.loads(path.read_text(encoding="utf-8"))
        state["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        path.write_text(json.dumps(state), encoding="utf-8")

        no_submit = self.claim("submit_gsp")
        self.assertFalse(no_submit["claimed"])
        persisted = self.store.read(task["task_id"])
        self.assertEqual(persisted["state"], "verifying")
        self.assertEqual(persisted["submission"]["attempts"], 1)
        self.assertEqual(persisted["submission"]["last_outcome"], "lease_expired_result_unknown")

        verify = self.claim("verify_gsp_submission")
        self.assertTrue(verify["claimed"])
        self.assertEqual(verify["action"], "verify_gsp_submission")

    def test_confirmed_absent_allows_bounded_safe_retry(self) -> None:
        task = self.create_task()
        submit_one = self.claim("submit_gsp")
        self.report(submit_one, "unknown", report_id="submit-1")
        verify_one = self.claim("verify_gsp_submission")
        retry = self.report(verify_one, "absent", report_id="verify-1")
        self.assertEqual(retry["state"], "submitting")

        submit_two = self.claim("submit_gsp")
        self.report(submit_two, "unknown", report_id="submit-2")
        verify_two = self.claim("verify_gsp_submission")
        stopped = self.report(verify_two, "absent", report_id="verify-2")
        self.assertEqual(stopped["state"], "manual_review")
        self.assertEqual(self.store.read(task["task_id"])["submission"]["attempts"], 2)

    def test_incomplete_application_only_resumes_when_local_ledger_is_proven(self) -> None:
        task = self.create_task(max_submission_attempts=1)
        submit = self.claim("submit_gsp")
        self.report(submit, "unknown", report_id="submit-unknown")
        verify = self.claim("verify_gsp_submission")
        resumed = self.store.report(
            task["task_id"],
            worker_id="windows-1",
            lease_id=verify["lease"]["lease_id"],
            report_id="verify-incomplete",
            outcome="incomplete",
            pla_no="PLA-INCOMPLETE",
            evidence={"resumable": True},
        )
        self.assertEqual(resumed["state"], "submitting")
        resume_claim = self.claim("submit_gsp")
        self.assertTrue(resume_claim["claimed"])
        self.assertTrue(resume_claim["submission"]["resume_only"])
        self.assertEqual(resume_claim["submission"]["attempts"], 1)

    def test_incomplete_application_without_ledger_requires_manual_review(self) -> None:
        task = self.create_task()
        submit = self.claim("submit_gsp")
        self.report(submit, "unknown", report_id="submit-unknown")
        verify = self.claim("verify_gsp_submission")
        result = self.store.report(
            task["task_id"],
            worker_id="windows-1",
            lease_id=verify["lease"]["lease_id"],
            report_id="verify-incomplete-no-ledger",
            outcome="incomplete",
            pla_no="PLA-INCOMPLETE",
            evidence={"resumable": False},
        )
        self.assertEqual(result["state"], "manual_review")

    def test_duplicate_report_is_idempotent_and_stale_lease_is_rejected(self) -> None:
        self.create_task()
        submit = self.claim("submit_gsp")
        first = self.report(submit, "unknown", report_id="same-report")
        second = self.report(submit, "unknown", report_id="same-report")
        self.assertEqual(first["version"], second["version"])
        self.assertTrue(second["idempotent_replay"])

        with self.assertRaises(WorkflowConflict):
            self.store.report(
                submit["task_id"],
                worker_id="windows-1",
                lease_id=submit["lease"]["lease_id"],
                report_id="different-report",
                outcome="unknown",
            )

    def test_idempotency_key_returns_same_task_without_repricing(self) -> None:
        calls = 0

        def counted(pns: list[str], apply_black_markup: bool) -> dict:
            nonlocal calls
            calls += 1
            return successful_pricing(pns, apply_black_markup)

        req = PricingWorkflowCreateReq(pns=["A"], idempotency_key="stable-key")
        first = self.store.create(req, counted)
        second = self.store.create(req, counted)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(calls, 1)

    def test_only_one_worker_can_claim_a_side_effect(self) -> None:
        self.create_task()
        results: list[dict] = []

        def attempt(worker: str) -> None:
            results.append(
                self.store.claim(
                    worker_id=worker,
                    capabilities=["submit_gsp"],
                    lease_seconds=120,
                )
            )

        threads = [threading.Thread(target=attempt, args=(f"worker-{idx}",)) for idx in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(bool(item["claimed"]) for item in results), 1)
        self.assertEqual(self.store.read(self.create_task()["task_id"])["submission"]["attempts"], 1)

    def test_not_found_pricing_goes_to_manual_review_without_submission(self) -> None:
        def not_found(pns: list[str], apply_black_markup: bool) -> dict:
            _ = apply_black_markup
            return {
                "rows": [{"pn": pns[0], "status": "not_found"}],
                "report": {"not_found": [pns[0]], "count_not_found": 1},
            }

        task = self.store.create(
            PricingWorkflowCreateReq(pns=["MISSING"], idempotency_key="missing"),
            not_found,
        )
        self.assertEqual(task["state"], "manual_review")
        self.assertFalse(self.claim("submit_gsp")["claimed"])

    def test_unapproved_pricing_task_cannot_enter_submission_queue(self) -> None:
        task = self.store.create(
            PricingWorkflowCreateReq(
                pns=["A"],
                idempotency_key="pricing-only",
                gsp_payload={"private": "template"},
                submission_authorized=False,
            ),
            successful_pricing,
        )
        self.assertEqual(task["state"], "manual_review")
        self.assertFalse(task["submission"]["authorized"])
        self.assertFalse(self.claim("submit_gsp")["claimed"])
        public = self.store.redact(task)
        self.assertEqual(public["input"]["gsp_payload"], {"present": True})
        self.assertNotIn("private", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
