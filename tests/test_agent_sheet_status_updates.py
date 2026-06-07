from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent_ops import (  # noqa: E402
    AgentAutomation,
    AgentGspStatusResultReq,
    AgentSheetStatusUpdateAckReq,
    AgentSheetStatusUpdatesReq,
)


class AgentSheetStatusUpdateTests(unittest.TestCase):
    def _agent(self, runtime: str) -> AgentAutomation:
        agent = AgentAutomation(Path(runtime))
        agent.ensure_dirs()
        (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
        agent._write_json(
            agent.sheet_parsed_dir / "latest.json",
            {
                "push_id": "push1",
                "tasks": [
                    {
                        "sheet": "2026.06",
                        "row_index": 23,
                        "pla_no": "PLA20260605143508433",
                        "pla_numbers": ["PLA20260605143508433"],
                        "status": "进行中",
                        "pn": "DH-PN-1",
                        "requester": "Li",
                    }
                ],
                "gsp_check_tasks": [],
            },
        )
        return agent

    def test_approved_gsp_result_queues_l_column_completion_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            saved = agent.save_gsp_status_result(
                AgentGspStatusResultReq(
                    token="token",
                    pla_no="PLA20260605143508433",
                    status="Approved",
                    sheet="2026.06",
                    row_index=23,
                    sheet_status="进行中",
                    approval_current_step="End",
                )
            )

            self.assertTrue(saved["sheet_update"]["queued"])
            pending = agent.sheet_status_updates(AgentSheetStatusUpdatesReq(token="token"))

            self.assertEqual(pending["count"], 1)
            update = pending["updates"][0]
            self.assertEqual(update["cell"], "L23")
            self.assertEqual(update["new_status"], "已完成")
            self.assertEqual(update["pla_no"], "PLA20260605143508433")

            ack = agent.ack_sheet_status_updates(
                AgentSheetStatusUpdateAckReq(token="token", update_ids=[update["update_id"]])
            )
            self.assertEqual(ack["acked"], 1)
            self.assertEqual(agent.sheet_status_updates(AgentSheetStatusUpdatesReq(token="token"))["count"], 0)

    def test_under_approval_does_not_queue_sheet_update(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            saved = agent.save_gsp_status_result(
                AgentGspStatusResultReq(
                    token="token",
                    pla_no="PLA20260605143508433",
                    status="Under Approval",
                    sheet="2026.06",
                    row_index=23,
                    sheet_status="进行中",
                )
            )

            self.assertFalse(saved["sheet_update"]["queued"])
            self.assertEqual(agent.sheet_status_updates(AgentSheetStatusUpdatesReq(token="token"))["count"], 0)


if __name__ == "__main__":
    unittest.main()
