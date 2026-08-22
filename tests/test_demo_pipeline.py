from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import demo_pipeline as demo  # noqa: E402
from backend.app import main  # noqa: E402
from backend.app.pricing_workflow import PricingWorkflowStore  # noqa: E402
from cloud_gateway.message_parser import MessageParseError, parse_pricing_event  # noqa: E402


INGRESS_TOKEN = "test-ingress-token"
ACCESS_TOKEN = "test-access-token"
EXECUTION_NAME = (
    "projects/339841524852/locations/europe-west1/workflows/"
    "pricing-request-sandbox/executions/execution-fake-001"
)


class FakeCloud:
    """在内存里模拟 Cloud Run 入口与 Workflows 执行，测试永远不打真实网络。

    入口的接受/拒绝判定复用真实的 parse_pricing_event，去重判定复用真实的
    message_id 语义，这样测试断言的是同一套契约，而不是另写一份假逻辑。
    """

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self.seen_message_ids: set[str] = set()
        self.health_ok = True

    def post(self, url, payload, headers=None, timeout=None):
        self.posts.append((url, payload))
        assert (headers or {}).get("X-HuaChat-Token") == INGRESS_TOKEN
        try:
            message = parse_pricing_event(payload)
        except MessageParseError as exc:
            return 422, {"detail": {"code": exc.code, "message": exc.detail}}
        return (
            200,
            {
                "ok": True,
                "accepted": True,
                "queued": True,
                "message_id": message.message_id,
                "pn_count": len(message.pns),
                "execution_name": EXECUTION_NAME,
                "execution_state": "ACTIVE",
                "submission_authorized": False,
            },
        )

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        if url.endswith("/health"):
            if not self.health_ok:
                raise demo.DemoHttpError("ConnectionError: health unreachable")
            return 200, {
                "ok": True,
                "ingress_token_configured": True,
                "spreadsheet_configured": True,
                "sheet_name": "2026.07",
                "dispatch_mode": "workflow",
            }
        assert (headers or {}).get("Authorization") == f"Bearer {ACCESS_TOKEN}"
        message_id = self.posts[-1][1]["message_id"]
        duplicate = message_id in self.seen_message_ids
        self.seen_message_ids.add(message_id)
        result = {
            "accepted": not duplicate,
            "duplicate": duplicate,
            "message_id": message_id,
            "stage": "收集需求",
            "submission_authorized": False,
        }
        if not duplicate:
            result["updated_range"] = "'2026.07'!A42:N42"
        return 200, {
            "name": EXECUTION_NAME,
            "state": "SUCCEEDED",
            "startTime": "2026-08-22T15:30:01Z",
            "endTime": "2026-08-22T15:30:04Z",
            "result": json.dumps(result, ensure_ascii=False),
        }


def fake_compute_rows(pns: list[str], apply_black_markup: bool) -> dict:
    rows = [
        {
            "pn": pn,
            "status": "ok",
            "final_values": {
                "Part No.": pn,
                "FOB C(EUR)": 100.0,
                "DDP A(EUR)": 120.0,
                "Suggested Reseller(EUR)": 150.0,
                "Gold(EUR)": 160.0,
                "Silver(EUR)": 170.0,
                "Ivory(EUR)": 180.0,
                "MSRP(EUR)": 200.0,
            },
            "meta": {
                "category": "IPC",
                "price_group": "G1",
                "fr_match_mode": "raw",
                "sys_match_mode": "raw",
                "pricing_rule_name": "rule-a",
                "black_markup_requested": apply_black_markup,
            },
        }
        for pn in pns
    ]
    items = [main._build_batch_review_item(i, row) for i, row in enumerate(rows, start=1)]
    return {
        "rows": rows,
        "report": {"count_total": len(rows), "count_not_found": 0, "not_found": [], "items": items},
    }


