from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent_ops import (  # noqa: E402
    AgentAutomation,
    AgentGspQueueReq,
    AgentGspStatusResultReq,
    AgentToolBackendHeartbeatReq,
)
from backend.app.dingtalk_stream_worker import build_reply  # noqa: E402


class AgentGroupReplyPolicyTests(unittest.TestCase):
    def _agent(self, runtime: str) -> AgentAutomation:
        agent = AgentAutomation(Path(runtime))
        agent.ensure_dirs()
        (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
        return agent

    def test_incoming_without_mention_cannot_reply_to_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.config_path,
                {
                    **agent._default_config(),
                    "dry_run": False,
                    "group_reply_enabled": True,
                },
            )
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1"},
                    "tasks": [],
                    "gsp_check_tasks": [],
                },
            )

            result = agent.handle_dingtalk_event(
                {"text": {"content": "帮我查审批 2026.06"}, "senderStaffId": "me"},
                source="dingtalk-stream",
            )

            self.assertTrue(result["matched"])
            self.assertFalse(result["event"]["mentioned_bot"])
            self.assertFalse(result["event"]["reply_policy"]["can_reply_to_group"])

    def test_incoming_mention_records_reply_policy_but_dry_run_blocks_group_reply(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.config_path,
                {
                    **agent._default_config(),
                    "dry_run": True,
                    "group_reply_enabled": True,
                    "reply_to_mentions_only": True,
                },
            )
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1"},
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
                    "gsp_check_tasks": [
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
                },
            )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 帮我查审批 2026.06"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertTrue(result["event"]["mentioned_bot"])
            self.assertFalse(result["event"]["reply_policy"]["can_reply_to_group"])
            self.assertTrue(result["event"]["reply_policy"]["dry_run"])

    def test_month_shorthand_filters_to_single_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.config_path,
                {
                    **agent._default_config(),
                    "dry_run": True,
                    "group_reply_enabled": True,
                },
            )
            tasks = [
                {
                    "sheet": "2026.07",
                    "row_index": 11,
                    "pla_no": "PLA20260703082642388",
                    "pla_numbers": ["PLA20260703082642388"],
                    "status": "进行中",
                    "pn": "PN-JULY",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.06",
                    "row_index": 127,
                    "pla_no": "PLA20260702084935730",
                    "pla_numbers": ["PLA20260702084935730"],
                    "status": "进行中",
                    "pn": "PN-JUNE",
                    "requester": "xiyan",
                },
            ]
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1"},
                    "tasks": tasks,
                    "gsp_check_tasks": tasks,
                },
            )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 帮我查审批 2026.7"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertEqual(result["command"]["sheet"], "2026.07")
            self.assertEqual(result["queue"]["count"], 1)
            self.assertEqual(result["queue"]["tasks"][0]["sheet"], "2026.07")

    def test_chat_query_ignores_limit_words_and_queues_all_running_pla_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.config_path,
                {
                    **agent._default_config(),
                    "dry_run": True,
                    "group_reply_enabled": True,
                },
            )
            tasks = [
                {
                    "sheet": "2026.07",
                    "row_index": 11,
                    "pla_no": "PLA20260703082642388",
                    "pla_numbers": ["PLA20260703082642388"],
                    "status": "进行中",
                    "pn": "PN-1",
                    "internal_model": "MODEL-1",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 14,
                    "pla_no": "PLA20260703152251994",
                    "pla_numbers": ["PLA20260703152251994"],
                    "status": "进行中",
                    "pn": "PN-2",
                    "internal_model": "MODEL-2",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 15,
                    "pla_no": "PLA20260703152251994",
                    "pla_numbers": ["PLA20260703152251994"],
                    "status": "进行中",
                    "pn": "PN-2B",
                    "internal_model": "MODEL-2B",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 20,
                    "pla_no": "PLA20260704000000000",
                    "pla_numbers": ["PLA20260704000000000"],
                    "status": "阻塞",
                    "normalized_status": "blocked",
                    "pn": "PN-BLOCKED",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 21,
                    "pla_no": "",
                    "pla_numbers": [],
                    "status": "进行中",
                    "pn": "PN-NO-PLA",
                    "requester": "zhenbang",
                },
            ]
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1"},
                    "tasks": tasks,
                    "gsp_check_tasks": tasks,
                },
            )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 查在线表格PLA 2026.07 只查1条"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertEqual(result["command"]["limit"], 500)
            self.assertEqual(result["queue"]["count"], 2)
            self.assertEqual([t["row_index"] for t in result["queue"]["tasks"]], [11, 14])
            self.assertEqual(len(result["queue"]["tasks"][1]["related_rows"]), 2)
            self.assertEqual(
                [r["row_index"] for r in result["queue"]["tasks"][1]["related_rows"]],
                [14, 15],
            )
            reply = build_reply(result)
            self.assertEqual(reply, "")

    def test_chat_query_without_month_defaults_to_latest_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            tasks = [
                {
                    "sheet": "2026.07",
                    "row_index": 11,
                    "pla_no": "PLA20260703082642388",
                    "pla_numbers": ["PLA20260703082642388"],
                    "status": "进行中",
                    "pn": "PN-JULY",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.06",
                    "row_index": 127,
                    "pla_no": "PLA20260702084935730",
                    "pla_numbers": ["PLA20260702084935730"],
                    "status": "进行中",
                    "pn": "PN-JUNE",
                    "requester": "xiyan",
                },
            ]
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {
                        "push_id": "push1",
                        "sheets": [
                            {"name": "2026.07"},
                            {"name": "2026.06"},
                            {"name": "2025.12-2026.01"},
                        ],
                    },
                    "tasks": tasks,
                    "gsp_check_tasks": tasks,
                },
            )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 哥们，查下pla"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertEqual(result["command"]["sheet"], "2026.07")
            self.assertEqual(result["queue"]["count"], 1)
            self.assertEqual(result["queue"]["tasks"][0]["sheet"], "2026.07")

    def test_chat_query_with_chinese_month_filters_that_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            tasks = [
                {
                    "sheet": "2026.07",
                    "row_index": 11,
                    "pla_no": "PLA20260703082642388",
                    "pla_numbers": ["PLA20260703082642388"],
                    "status": "进行中",
                    "pn": "PN-JULY",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.06",
                    "row_index": 127,
                    "pla_no": "PLA20260702084935730",
                    "pla_numbers": ["PLA20260702084935730"],
                    "status": "进行中",
                    "pn": "PN-JUNE",
                    "requester": "xiyan",
                },
            ]
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1", "sheets": [{"name": "2026.06"}, {"name": "2026.07"}]},
                    "tasks": tasks,
                    "gsp_check_tasks": tasks,
                },
            )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人，查pla，7月"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertEqual(result["command"]["sheet"], "2026.07")
            self.assertEqual(result["queue"]["count"], 1)
            self.assertEqual(result["queue"]["tasks"][0]["pn"], "PN-JULY")

    def test_chat_query_triggers_responsive_windows_agent_when_control_url_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent.record_tool_backend_heartbeat(
                AgentToolBackendHeartbeatReq(
                    token="token",
                    backend_id="windows-gsp-agent",
                    display_name="Windows GSP Agent",
                    capabilities=["gsp.status_check", "gsp.status_api"],
                    status="online",
                    load=0.0,
                    metadata={"control_url": "http://127.0.0.1:8765"},
                )
            )
            task = {
                "sheet": "2026.07",
                "row_index": 17,
                "pla_no": "PLA20260707090253190",
                "pla_numbers": ["PLA20260707090253190"],
                "status": "进行中",
                "pn": "PN-JULY",
                "requester": "zhenbang",
            }
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1", "sheets": [{"name": "2026.07"}]},
                    "tasks": [task],
                    "gsp_check_tasks": [task],
                },
            )
            posted: list[dict] = []

            def fake_post(control_url: str, payload: dict) -> dict:
                posted.append({"control_url": control_url, "payload": payload})
                return {"ok": True, "status": 202, "response": {"accepted": True}}

            agent._post_agent_control_run_once = fake_post  # type: ignore[method-assign]

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 查下pla"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["responsive_trigger"]["triggered"])
            self.assertEqual(posted[0]["control_url"], "http://127.0.0.1:8765")
            self.assertEqual(posted[0]["payload"]["sheet"], "2026.07")
            self.assertEqual(posted[0]["payload"]["max_tasks"], 1)

    def test_sheet_queue_includes_latest_gsp_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1"},
                    "tasks": [],
                    "gsp_check_tasks": [
                        {
                            "sheet": "2026.07",
                            "row_index": 11,
                            "pla_no": "PLA20260703082642388",
                            "pla_numbers": ["PLA20260703082642388"],
                            "status": "进行中",
                            "pn": "PN-JULY",
                            "requester": "zhenbang",
                        }
                    ],
                },
            )
            with (agent.gsp_status_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
                fh.write(
                    '{"pla_no":"PLA20260703082642388","sheet":"2026.07","row_index":11,'
                    '"status":"Under Approval","ok":true,'
                    '"approval_current_step":"Country Product Manager",'
                    '"approval_taskers":"LEON HOU(30195)",'
                    '"checked_at":"2026-07-06T14:09:21+00:00","source":"windows-desktop-agent"}\n'
                )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 查在线表格PLA 2026.07"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            gsp = result["queue"]["tasks"][0]["latest_gsp_result"]
            self.assertEqual(gsp["status"], "Under Approval")
            self.assertEqual(gsp["approval_current_step"], "Country Product Manager")
            self.assertEqual(gsp["approval_taskers"], "LEON HOU(30195)")

    def test_under_approval_intent_does_not_query_sheet_queue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 查一下当前GSP所有待审批"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertEqual(result["command"]["intent"], "scan_gsp_under_approval")
            self.assertEqual(result["queue"]["count"], 0)
            self.assertIn("pending_integration", result)

    def test_stuck_owner_summary_intent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 今天谁手上卡着最多"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                },
                source="dingtalk",
            )

            self.assertTrue(result["matched"])
            self.assertEqual(result["command"]["intent"], "scan_gsp_under_approval")

    def test_deepseek_fallback_command_is_coerced_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)

            def fake_deepseek(text: str) -> dict:
                return agent._coerce_intent_command(
                    {
                        "intent": "check_sheet_pla",
                        "sheet": "2026.7",
                        "country": "FR",
                        "limit": 5,
                        "confidence": 0.91,
                    },
                    text,
                    source="deepseek",
                )

            agent._parse_deepseek_intent = fake_deepseek  # type: ignore[method-assign]
            command = agent._parse_approval_command("看看七月这个表还有啥没批", allow_llm=True)

            self.assertTrue(command["matched"])
            self.assertEqual(command["intent"], "check_sheet_pla")
            self.assertEqual(command["source"], "deepseek")
            self.assertEqual(command["sheet"], "2026.07")
            self.assertEqual(command["limit"], 500)

    def test_unknown_mentioned_request_is_recorded_for_future_logic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.config_path,
                {
                    **agent._default_config(),
                    "llm_intent_enabled": False,
                },
            )

            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 帮我自动催一下法国区所有没报价的人"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                    "conversationId": "cid1",
                },
                source="dingtalk",
            )

            self.assertFalse(result["matched"])
            self.assertTrue(result["unsupported"])
            self.assertTrue(result["unsupported_request_id"])
            path = agent.unsupported_request_dir / f"{result['unsupported_request_id']}.json"
            self.assertTrue(path.exists())
            record = agent._read_json(path, {})
            self.assertEqual(record["status"], "new")
            self.assertIn("自动催", record["text"])

    def test_async_reply_is_sent_after_all_gsp_results_arrive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            agent._write_json(
                agent.config_path,
                {
                    **agent._default_config(),
                    "dry_run": False,
                    "group_reply_enabled": True,
                },
            )
            tasks = [
                {
                    "sheet": "2026.07",
                    "row_index": 11,
                    "pla_no": "PLA20260703082642388",
                    "pla_numbers": ["PLA20260703082642388"],
                    "status": "进行中",
                    "pn": "PN-1",
                    "internal_model": "MODEL-1",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 14,
                    "pla_no": "PLA20260703152251994",
                    "pla_numbers": ["PLA20260703152251994"],
                    "status": "进行中",
                    "pn": "PN-2",
                    "internal_model": "MODEL-2",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 15,
                    "pla_no": "PLA20260703152251994",
                    "pla_numbers": ["PLA20260703152251994"],
                    "status": "进行中",
                    "pn": "PN-2B",
                    "internal_model": "MODEL-2B",
                    "requester": "zhenbang",
                },
                {
                    "sheet": "2026.07",
                    "row_index": 16,
                    "pla_no": "PLA20260703152251994",
                    "pla_numbers": ["PLA20260703152251994"],
                    "status": "进行中",
                    "pn": "PN-2C",
                    "internal_model": "MODEL-2C",
                    "requester": "zhenbang",
                },
            ]
            agent._write_json(
                agent.sheet_parsed_dir / "latest.json",
                {
                    "push_id": "push1",
                    "summary": {"push_id": "push1"},
                    "tasks": tasks,
                    "gsp_check_tasks": tasks,
                },
            )
            sent: list[dict] = []

            def fake_post(session_webhook: str, text: str, *, at_user_id: str = "") -> dict:
                sent.append({"session_webhook": session_webhook, "text": text, "at_user_id": at_user_id})
                return {"ok": True, "status": 200, "response": {"errcode": 0}}

            agent._post_dingtalk_session_text = fake_post  # type: ignore[method-assign]
            result = agent.handle_dingtalk_event(
                {
                    "text": {"content": "@机器人 查在线表格PLA 2026.07"},
                    "isInAtList": True,
                    "senderStaffId": "me",
                    "senderId": "sender-open-id",
                    "sessionWebhook": "https://example.invalid/sessionWebhook",
                    "sessionWebhookExpiredTime": "9999999999999",
                },
                source="dingtalk",
            )
            run_id = result["queue"]["run_id"]
            session_id = result["queue"]["session_id"]
            pulled = agent.gsp_status_queue(AgentGspQueueReq(token="token", limit=500, sheet="2026.07"))
            self.assertEqual(pulled["run_id"], run_id)
            self.assertEqual(pulled["session_id"], session_id)
            self.assertEqual(pulled["count"], 2)
            self.assertEqual([t["row_index"] for t in pulled["tasks"]], [11, 14])
            self.assertEqual(len(pulled["tasks"][1]["related_rows"]), 3)

            first = agent.save_gsp_status_result(
                AgentGspStatusResultReq(
                    token="token",
                    run_id=run_id,
                    session_id=session_id,
                    queue_id=result["queue"]["tasks"][0]["queue_id"],
                    pla_no="PLA20260703082642388",
                    status="Approved",
                    ok=True,
                    sheet="2026.07",
                    row_index=11,
                    pn="PN-1",
                    requester="zhenbang",
                    sheet_status="进行中",
                    approval_current_step="End",
                    approval_taskers="",
                    source="windows-desktop-agent",
                )
            )
            self.assertFalse(first["async_reply"]["sent"])
            self.assertEqual(sent, [])

            second = agent.save_gsp_status_result(
                AgentGspStatusResultReq(
                    token="token",
                    run_id=run_id,
                    session_id=session_id,
                    queue_id=result["queue"]["tasks"][1]["queue_id"],
                    pla_no="PLA20260703152251994",
                    status="Under Approval",
                    ok=True,
                    sheet="2026.07",
                    row_index=14,
                    pn="PN-2",
                    requester="zhenbang",
                    sheet_status="进行中",
                    related_rows=result["queue"]["tasks"][1]["related_rows"],
                    approval_current_step="Country Product Manager",
                    approval_taskers="LEON HOU(30195)",
                    source="windows-desktop-agent",
                )
            )
            self.assertTrue(second["async_reply"]["sent"])
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["at_user_id"], "me")
            self.assertIn("1. row 11 / PLA20260703082642388", sent[0]["text"])
            self.assertIn("内部型号：MODEL-1", sent[0]["text"])
            self.assertIn("requester: @zhenbang", sent[0]["text"])
            self.assertIn("GSP: 审批已通过 Approved", sent[0]["text"])
            self.assertIn("2. row 14 / PLA20260703152251994", sent[0]["text"])
            self.assertIn("内部型号：\n2.1 MODEL-2\n2.2 MODEL-2B\n2.3 MODEL-2C", sent[0]["text"])
            self.assertNotIn("3. row 15 / PLA20260703152251994", sent[0]["text"])
            self.assertIn("requester: zhenbang", sent[0]["text"])
            self.assertIn("GSP: 还在审批中", sent[0]["text"])
            self.assertIn("GSP: 审批已通过 Approved\n------\n2. row 14", sent[0]["text"])
            self.assertNotIn("Windows GSP 实时查询回来了", sent[0]["text"])
            self.assertNotIn("表格原状态", sent[0]["text"])
            self.assertNotIn("checked", sent[0]["text"])
            self.assertIn("审批人：Country Product Manager - LEON HOU(30195)", sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
