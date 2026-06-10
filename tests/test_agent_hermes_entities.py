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
    AgentReplayEvalReq,
    AgentToolBackendHeartbeatReq,
)


class AgentHermesEntityTests(unittest.TestCase):
    def _agent(self, runtime: str) -> AgentAutomation:
        agent = AgentAutomation(Path(runtime))
        agent.ensure_dirs()
        (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
        task = {
            "sheet": "2026.06",
            "row_index": 23,
            "pla_no": "PLA20260605143508433",
            "pla_numbers": ["PLA20260605143508433"],
            "status": "进行中",
            "normalized_status": "running",
            "pn": "DH-PN-1",
            "requester": "Li",
        }
        agent._write_json(
            agent.sheet_parsed_dir / "latest.json",
            {
                "push_id": "push1",
                "summary": {"task_count": 1, "gsp_check_count": 1},
                "tasks": [task],
                "gsp_check_tasks": [task],
            },
        )
        return agent

    def test_queue_result_memory_trace_and_replay_eval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)

            queue = agent.gsp_status_queue(AgentGspQueueReq(token="token", limit=10))
            self.assertEqual(queue["count"], 1)
            item = queue["tasks"][0]
            self.assertTrue(item["run_id"].startswith("run_"))
            self.assertEqual(item["skill"]["name"], "check_gsp_approval_status")
            self.assertEqual(item["selected_tool_backend"]["capability"], "gsp.status_check")

            saved = agent.save_gsp_status_result(
                AgentGspStatusResultReq(
                    token="token",
                    run_id=item["run_id"],
                    queue_id=item["queue_id"],
                    pla_no=item["pla_no"],
                    status="Approved",
                    ok=True,
                    sheet=item["sheet"],
                    row_index=item["row_index"],
                    pn=item["pn"],
                    requester=item["requester"],
                    sheet_status=item["sheet_status"],
                )
            )
            self.assertTrue(saved["sheet_update"]["queued"])

            trace = agent.read_agent_trace(item["run_id"])
            self.assertEqual(trace["status"], "completed")
            self.assertTrue(any(s["name"] == "gsp_status_result" for s in trace["spans"]))

            timeline = agent.read_pla_timeline(item["pla_no"])
            self.assertEqual(timeline["pla_no"], item["pla_no"])
            self.assertTrue(timeline["sightings"])
            self.assertTrue(timeline["status_events"])
            self.assertTrue(timeline["writebacks"])
            compact = timeline["compact_memory"]
            self.assertEqual(compact["latest_gsp_status"], "Approved")
            self.assertEqual(compact["writeback_state"], "pending")
            self.assertEqual(compact["approved_status_count"], 1)

            evaluation = agent.run_replay_eval(AgentReplayEvalReq(token="token"))
            self.assertTrue(evaluation["ok"])
            self.assertEqual(evaluation["scores"]["false_writeback_count"], 0)
            self.assertEqual(evaluation["failures"]["missing_queue"], [])
            self.assertEqual(evaluation["failures"]["missing_writeback"], [])

    def test_failed_gsp_result_creates_reflection_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)
            queue = agent.gsp_status_queue(AgentGspQueueReq(token="token", limit=1))
            item = queue["tasks"][0]

            saved = agent.save_gsp_status_result(
                AgentGspStatusResultReq(
                    token="token",
                    run_id=item["run_id"],
                    queue_id=item["queue_id"],
                    pla_no=item["pla_no"],
                    status="ERROR",
                    ok=False,
                    sheet=item["sheet"],
                    row_index=item["row_index"],
                    error="simulated auth failure",
                )
            )

            reflection = saved["reflection"]
            self.assertEqual(reflection["kind"], "executor_reliability")
            self.assertEqual(reflection["state"], "proposed")
            self.assertEqual(agent.list_agent_reflections()["count"], 1)

    def test_skill_plan_graph_and_tool_backend_registry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(td)

            skill = agent.read_agent_skill("check_gsp_approval_status")
            version = skill["versions"][0]
            self.assertTrue(version["plan_graph"])
            self.assertEqual(version["plan_graph"][0]["step"], "context_assembly")

            heartbeat = agent.record_tool_backend_heartbeat(
                AgentToolBackendHeartbeatReq(
                    token="token",
                    backend_id="win-gsp-01",
                    display_name="Windows GSP Agent 01",
                    capabilities=["gsp.status_check", "gsp.browser_status_checker"],
                    status="online",
                    load=0.2,
                    metadata={"host": "windows-test"},
                )
            )
            self.assertTrue(heartbeat["ok"])

            backends = agent.list_tool_backends()
            ids = {b["backend_id"] for b in backends["tool_backends"]}
            self.assertIn("win-gsp-01", ids)

            queue = agent.gsp_status_queue(AgentGspQueueReq(token="token", limit=1))
            item = queue["tasks"][0]
            self.assertEqual(item["selected_tool_backend"]["backend_id"], "win-gsp-01")


if __name__ == "__main__":
    unittest.main()
