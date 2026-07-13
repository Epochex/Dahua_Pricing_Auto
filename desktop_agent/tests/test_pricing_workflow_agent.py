from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pricing_workflow_agent as agent  # noqa: E402


def submission_claim() -> dict:
    return {
        "claimed": True,
        "task_id": "pricing-abc",
        "effect_key": "DPA:pricing-abc",
        "action": "submit_gsp",
        "lease": {"lease_id": "lease-1"},
        "submission": {"pla_no": None},
        "input": {
            "gsp_payload": {
                "application": {"countryCode": "FR", "description": "request {{effect_key}}"},
                "save_application_and_product": {
                    "priceListApplicationId": "{{pla_no}}",
                    "products": [{"partNum": "PN-1"}],
                },
                "create_price_work": {"priceListApplicationId": "{{pla_no}}"},
                "start_price_work": {
                    "priceListApplicationId": "{{pla_no}}",
                    "workflowId": "{{workflow_id}}",
                },
            }
        },
    }


class FakeClient(agent.GspPricingWorkflowClient):
    def __init__(self, ledger: agent.WorkflowLedger, *, fail_path: str | None = None):
        self.cfg = {"gsp_submission_enabled": True}
        self.ledger = ledger
        self.fail_path = fail_path
        self.calls: list[tuple[str, dict]] = []

    def find_by_effect_key(self, effect_key: str) -> dict:
        return {"found": False, "confirmed_absent": True, "effect_key": effect_key}

    def _post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if path == self.fail_path:
            raise requests.Timeout("simulated timeout")
        if path == agent.INSERT_APPLICATION_PATH:
            return {"code": 0, "data": {"priceListApplicationId": "PLA-100"}}
        if path == agent.CREATE_WORKFLOW_PATH:
            return {"code": 0, "data": {"workflowId": "WF-200"}}
        return {"code": 0, "data": {}}


class PricingWorkflowAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = agent.WorkflowLedger(Path(self.tmp.name) / "ledger.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_submission_calls_verified_endpoint_chain_and_persists_stages(self) -> None:
        client = FakeClient(self.ledger)
        result = client.submit(submission_claim())

        self.assertEqual(result["outcome"], "submitted")
        self.assertEqual(result["pla_no"], "PLA-100")
        self.assertEqual(
            [path for path, _payload in client.calls],
            [
                agent.INSERT_APPLICATION_PATH,
                agent.SAVE_APPLICATION_PATH,
                agent.CREATE_WORKFLOW_PATH,
                agent.START_WORKFLOW_PATH,
            ],
        )
        application_payload = client.calls[0][1]
        self.assertIn("DPA:pricing-abc", application_payload["description"])
        self.assertIn("DPA:pricing-abc", application_payload["comment"])
        self.assertEqual(client.calls[1][1]["priceListApplicationId"], "PLA-100")
        self.assertEqual(client.calls[3][1]["workflowId"], "WF-200")
        execution = self.ledger.execution("DPA:pricing-abc")
        self.assertEqual(execution["pla_no"], "PLA-100")
        self.assertIn("workflow_started", execution["stages"])

    def test_timeout_after_create_resumes_without_creating_duplicate_application(self) -> None:
        first = FakeClient(self.ledger, fail_path=agent.SAVE_APPLICATION_PATH)
        result = agent.execute_claim({}, submission_claim(), first)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(self.ledger.execution("DPA:pricing-abc")["pla_no"], "PLA-100")

        second = FakeClient(self.ledger)
        resumed = second.submit(submission_claim())
        self.assertEqual(resumed["outcome"], "submitted")
        self.assertNotIn(agent.INSERT_APPLICATION_PATH, [path for path, _payload in second.calls])
        self.assertEqual(second.calls[0][0], agent.SAVE_APPLICATION_PATH)

    def test_all_templates_are_validated_before_first_write(self) -> None:
        claim = submission_claim()
        del claim["input"]["gsp_payload"]["start_price_work"]
        client = FakeClient(self.ledger)
        result = client.submit(claim)
        self.assertEqual(result["outcome"], "invalid_payload")
        self.assertEqual(client.calls, [])

    def test_verification_distinguishes_submitted_incomplete_and_absent(self) -> None:
        client = FakeClient(self.ledger)
        claim = submission_claim()
        for found, expected in (
            ({"found": True, "pla_no": "PLA-1", "submitted": True}, "found"),
            ({"found": True, "pla_no": "PLA-1", "submitted": False, "resumable": False}, "incomplete"),
            ({"found": False, "confirmed_absent": True}, "absent"),
            ({"found": False, "confirmed_absent": False}, "unknown"),
        ):
            with patch.object(client, "find_by_effect_key", return_value=found):
                self.assertEqual(client.verify(claim)["outcome"], expected)

    def test_submit_capability_is_not_advertised_until_explicitly_enabled(self) -> None:
        cfg = {
            "agent_token": "token",
            "worker_id": "worker",
            "workflow_lease_seconds": 120,
            "gsp_submission_enabled": False,
        }
        with patch.object(agent, "api_post", return_value={"claimed": False}) as post:
            agent.claim_workflow(cfg)
        payload = post.call_args.args[2]
        self.assertNotIn("submit_gsp", payload["capabilities"])
        self.assertIn("verify_gsp_submission", payload["capabilities"])

    def test_pending_report_is_durable_and_contains_no_auth_file_data(self) -> None:
        payload = {
            "task_id": "pricing-abc",
            "lease_id": "lease-1",
            "report_id": "report:lease-1",
            "outcome": "unknown",
        }
        self.ledger.put_pending_report("report:lease-1", payload)
        persisted = json.loads(self.ledger.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["pending_reports"]["report:lease-1"]["payload"]["task_id"], "pricing-abc")
        self.assertNotIn("gsp_password", json.dumps(persisted))
        self.assertNotIn("access_token", json.dumps(persisted))
        self.assertNotIn("transport-token", json.dumps(persisted))


if __name__ == "__main__":
    unittest.main()
