from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi import Response
from starlette.requests import Request

from backend.app import main
from backend.app.agent_ops import AgentAutomation, AgentSheetPushReq
from backend.app.pricing_workflow import PricingWorkflowStore
from backend.app.sheet_workflow_ingest import SheetWorkflowIngestCoordinator
from cloud_gateway.app import GatewaySettings, create_app
from cloud_gateway.message_parser import MessageParseError, parse_pricing_event
from cloud_gateway.sheets import GoogleSheetsTaskWriter, SheetAppendResult
from cloud_gateway.workflow_dispatch import GoogleWorkflowTaskDispatcher, WorkflowDispatchResult


class RecordingWriter:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.rows: list[list[str]] = []
        self.duplicate = duplicate

    def append_if_absent(self, row: list[str]) -> SheetAppendResult:
        self.rows.append(row)
        return SheetAppendResult(
            appended=not self.duplicate,
            duplicate=self.duplicate,
            updated_range="'2026.07'!A11:N11" if not self.duplicate else "",
        )


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def dispatch(self, *, message_id: str, row: list[str]) -> WorkflowDispatchResult:
        self.calls.append((message_id, row))
        return WorkflowDispatchResult(
            execution_name=(
                "projects/gen-lang-client-0394580582/locations/europe-west1/"
                "workflows/pricing-request-sandbox/executions/execution-001"
            ),
            state="ACTIVE",
        )


class FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, existing_notes: list[str]) -> None:
        self.existing_notes = existing_notes
        self.posts: list[tuple[str, dict]] = []

    def get(self, _url: str, timeout: int) -> FakeResponse:
        assert timeout == 20
        return FakeResponse(200, {"values": [self.existing_notes]})

    def post(self, url: str, json: dict, timeout: int) -> FakeResponse:
        assert timeout == 20
        self.posts.append((url, json))
        return FakeResponse(200, {"updates": {"updatedRange": "'2026.07'!A11:N11"}})


class FakeWorkflowSession:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict, timeout: int) -> FakeResponse:
        assert timeout == 20
        self.posts.append((url, json))
        return FakeResponse(
            200,
            {
                "name": (
                    "projects/gen-lang-client-0394580582/locations/europe-west1/"
                    "workflows/pricing-request-sandbox/executions/execution-001"
                ),
                "state": "ACTIVE",
            },
        )


def _payload(message_id: str = "hc-001") -> dict:
    return {
        "message_id": message_id,
        "created_at": "2026-08-03T14:00:00Z",
        "sender": {"name": "Sandbox User"},
        "text": (
            "请做定价\n"
            "PN: DEMO-PN-002, DEMO-PN-003, DEMO-PN-002\n"
            "层级: Country\n"
            "客户: Demo Customer\n"
            "产品线: Video\n"
            "内部型号: Demo Model\n"
            "截止: 2026-08-10"
        ),
    }


def test_parser_builds_the_existing_a_to_n_sheet_contract() -> None:
    message = parse_pricing_event(_payload(), now=datetime(2026, 8, 3, tzinfo=timezone.utc))
    assert message.pns == ("DEMO-PN-002", "DEMO-PN-003")
    row = message.to_sheet_row()
    assert len(row) == 14
    assert row[0] == "Sandbox User"
    assert row[3] == "DEMO-PN-002, DEMO-PN-003"
    assert row[5] == "Country"
    assert row[11:13] == ["尚未开始", "收集需求"]
    assert row[13] == "[HUACHAT:hc-001] 2026-08-03T14:00:00Z"


def test_parser_rejects_unrelated_or_unidentified_messages() -> None:
    with pytest.raises(MessageParseError, match="pricing intent"):
        parse_pricing_event({"message_id": "x", "text": "今天开会吗"})
    with pytest.raises(MessageParseError, match="explicit 'PN:'"):
        parse_pricing_event({"message_id": "x", "text": "请帮我定价"})
    with pytest.raises(MessageParseError, match="message_id"):
        parse_pricing_event({"text": "定价\nPN: X"})


def test_gateway_requires_token_and_appends_only_a_safe_row() -> None:
    writer = RecordingWriter()
    app = create_app(
        settings=GatewaySettings(ingress_token="secret", spreadsheet_id="sheet-id", sheet_name="2026.07"),
        writer=writer,
    )
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/events/huachat")
    with pytest.raises(HTTPException) as unauthorized:
        endpoint(_payload(), authorization=None, x_huachat_token=None)
    assert unauthorized.value.status_code == 401

    body = endpoint(_payload(), authorization=None, x_huachat_token="secret")
    assert body["accepted"] is True
    assert body["submission_authorized"] is False
    assert writer.rows[0][11:13] == ["尚未开始", "收集需求"]


