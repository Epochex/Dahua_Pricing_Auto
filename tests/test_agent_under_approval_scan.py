from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.agent_ops import (  # noqa: E402
    AgentAutomation,
    AgentGspStatusResultReq,
    AgentGspUnderApprovalQueueReq,
    AgentGspUnderApprovalResultReq,
    AgentSheetPushReq,
    AgentToolBackendHeartbeatReq,
)


class AgentUnderApprovalScanTests(unittest.TestCase):
    def _agent(self, root: str, *, dry_run: bool = True) -> AgentAutomation:
        agent = AgentAutomation(Path(root))
        agent.ensure_dirs()
        (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
        agent._write_json(
            agent.config_path,
            {
                **agent._default_config(),
                "dry_run": dry_run,
                "group_reply_enabled": True,
                "llm_intent_enabled": False,
            },
        )
        agent.record_tool_backend_heartbeat(
            AgentToolBackendHeartbeatReq(
                token="token",
                backend_id="windows-gsp",
                capabilities=["gsp.status_check", "gsp.under_approval_scan"],
                metadata={"control_url": "http://127.0.0.1:8765"},
            )
        )
        return agent

    def test_chat_scan_dispatch_result_summary_and_replay_are_closed_loop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td, dry_run=True)
            control_calls: list[dict] = []
            outbound_calls: list[dict] = []

            def fake_control(url: str, payload: dict) -> dict:
                control_calls.append({"url": url, "payload": payload})
                return {"ok": True, "status": 202}

            def forbidden_outbound(*args: object, **kwargs: object) -> dict:
                outbound_calls.append({"args": list(args), "kwargs": kwargs})
                raise AssertionError("dry-run scan must never call DingTalk outbound")

            agent._post_agent_control_run_once = fake_control  # type: ignore[method-assign]
            agent._post_dingtalk_session_text = forbidden_outbound  # type: ignore[method-assign]
            chat = agent.handle_dingtalk_event(
                {
                    "msgId": "msg-under-approval-1",
                    "text": {"content": "@机器人 查一下当前GSP所有待审批，谁手上最多"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                    "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession",
                },
                source="dingtalk",
            )

            self.assertTrue(chat["matched"])
            self.assertEqual(chat["command"]["intent"], "scan_gsp_under_approval")
            self.assertEqual(chat["queue"]["count"], 1)
            self.assertNotIn("pending_integration", chat)
            self.assertEqual(len(control_calls), 1)
            operation = control_calls[0]["payload"]
            self.assertEqual(operation["operation"], "scan_under_approval")
            self.assertEqual(operation["scan_id"], chat["scan"]["scan_id"])

            pulled = agent.gsp_under_approval_queue(AgentGspUnderApprovalQueueReq(token="token", limit=10))
            self.assertEqual(pulled["count"], 1)
            self.assertEqual(pulled["jobs"][0]["scan_id"], chat["scan"]["scan_id"])

            request = AgentGspUnderApprovalResultReq(
                token="token",
                scan_id=chat["scan"]["scan_id"],
                run_id=chat["scan"]["run_id"],
                session_id=chat["scan"]["session_id"],
                report_id="report-stable-1",
                ok=True,
                total=3,
                checked_at="2026-07-16T12:00:00+00:00",
                items=[
                    {
                        "pla_no": "PLA-1",
                        "status": "Under Approval",
                        "approval_current_step": "Country Manager",
                        "approval_taskers": "Alice",
                        "age_days": 5,
                    },
                    {
                        "pla_no": "PLA-2",
                        "status": "Under Approval",
                        "approval_current_step": "Country Manager",
                        "approval_taskers": "Alice; Bob",
                        "age_days": 2,
                    },
                    {
                        "pla_no": "PLA-3",
                        "status": "Under Approval",
                        "approval_current_step": "Finance",
                        "approval_taskers": "Bob",
                        "age_days": 1,
                    },
                ],
            )
            saved = agent.save_gsp_under_approval_result(request)
            self.assertFalse(saved["replayed"])
            self.assertFalse(saved["reply"]["sent"])
            self.assertIn("blocked by current policy", saved["reply"]["reason"])
            self.assertEqual(saved["summary"]["reported_total"], 3)
            self.assertEqual(saved["summary"]["by_approver"][0], {"name": "Alice", "count": 2})
            self.assertIn("当前待办最多：Alice 2条", saved["reply_preview"])
            self.assertEqual(outbound_calls, [])

            replayed = agent.save_gsp_under_approval_result(request)
            self.assertTrue(replayed["replayed"])
            self.assertEqual(outbound_calls, [])
            self.assertEqual(len(list(agent.gsp_under_approval_result_dir.glob("*.json"))), 1)
            self.assertEqual(
                agent._read_json(agent._under_approval_job_path(chat["scan"]["scan_id"]), {})["state"],
                "completed",
            )

    def test_current_dry_run_policy_blocks_reply_even_if_enabled_at_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td, dry_run=False)
            agent._post_agent_control_run_once = lambda *_args, **_kwargs: {"ok": True, "status": 202}  # type: ignore[method-assign]
            chat = agent.handle_dingtalk_event(
                {
                    "msgId": "msg-policy-flip",
                    "text": {"content": "@机器人 查当前GSP所有待审批"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                    "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession",
                },
                source="dingtalk",
            )
            cfg = agent.read_config(redacted=False)
            cfg["dry_run"] = True
            agent._write_json(agent.config_path, cfg)
            agent._post_dingtalk_session_text = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
                AssertionError("outbound must be blocked after policy flip")
            )
            saved = agent.save_gsp_under_approval_result(
                AgentGspUnderApprovalResultReq(
                    token="token",
                    scan_id=chat["scan"]["scan_id"],
                    run_id=chat["scan"]["run_id"],
                    session_id=chat["scan"]["session_id"],
                    report_id="report-policy-flip",
                    items=[],
                )
            )
            self.assertFalse(saved["reply"]["sent"])
            self.assertIn("blocked by current policy", saved["reply"]["reason"])

    def test_google_sheet_snapshot_idempotency_reuses_original_push(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            payload = {
                "file": "询价任务状态表",
                "spreadsheetId": "sheet-123",
                "snapshotHash": "abc123",
                "idempotencyKey": "sheet-123:abc123",
                "sheets": [
                    {
                        "name": "2026.07",
                        "rows": [
                            ["说明"],
                            [],
                            [],
                            ["请求发起人", "请求任务描述", "PN码(Part Number)", "价格应用层级", "状态"],
                            ["Li", "定价", "PN-1", "Country", "进行中"],
                        ],
                    }
                ],
            }
            first = agent.receive_sheet_push(AgentSheetPushReq(token="token", source="google-sheets", payload=payload))
            second = agent.receive_sheet_push(AgentSheetPushReq(token="token", source="google-sheets", payload=payload))

            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(second["push_id"], first["push_id"])
            self.assertEqual(len(list(agent.event_dir.glob("*.json"))), 1)
            self.assertEqual(len(list(agent.trace_dir.glob("*.json"))), 1)

    def test_single_gsp_result_report_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            run_id = agent._start_trace(source="test", intent="check_gsp_approval_status")
            request = AgentGspStatusResultReq(
                token="token",
                report_id="gsp-report-1",
                run_id=run_id,
                pla_no="PLA-1",
                status="Under Approval",
                ok=True,
                checked_at="2026-07-16T12:00:00+00:00",
            )
            first = agent.save_gsp_status_result(request)
            second = agent.save_gsp_status_result(request)
            self.assertNotIn("replayed", first)
            self.assertTrue(second["replayed"])
            lines = (agent.gsp_status_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
