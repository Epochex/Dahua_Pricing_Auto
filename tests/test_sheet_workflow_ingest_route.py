from __future__ import annotations

from pathlib import Path

from fastapi import Response
from starlette.requests import Request

from backend.app import main
from backend.app.agent_ops import AgentAutomation, AgentSheetPushReq
from backend.app.pricing_workflow import PricingWorkflowStore
from backend.app.sheet_workflow_ingest import SheetWorkflowIngestCoordinator
from backend.app.evolution.control_plane import EvolutionControlPlane


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _payload(rows: list[list[str]], snapshot: str) -> dict:
    return {
        "file": "询价任务状态表",
        "spreadsheetId": "sheet-route-test",
        "snapshotHash": snapshot,
        "idempotencyKey": f"sheet-route-test:{snapshot}",
        "sheets": [{"name": "2026.07", "rows": rows}],
    }


def test_sheet_push_route_baselines_then_creates_a_safe_manual_review_task(tmp_path: Path, monkeypatch) -> None:
    agent = AgentAutomation(tmp_path)
    agent.ensure_dirs()
    (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
    agent._write_json(agent.config_path, agent._default_config())
    workflows = PricingWorkflowStore(tmp_path)
    coordinator = SheetWorkflowIngestCoordinator(tmp_path / "agent" / "sheet_workflow_ingest" / "state.json")

    monkeypatch.setattr(main, "_agent", agent)
    monkeypatch.setattr(main, "_pricing_workflows", workflows)
    monkeypatch.setattr(main, "_sheet_workflow_ingest", coordinator)
    monkeypatch.setattr(
        main,
        "_agent_compute_rows",
        lambda pns, _apply: {
            "rows": [{"pn": pn, "status": "ok"} for pn in pns],
            "report": {"not_found": []},
        },
    )

    headers = ["请求发起人", "请求任务描述", "PN码(Part Number)", "价格应用层级", "状态"]
    first_rows = [["说明"], [], [], headers, ["Li", "定价", "PN-BASELINE", "Country", "待处理"]]
    first = main.agent_sheet_push(
        AgentSheetPushReq(token="token", source="google-sheets", payload=_payload(first_rows, "snapshot-1")),
        _request(),
        Response(),
    )
    assert first["workflow_ingest"]["counts"] == {"baseline": 1}
    assert workflows.list()["count"] == 0

    cfg = agent.read_config(redacted=False)
    cfg["sheet_workflow_auto_create_enabled"] = True
    agent._write_json(agent.config_path, cfg)
    second_rows = [
        ["说明"],
        [],
        [],
        headers,
        ["Li", "定价", "PN-BASELINE", "Country", "待处理"],
        ["Li", "定价", "PN-NEW", "Country", "待处理"],
    ]
    second = main.agent_sheet_push(
        AgentSheetPushReq(token="token", source="google-sheets", payload=_payload(second_rows, "snapshot-2")),
        _request(),
        Response(),
    )
    assert second["workflow_ingest"]["ok"] is True
    assert len(second["workflow_ingest"]["created"]) == 1

    tasks = workflows.list()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["state"] == "manual_review"
    assert tasks[0]["submission"]["authorized"] is False
    assert workflows.claim(worker_id="windows", capabilities=["submit_gsp"], lease_seconds=120)["claimed"] is False


def test_existing_sheet_backfill_is_preview_bound_and_never_calls_windows(tmp_path: Path, monkeypatch) -> None:
    agent = AgentAutomation(tmp_path)
    agent.ensure_dirs()
    (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
    cfg = agent._default_config()
    cfg.update({"dry_run": True, "group_reply_enabled": False, "poller_enabled": False})
    agent._write_json(agent.config_path, cfg)
    workflows = PricingWorkflowStore(tmp_path)
    coordinator = SheetWorkflowIngestCoordinator(tmp_path / "agent" / "sheet_workflow_ingest" / "state.json")
    evolution = EvolutionControlPlane(tmp_path)
    evolution.ensure_baseline()
    parsed = {
        "tasks": [
            {
                "sheet": "2026.07",
                "row_index": 8,
                "requester": "Li",
                "description": "定价",
                "pn": "PN-BACKFILL",
                "price_level": "Country",
                "status": "待处理",
                "normalized_status": "pending",
                "pla_no": "",
                "pla_numbers": [],
            }
        ]
    }

    monkeypatch.setattr(main, "_agent", agent)
    monkeypatch.setattr(main, "_pricing_workflows", workflows)
    monkeypatch.setattr(main, "_sheet_workflow_ingest", coordinator)
    monkeypatch.setattr(main, "_evolution", evolution)
    monkeypatch.setattr(agent, "read_parsed_sheet_push", lambda _push_id: parsed)
    monkeypatch.setattr(
        main,
        "_agent_compute_rows",
        lambda pns, _apply: {
            "rows": [{"pn": pn, "status": "ok"} for pn in pns],
            "report": {"not_found": []},
        },
    )

    preview = main.agent_sheet_workflow_backfill(
        main.SheetWorkflowBackfillReq(token="token", source_id="sheet-production", apply=False)
    )
    assert preview["candidate_count"] == 1
    assert "created" not in preview

    applied = main.agent_sheet_workflow_backfill(
        main.SheetWorkflowBackfillReq(
            token="token",
            source_id="sheet-production",
            apply=True,
            expected_manifest_hash=preview["manifest_hash"],
        )
    )
    assert applied["ok"] is True
    assert applied["windows_agent_called"] is False
    task = workflows.list()["tasks"][0]
    assert task["state"] == "manual_review"
    assert task["submission"]["authorized"] is False