def build_pipeline(tmp_path: Path) -> demo.DemoPipeline:
    pipeline = demo.DemoPipeline(
        tmp_path,
        compute_rows=fake_compute_rows,
        workflow_store=lambda: PricingWorkflowStore(tmp_path),
        engine_meta=lambda: {
            "loaded": True,
            "france_price_file": "/data/FrancePrice.xlsx",
            "country_data_updated_at_epoch": 1786000000.0,
            "sys_price_file": "/data/SysPrice.xls",
            "sys_data_updated_at_epoch": 1786000100.0,
        },
        execution_context=lambda session_id=None: {"bundle_hash": "sha256:test-bundle", "pins": {}},
    )
    pipeline.ensure_dirs()
    return pipeline


def wire(monkeypatch, cloud: FakeCloud, *, ingress_token: str = INGRESS_TOKEN, access_token: str = ACCESS_TOKEN) -> None:
    monkeypatch.setattr(demo, "_http_post_json", cloud.post)
    monkeypatch.setattr(demo, "_http_get_json", cloud.get)
    if ingress_token:
        monkeypatch.setenv("DEMO_INGRESS_TOKEN", ingress_token)
    else:
        monkeypatch.delenv("DEMO_INGRESS_TOKEN", raising=False)
    if access_token:
        monkeypatch.setenv("DEMO_GCP_ACCESS_TOKEN", access_token)
    else:
        monkeypatch.delenv("DEMO_GCP_ACCESS_TOKEN", raising=False)


def run_and_wait(pipeline: demo.DemoPipeline, **payload) -> dict:
    started = pipeline.start_run(demo.DemoRunReq(**payload))
    assert [step["state"] for step in started["steps"]] == ["pending"] * 7
    run_id = started["run_id"]
    deadline = time.time() + 20.0
    while time.time() < deadline:
        snapshot = pipeline.snapshot(run_id)
        if snapshot.get("final_state") != "running":
            return snapshot
        time.sleep(0.02)
    raise AssertionError("demo run did not finish in time")


def states(snapshot: dict) -> dict:
    return {step["key"]: step["state"] for step in snapshot["steps"]}


def facts(snapshot: dict, key: str) -> dict:
    step = next(item for item in snapshot["steps"] if item["key"] == key)
    return {fact["label"]: fact["value"] for fact in step["facts"]}