def test_google_writer_checks_message_id_before_append() -> None:
    row = parse_pricing_event(_payload()).to_sheet_row()

    duplicate_session = FakeSession(["old note", "[HUACHAT:hc-001] 2026-08-03T14:00:00Z"])
    duplicate_writer = GoogleSheetsTaskWriter(
        spreadsheet_id="sheet-id", sheet_name="2026.07", session=duplicate_session
    )
    duplicate = duplicate_writer.append_if_absent(row)
    assert duplicate.duplicate is True
    assert duplicate_session.posts == []

    new_session = FakeSession(["[HUACHAT:another-id]"])
    new_writer = GoogleSheetsTaskWriter(spreadsheet_id="sheet-id", sheet_name="2026.07", session=new_session)
    created = new_writer.append_if_absent(row)
    assert created.appended is True
    assert created.updated_range == "'2026.07'!A11:N11"
    assert len(new_session.posts) == 1
    assert new_session.posts[0][1]["values"] == [row]


def test_gateway_can_queue_the_safe_row_in_google_workflows() -> None:
    dispatcher = RecordingDispatcher()
    app = create_app(
        settings=GatewaySettings(
            ingress_token="secret",
            spreadsheet_id="sheet-id",
            sheet_name="2026.07",
            workflow_execution_url="https://workflowexecutions.googleapis.com/v1/projects/p/locations/l/workflows/w/executions",
        ),
        dispatcher=dispatcher,
    )
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/events/huachat")

    body = endpoint(_payload(), authorization="Bearer secret", x_huachat_token=None)

    assert body["accepted"] is True
    assert body["queued"] is True
    assert body["execution_state"] == "ACTIVE"
    assert body["submission_authorized"] is False
    assert dispatcher.calls[0][0] == "hc-001"
    assert dispatcher.calls[0][1][11:13] == ["尚未开始", "收集需求"]


def test_google_workflow_dispatch_serializes_unicode_without_changing_the_row() -> None:
    session = FakeWorkflowSession()
    execution_url = (
        "https://workflowexecutions.googleapis.com/v1/projects/gen-lang-client-0394580582/"
        "locations/europe-west1/workflows/pricing-request-sandbox/executions"
    )
    dispatcher = GoogleWorkflowTaskDispatcher(execution_url=execution_url, session=session)
    row = parse_pricing_event(_payload()).to_sheet_row()

    result = dispatcher.dispatch(message_id="hc-001", row=row)

    assert result.state == "ACTIVE"
    assert result.execution_name.endswith("/executions/execution-001")
    posted = session.posts[0][1]
    assert posted["executionHistoryLevel"] == "EXECUTION_HISTORY_BASIC"
    assert "尚未开始" in posted["argument"]
    assert "\\u5c1a" not in posted["argument"]


def test_chat_row_reaches_safe_manual_review_workflow(tmp_path, monkeypatch) -> None:
    agent = AgentAutomation(tmp_path)
    agent.ensure_dirs()
    (agent.agent_dir / "sheet_push_token.txt").write_text("token", encoding="utf-8")
    cfg = agent._default_config()
    cfg["sheet_workflow_auto_create_enabled"] = True
    agent._write_json(agent.config_path, cfg)
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

    headers = [
        "请求发起人以及相关人员",
        "请求任务描述",
        "产品线",
        "PN码(Part Number)",
        "内部型号",
        "价格应用层级",
        "客户名称",
        "是否对产品进行发布",
        "移除截止日期",
        "PLA NO.",
        "负责执行人",
        "状态",
        "阶段",
        "当前操作状态细节以及问题备注",
    ]
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    def push(rows: list[list[str]], snapshot: str) -> dict:
        return main.agent_sheet_push(
            AgentSheetPushReq(
                token="token",
                source="google-sheets-sandbox",
                payload={
                    "file": "自动化定价工作流沙箱",
                    "spreadsheetId": "chat-sheet-sandbox",
                    "snapshotHash": snapshot,
                    "idempotencyKey": f"chat-sheet-sandbox:{snapshot}",
                    "sheets": [{"name": "2026.07", "rows": rows}],
                },
            ),
            request,
            Response(),
        )

    initial_rows = [["说明"], [], [], headers]
    assert push(initial_rows, "baseline")["workflow_ingest"]["counts"] == {}

    chat_row = parse_pricing_event(_payload()).to_sheet_row()
    created = push([*initial_rows, chat_row], "with-chat-row")
    assert created["workflow_ingest"]["counts"] == {"create": 1}
    task = workflows.list()["tasks"][0]
    assert task["state"] == "manual_review"
    assert task["submission"]["authorized"] is False
    assert workflows.claim(worker_id="windows", capabilities=["submit_gsp"], lease_seconds=120)["claimed"] is False