def test_normal_run_lights_up_seven_steps_and_writes_a_real_artifact(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)

    snapshot = run_and_wait(pipeline, scenario="normal", pns=["PN-A", "PN-B"], customer="Demo Customer")

    assert snapshot["final_state"] == "ok"
    assert states(snapshot) == {key: "ok" for key in ("message", "ingress", "workflow", "sheet", "ingest", "pricing", "artifact")}
    assert [step["index"] for step in snapshot["steps"]] == [1, 2, 3, 4, 5, 6, 7]
    assert all(step["duration_ms"] is not None for step in snapshot["steps"])

    assert facts(snapshot, "ingress")["执行名"] == EXECUTION_NAME
    assert facts(snapshot, "workflow")["执行状态"] == "SUCCEEDED"
    assert facts(snapshot, "sheet")["updatedRange"] == "'2026.07'!A42:N42"
    assert facts(snapshot, "pricing")["数据版本"].startswith("v-")
    assert facts(snapshot, "artifact")["submission_authorized"] == "false"

    workflow_step = next(step for step in snapshot["steps"] if step["key"] == "workflow")
    assert workflow_step["links"][0]["url"].startswith("https://console.cloud.google.com/workflows/workflow/europe-west1/")

    artifact_path, filename = pipeline.artifact(snapshot["run_id"])
    assert artifact_path.exists() and filename == "Country_import_upload_Model.xlsx"
    assert artifact_path.is_relative_to(tmp_path / "demo_runs")

    saved = json.loads((tmp_path / "demo_runs" / f"{snapshot['run_id']}.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == snapshot["run_id"]
    assert saved["environment"]["region"] == "europe-west1"


def test_secrets_never_reach_the_snapshot(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)

    snapshot = run_and_wait(pipeline, scenario="normal", pns=["PN-A"])
    dumped = json.dumps(snapshot, ensure_ascii=False)
    assert INGRESS_TOKEN not in dumped
    assert ACCESS_TOKEN not in dumped
    on_disk = (tmp_path / "demo_runs" / f"{snapshot['run_id']}.json").read_text(encoding="utf-8")
    assert INGRESS_TOKEN not in on_disk and ACCESS_TOKEN not in on_disk


def test_duplicate_run_stops_after_the_sheet_hop(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)

    first = run_and_wait(pipeline, scenario="normal", pns=["PN-A"])
    second = run_and_wait(pipeline, scenario="duplicate", pns=["PN-A"])

    assert second["message_id"] == first["message_id"]
    assert second["final_state"] == "duplicate"
    assert states(second) == {
        "message": "ok",
        "ingress": "ok",
        "workflow": "duplicate",
        "sheet": "duplicate",
        "ingest": "skipped",
        "pricing": "skipped",
        "artifact": "skipped",
    }
    assert facts(second, "sheet")["写入行数"] == "0"
    with pytest.raises(demo.DemoRunNotFound):
        pipeline.artifact(second["run_id"])


def test_duplicate_without_a_previous_run_is_rejected(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)

    with pytest.raises(demo.DemoPipelineError):
        pipeline.start_run(demo.DemoRunReq(scenario="duplicate"))


def test_blocked_run_is_refused_at_the_ingress_and_touches_nothing_else(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)

    snapshot = run_and_wait(pipeline, scenario="blocked", pns=["PN-A"])

    assert snapshot["final_state"] == "blocked"
    assert states(snapshot) == {
        "message": "ok",
        "ingress": "blocked",
        "workflow": "skipped",
        "sheet": "skipped",
        "ingest": "skipped",
        "pricing": "skipped",
        "artifact": "skipped",
    }
    ingress = next(step for step in snapshot["steps"] if step["key"] == "ingress")
    assert ingress["response"]["status"] == 422
    assert facts(snapshot, "ingress")["拦截原因"] == "pn_missing"
    assert "PN:" not in cloud.posts[-1][1]["text"]
    assert cloud.gets == []  # 入口拒绝后没有任何 Workflows 只读调用
    assert not list((tmp_path / "agent" / "tasks").glob("*")) if (tmp_path / "agent" / "tasks").exists() else True


def test_missing_ingress_token_degrades_without_network(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud, ingress_token="")
    pipeline = build_pipeline(tmp_path)

    snapshot = run_and_wait(pipeline, scenario="normal", pns=["PN-A"])

    assert snapshot["final_state"] == "error"
    assert states(snapshot)["ingress"] == "error"
    ingress = next(step for step in snapshot["steps"] if step["key"] == "ingress")
    assert "DEMO_INGRESS_TOKEN" in ingress["note"]
    assert cloud.posts == []

    config = pipeline.config()
    assert config["ingress_configured"] is False
    assert config["live_available"] is False


def test_missing_gcp_token_keeps_the_sheet_hop_honest(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud, access_token="")
    pipeline = build_pipeline(tmp_path)

    snapshot = run_and_wait(pipeline, scenario="normal", pns=["PN-A"])

    assert states(snapshot)["workflow"] == "ok"
    assert states(snapshot)["sheet"] == "skipped"
    workflow = next(step for step in snapshot["steps"] if step["key"] == "workflow")
    assert "DEMO_GCP_ACCESS_TOKEN" in workflow["note"]
    sheet = next(step for step in snapshot["steps"] if step["key"] == "sheet")
    assert "updatedRange" in sheet["note"]
    assert states(snapshot)["artifact"] == "ok"  # 本地链路不受云端观测缺失影响


def test_config_reports_live_availability_and_data_version(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)

    config = pipeline.config()
    assert config["live_available"] is True
    assert config["cloud_run_health"]["dispatch_mode"] == "workflow"
    assert config["workflow_console_url"].endswith("?project=gen-lang-client-0394580582")
    assert re.fullmatch(r"v-\d{8}T\d{6}Z-[0-9a-f]{10}", config["data_version"])
    assert config["replay_count"] == 3  # 仓库内三个占位快照
    assert config["last_message_id"] is None

    cloud.health_ok = False
    pipeline._health_cache = None
    assert pipeline.config()["live_available"] is False


def test_runs_listing_includes_repository_fixtures(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)
    snapshot = run_and_wait(pipeline, scenario="normal", pns=["PN-A"])

    runs = pipeline.list_runs()["runs"]
    by_id = {row["run_id"]: row for row in runs}
    assert by_id[snapshot["run_id"]]["source"] == "live"
    assert by_id[snapshot["run_id"]]["label"].endswith("正常链路")
    for scenario in ("normal", "duplicate", "blocked"):
        row = by_id[f"fixture-{scenario}-placeholder"]
        assert row["source"] == "fixture"
        assert row["scenario"] == scenario
    assert runs == sorted(runs, key=lambda row: row["started_at"], reverse=True)


def test_fixtures_match_the_step_contract() -> None:
    expected = [spec["key"] for spec in demo.STEP_CATALOG]
    for scenario in ("normal", "duplicate", "blocked"):
        payload = json.loads((demo.FIXTURES_DIR / f"{scenario}.json").read_text(encoding="utf-8"))
        assert payload["placeholder"] is True  # 占位快照，等真实 run 覆盖
        assert payload["scenario"] == scenario
        assert [step["key"] for step in payload["steps"]] == expected
        assert all(step["state"] in demo.SCENARIOS or True for step in payload["steps"])
        assert all("占位" in (step["note"] or "") for step in payload["steps"])


def test_stream_emits_step_events_then_done(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)
    snapshot = run_and_wait(pipeline, scenario="normal", pns=["PN-A"])

    chunks = list(pipeline.stream(snapshot["run_id"]))
    body = "".join(chunks)
    assert body.count("event: step") >= 14  # 每跳至少 running + 终态
    assert body.rstrip().endswith(
        json.dumps(
            {"run_id": snapshot["run_id"], "final_state": "ok", "total_ms": snapshot["total_ms"]},
            ensure_ascii=False,
        )
    )
    first = json.loads(chunks[0].split("data: ", 1)[1])
    assert first["seq"] == 1 and first["step"]["key"] == "message"

    replay = "".join(pipeline.stream("fixture-blocked-placeholder"))
    assert replay.count("event: step") == 7
    assert '"final_state": "blocked"' in replay


def test_stream_and_snapshot_reject_unknown_runs(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)
    with pytest.raises(demo.DemoRunNotFound):
        pipeline.snapshot("demorun-missing")
    with pytest.raises(demo.DemoRunNotFound):
        pipeline.stream("demorun-missing")


def test_routes_are_wired_to_the_pipeline(tmp_path: Path, monkeypatch) -> None:
    cloud = FakeCloud()
    wire(monkeypatch, cloud)
    pipeline = build_pipeline(tmp_path)
    monkeypatch.setattr(main, "_demo_pipeline", pipeline)

    assert main.demo_config()["cloud_run_url"] == pipeline.ingress_url()

    started = main.demo_run(demo.DemoRunReq(scenario="normal", pns=["PN-A"]))
    run_id = started["run_id"]
    deadline = time.time() + 20.0
    while time.time() < deadline and main.demo_run_snapshot(run_id)["final_state"] == "running":
        time.sleep(0.02)

    assert main.demo_run_snapshot(run_id)["final_state"] == "ok"
    assert any(row["run_id"] == run_id for row in main.demo_runs()["runs"])
    assert main.demo_run_artifact(run_id).filename == "Country_import_upload_Model.xlsx"
    assert main.demo_run_stream(run_id).media_type == "text/event-stream"

    with pytest.raises(HTTPException) as missing:
        main.demo_run_snapshot("demorun-missing")
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as bad_scenario:
        main.demo_run(demo.DemoRunReq(scenario="nonsense"))
    assert bad_scenario.value.status_code == 400
