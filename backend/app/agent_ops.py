from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.engine.core.formatter import build_export_frames, write_export_xlsx
from backend.engine.core.loader import parse_pn_list_file


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentConfigReq(BaseModel):
    enabled: bool = Field(default=True)
    mode: str = Field(default="pricing_ops")
    notification_keyword: str = Field(default="定价Agent")
    webhook_url: Optional[str] = Field(default=None)
    webhook_secret: Optional[str] = Field(default=None)
    notify_on_task_done: bool = Field(default=True)
    notify_on_task_failed: bool = Field(default=True)
    poller_enabled: bool = Field(default=False)
    poll_interval_seconds: int = Field(default=60, ge=10, le=3600)
    sheet_source_type: str = Field(default="file")
    sheet_source_path: str = Field(default="")
    apply_black_markup: bool = Field(default=True)
    dry_run: bool = Field(default=False)


class AgentNotifyTestReq(BaseModel):
    message: str = Field(default="测试消息：自动化系统已接入当前服务器")


class AgentPricingTaskReq(BaseModel):
    pns: List[str] = Field(default_factory=list)
    source: str = Field(default="manual")
    notify: bool = Field(default=True)
    apply_black_markup: Optional[bool] = Field(default=None)


class AgentSheetProbeReq(BaseModel):
    source_path: Optional[str] = Field(default=None)


class AgentSheetPushReq(BaseModel):
    source: str = Field(default="alidocs-script")
    token: Optional[str] = Field(default=None)
    payload: Any = Field(default_factory=dict)


class AgentGspQueueReq(BaseModel):
    token: Optional[str] = Field(default=None)
    limit: int = Field(default=50, ge=1, le=500)
    sheet: Optional[str] = Field(default=None)


class AgentGspStatusResultReq(BaseModel):
    token: Optional[str] = Field(default=None)
    pla_no: str = Field(default="")
    status: str = Field(default="")
    ok: bool = Field(default=True)
    sheet: Optional[str] = Field(default=None)
    row_index: Optional[int] = Field(default=None)
    pn: Optional[str] = Field(default=None)
    requester: Optional[str] = Field(default=None)
    checked_at: Optional[str] = Field(default=None)
    source: str = Field(default="windows-desktop-agent")
    detail: Optional[str] = Field(default=None)
    error: Optional[str] = Field(default=None)
    raw: Any = Field(default=None)


ComputeRows = Callable[[List[str], bool], Dict[str, Any]]


class AgentAutomation:
    def __init__(self, runtime_dir: Path):
        self.runtime_dir = runtime_dir
        self.agent_dir = runtime_dir / "agent"
        self.tasks_dir = self.agent_dir / "tasks"
        self.sheet_cache_dir = self.agent_dir / "sheet_cache"
        self.sheet_push_dir = self.agent_dir / "sheet_push"
        self.sheet_parsed_dir = self.agent_dir / "sheet_parsed"
        self.gsp_status_dir = self.agent_dir / "gsp_status"
        self.config_path = self.agent_dir / "config.json"
        self.state_path = self.agent_dir / "state.json"
        self._poller_thread: Optional[threading.Thread] = None
        self._poller_stop = threading.Event()
        self._lock = threading.Lock()

    def ensure_dirs(self) -> None:
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.sheet_cache_dir.mkdir(parents=True, exist_ok=True)
        self.sheet_push_dir.mkdir(parents=True, exist_ok=True)
        self.sheet_parsed_dir.mkdir(parents=True, exist_ok=True)
        self.gsp_status_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self._write_json(self.config_path, self._default_config())
        if not self.state_path.exists():
            self._write_json(
                self.state_path,
                {
                    "created_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "last_source_hash": None,
                    "last_poll_at": None,
                    "last_task_id": None,
                    "last_error": None,
                },
            )

    def _default_config(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "mode": "pricing_ops",
            "notification_keyword": "定价Agent",
            "webhook_url": "",
            "webhook_secret": "",
            "notify_on_task_done": True,
            "notify_on_task_failed": True,
            "poller_enabled": False,
            "poll_interval_seconds": 60,
            "sheet_source_type": "file",
            "sheet_source_path": "",
            "apply_black_markup": True,
            "dry_run": False,
        }

    def _read_json(self, path: Path, fallback: Any) -> Any:
        try:
            if not path.exists():
                return fallback
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return fallback

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _task_state_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "state.json"

    def _write_task_state(self, task_id: str, state: Dict[str, Any]) -> None:
        self._write_json(self._task_state_path(task_id), state)

    def read_config(self, *, redacted: bool = True) -> Dict[str, Any]:
        self.ensure_dirs()
        cfg = self._default_config()
        cfg.update(self._read_json(self.config_path, {}))
        if os.getenv("DAHUA_AGENT_WEBHOOK_URL"):
            cfg["webhook_url"] = os.getenv("DAHUA_AGENT_WEBHOOK_URL", "")
        if os.getenv("DAHUA_AGENT_WEBHOOK_SECRET"):
            cfg["webhook_secret"] = os.getenv("DAHUA_AGENT_WEBHOOK_SECRET", "")
        cfg["poll_interval_seconds"] = max(10, min(3600, int(cfg.get("poll_interval_seconds") or 60)))
        if redacted:
            out = dict(cfg)
            out["webhook_url"] = self._redact_url(str(out.get("webhook_url") or ""))
            out["webhook_secret"] = ""
            out["has_webhook_url"] = bool(cfg.get("webhook_url"))
            out["has_webhook_secret"] = bool(cfg.get("webhook_secret"))
            out["config_path"] = str(self.config_path)
            out["state_path"] = str(self.state_path)
            return out
        return cfg

    def save_config(self, req: AgentConfigReq) -> Dict[str, Any]:
        self.ensure_dirs()
        current = self.read_config(redacted=False)
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        if payload.get("webhook_url") is None:
            payload["webhook_url"] = current.get("webhook_url", "")
        if payload.get("webhook_secret") is None:
            payload["webhook_secret"] = current.get("webhook_secret", "")
        payload["mode"] = str(payload.get("mode") or "pricing_ops").strip() or "pricing_ops"
        payload["notification_keyword"] = (
            str(payload.get("notification_keyword") or "定价Agent").strip() or "定价Agent"
        )
        payload["sheet_source_type"] = str(payload.get("sheet_source_type") or "file").strip() or "file"
        payload["sheet_source_path"] = str(payload.get("sheet_source_path") or "").strip()
        payload["poll_interval_seconds"] = max(10, min(3600, int(payload.get("poll_interval_seconds") or 60)))
        self._write_json(self.config_path, payload)
        return self.read_config(redacted=True)

    def read_state(self) -> Dict[str, Any]:
        self.ensure_dirs()
        state = self._read_json(self.state_path, {})
        state["poller_thread_alive"] = bool(self._poller_thread and self._poller_thread.is_alive())
        state["agent_dir"] = str(self.agent_dir)
        return state

    def send_notification(self, content: str) -> Dict[str, Any]:
        cfg = self.read_config(redacted=False)
        if not bool(cfg.get("enabled", True)):
            return {"ok": False, "skipped": True, "reason": "agent disabled"}
        webhook_url = str(cfg.get("webhook_url") or "").strip()
        if not webhook_url:
            raise HTTPException(status_code=400, detail="agent webhook_url is empty")

        keyword = str(cfg.get("notification_keyword") or "").strip()
        msg = str(content or "").strip()
        if keyword and keyword not in msg:
            msg = f"【{keyword}】{msg}"

        payload = {
            "msgtype": "text",
            "text": {"content": msg},
        }
        url = self._signed_webhook_url(webhook_url, str(cfg.get("webhook_secret") or "").strip())
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = int(resp.status)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"webhook send failed: {type(e).__name__}: {e}") from e

        parsed: Any
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {"raw": body}
        return {"ok": 200 <= status < 300, "status": status, "response": parsed, "content": msg}

    def test_notification(self, req: AgentNotifyTestReq) -> Dict[str, Any]:
        return self.send_notification(req.message)

    def create_pricing_task(
        self,
        req: AgentPricingTaskReq,
        compute_rows: ComputeRows,
    ) -> Dict[str, Any]:
        _ = req
        _ = compute_rows
        raise HTTPException(status_code=403, detail="agent pricing task is disabled in sheet-probe phase")

    def _create_pricing_task_disabled_archive(
        self,
        req: AgentPricingTaskReq,
        compute_rows: ComputeRows,
    ) -> Dict[str, Any]:
        self.ensure_dirs()
        cfg = self.read_config(redacted=False)
        pns = self._clean_pns(req.pns)
        if not pns:
            raise HTTPException(status_code=400, detail="pns is empty")
        apply_black_markup = (
            bool(cfg.get("apply_black_markup", True))
            if req.apply_black_markup is None
            else bool(req.apply_black_markup)
        )
        task_id = uuid.uuid4().hex[:16]
        task_dir = self.tasks_dir / task_id
        out_dir = task_dir / "outputs"
        state = {
            "task_id": task_id,
            "status": "queued",
            "source": str(req.source or "manual"),
            "created_at": utc_now_iso(),
            "started_at": None,
            "finished_at": None,
            "pns": pns,
            "apply_black_markup": apply_black_markup,
            "notify": bool(req.notify),
            "output_files": [],
            "report": None,
            "error": None,
        }
        self._write_task_state(task_id, state)
        worker = threading.Thread(
            target=self._run_pricing_task,
            args=(task_id, out_dir, compute_rows),
            daemon=True,
            name=f"agent-pricing-task-{task_id}",
        )
        worker.start()
        self._update_state({"last_task_id": task_id, "last_error": None})
        return {"task_id": task_id, "status": "queued", "count": len(pns)}

    def read_task(self, task_id: str) -> Dict[str, Any]:
        path = self._task_state_path(task_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="agent task not found")
        return self._read_json(path, {})

    def read_parsed_sheet_push(self, push_id: str = "latest") -> Dict[str, Any]:
        self.ensure_dirs()
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(push_id or "latest")) or "latest"
        path = self.sheet_parsed_dir / ("latest.json" if safe_id == "latest" else f"{safe_id}.json")
        if not path.exists():
            raise HTTPException(status_code=404, detail="parsed sheet push not found")
        return self._read_json(path, {})

    def gsp_status_queue(self, req: AgentGspQueueReq) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        parsed = self.read_parsed_sheet_push("latest")
        tasks = parsed.get("gsp_check_tasks") or []
        sheet_filter = self._safe_text(req.sheet)
        if sheet_filter:
            tasks = [t for t in tasks if self._safe_text(t.get("sheet")) == sheet_filter]

        queued: list[Dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for task in tasks:
            for pla_no in task.get("pla_numbers") or []:
                pla = self._safe_text(pla_no)
                key = (self._safe_text(task.get("sheet")), str(task.get("row_index") or ""), pla)
                if not pla or key in seen:
                    continue
                seen.add(key)
                queued.append(
                    {
                        "queue_id": hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:16],
                        "pla_no": pla,
                        "sheet": task.get("sheet"),
                        "row_index": task.get("row_index"),
                        "requester": task.get("requester"),
                        "pn": task.get("pn"),
                        "price_level": task.get("price_level"),
                        "description": task.get("description"),
                        "owner": task.get("owner"),
                        "sheet_status": task.get("status"),
                        "stage": task.get("stage"),
                        "task": task,
                    }
                )
                if len(queued) >= int(req.limit):
                    break
            if len(queued) >= int(req.limit):
                break

        return {
            "ok": True,
            "generated_at": utc_now_iso(),
            "source_push_id": parsed.get("push_id"),
            "summary": parsed.get("summary") or {},
            "count": len(queued),
            "tasks": queued,
        }

    def save_gsp_status_result(self, req: AgentGspStatusResultReq) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        self.ensure_dirs()
        payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
        payload["pla_no"] = self._safe_text(payload.get("pla_no"))
        payload["status"] = self._safe_text(payload.get("status"))
        payload["received_at"] = utc_now_iso()
        if not payload["checked_at"]:
            payload["checked_at"] = payload["received_at"]
        if not payload["pla_no"]:
            raise HTTPException(status_code=400, detail="pla_no is empty")

        result_id_src = f"{payload['pla_no']}|{payload.get('sheet') or ''}|{payload.get('row_index') or ''}|{payload['received_at']}"
        result_id = hashlib.sha1(result_id_src.encode("utf-8")).hexdigest()[:16]
        payload["result_id"] = result_id

        result_file = self.gsp_status_dir / f"{result_id}.json"
        latest_file = self.gsp_status_dir / "latest.json"
        self._write_json(result_file, payload)
        self._write_json(latest_file, payload)
        jsonl_file = self.gsp_status_dir / "results.jsonl"
        with jsonl_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

        self._update_state(
            {
                "last_gsp_status_result": {
                    "result_id": result_id,
                    "pla_no": payload["pla_no"],
                    "status": payload["status"],
                    "ok": bool(payload.get("ok")),
                    "received_at": payload["received_at"],
                    "path": str(result_file),
                },
                "last_error": None,
            }
        )
        return {"ok": True, "result_id": result_id, "path": str(result_file)}

    def _check_desktop_agent_token(self, token: Optional[str]) -> None:
        expected_token = self._sheet_push_token()
        if not expected_token:
            raise HTTPException(status_code=503, detail="agent token is not configured")
        if str(token or "") != expected_token:
            raise HTTPException(status_code=403, detail="invalid desktop agent token")

    def task_output_file(self, task_id: str) -> Path:
        state = self.read_task(task_id)
        if state.get("status") != "done":
            raise HTTPException(status_code=409, detail=f"task not done, status={state.get('status')}")
        files = [Path(x) for x in (state.get("output_files") or [])]
        for p in files:
            if p.exists():
                return p
        raise HTTPException(status_code=404, detail="task output file missing")

    def probe_sheet_source(self, req: Optional[AgentSheetProbeReq] = None) -> Dict[str, Any]:
        self.ensure_dirs()
        cfg = self.read_config(redacted=False)
        self._update_state({"last_poll_at": utc_now_iso()})
        source_path = str((req.source_path if req and req.source_path is not None else cfg.get("sheet_source_path")) or "").strip()
        if not source_path:
            return {"ok": True, "changed": False, "reason": "sheet_source_path is empty"}

        try:
            path, fetch_meta = self._materialize_sheet_source(source_path)
        except Exception as e:
            msg = f"sheet source fetch failed: {type(e).__name__}: {e}"
            self._update_state({"last_error": msg})
            raise HTTPException(status_code=400, detail=msg) from e

        digest = self._file_sha256(path)
        state = self.read_state()
        preview = self._preview_table_file(path)
        payload = {
            "ok": True,
            "changed": state.get("last_source_hash") != digest,
            "source": source_path,
            "source_hash": digest,
            "cached_path": str(path),
            "fetched_at": utc_now_iso(),
            "fetch": fetch_meta,
            "preview": preview,
        }
        self._update_state(
            {
                "last_source_hash": digest,
                "last_error": None,
                "last_sheet_probe": payload,
            }
        )
        return payload

    def receive_sheet_push(self, req: AgentSheetPushReq) -> Dict[str, Any]:
        expected_token = self._sheet_push_token()
        if expected_token and str(req.token or "") != expected_token:
            raise HTTPException(status_code=403, detail="invalid sheet push token")

        received_at = utc_now_iso()
        push_id = uuid.uuid4().hex[:16]
        payload = {
            "push_id": push_id,
            "received_at": received_at,
            "source": str(req.source or "alidocs-script"),
            "payload": req.payload,
        }
        out_file = self.sheet_push_dir / f"{push_id}.json"
        latest_file = self.sheet_push_dir / "latest.json"
        self._write_json(out_file, payload)
        self._write_json(latest_file, payload)

        summary = self._summarize_push_payload(req.payload)
        parsed = self.parse_sheet_payload(req.payload, push_id=push_id, received_at=received_at)
        parsed_file = self.sheet_parsed_dir / f"{push_id}.json"
        parsed_latest_file = self.sheet_parsed_dir / "latest.json"
        self._write_json(parsed_file, parsed)
        self._write_json(parsed_latest_file, parsed)

        notification_result = self._notify_sheet_push(parsed, push_id)
        state_payload = {
            "push_id": push_id,
            "received_at": received_at,
            "source": payload["source"],
            "summary": summary,
            "path": str(out_file),
            "parsed_path": str(parsed_file),
            "parsed_summary": parsed.get("summary"),
            "notification": notification_result,
        }
        self._update_state({"last_sheet_push": state_payload, "last_error": None})
        return {"ok": True, **state_payload}

    def _sheet_push_token(self) -> str:
        env_token = str(os.getenv("DAHUA_AGENT_SHEET_PUSH_TOKEN") or "").strip()
        if env_token:
            return env_token
        token_file = self.agent_dir / "sheet_push_token.txt"
        try:
            return token_file.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def parse_sheet_payload(self, payload: Any, *, push_id: str, received_at: str) -> Dict[str, Any]:
        sheets = self._extract_pushed_sheets(payload)
        parsed_sheets: list[Dict[str, Any]] = []
        all_tasks: list[Dict[str, Any]] = []
        for sheet in sheets:
            parsed = self._parse_sheet_rows(str(sheet.get("name") or ""), sheet.get("rows") or [])
            parsed_sheets.append(parsed)
            all_tasks.extend(parsed.get("tasks") or [])

        pending_tasks = [t for t in all_tasks if self._is_pending_task(t)]
        gsp_check_tasks = [t for t in pending_tasks if t.get("pla_numbers")]
        completed_tasks = [t for t in all_tasks if self._is_completed_task(t)]
        blocked_tasks = [t for t in all_tasks if self._is_blocked_task(t)]
        summary = {
            "push_id": push_id,
            "received_at": received_at,
            "file": self._safe_text(payload.get("file")) if isinstance(payload, dict) else None,
            "sheet_count": len(parsed_sheets),
            "task_count": len(all_tasks),
            "pending_count": len(pending_tasks),
            "gsp_check_count": len(gsp_check_tasks),
            "completed_count": len(completed_tasks),
            "blocked_count": len(blocked_tasks),
            "sheets": [
                {
                    "name": s.get("name"),
                    "header_row_index": s.get("header_row_index"),
                    "row_count": s.get("row_count"),
                    "task_count": len(s.get("tasks") or []),
                }
                for s in parsed_sheets
            ],
        }
        return {
            "push_id": push_id,
            "received_at": received_at,
            "summary": summary,
            "tasks": all_tasks,
            "pending_tasks": pending_tasks,
            "gsp_check_tasks": gsp_check_tasks,
            "blocked_tasks": blocked_tasks,
            "sheets": parsed_sheets,
        }

    def _notify_sheet_push(self, parsed: Dict[str, Any], push_id: str) -> Dict[str, Any]:
        cfg = self.read_config(redacted=False)
        if bool(cfg.get("dry_run")) or not bool(cfg.get("notify_on_task_done", True)):
            return {"ok": False, "skipped": True, "reason": "notification disabled"}
        if not str(cfg.get("webhook_url") or "").strip():
            return {"ok": False, "skipped": True, "reason": "agent webhook_url is empty"}
        summary = parsed.get("summary") or {}
        pending = parsed.get("gsp_check_tasks") or parsed.get("pending_tasks") or []
        examples = []
        for t in pending[:5]:
            pla = ", ".join(t.get("pla_numbers") or []) or t.get("pla_no") or "-"
            pn = t.get("pn") or "-"
            requester = t.get("requester") or "-"
            desc = t.get("description") or "-"
            level = t.get("price_level") or "-"
            examples.append(f"- {t.get('sheet')}#{t.get('row_index')}: {pla} / {requester} / {pn} / {level} / {desc}")
        detail = "\n".join(examples) if examples else "- 暂无待处理样例"
        message = (
            f"表格同步完成：push={push_id}\n"
            f"Sheet {summary.get('sheet_count', 0)} 个，结构化任务 {summary.get('task_count', 0)} 条，"
            f"待处理 {summary.get('pending_count', 0)} 条，可查 GSP PLA {summary.get('gsp_check_count', 0)} 条，"
            f"已完成 {summary.get('completed_count', 0)} 条，"
            f"异常/阻塞 {summary.get('blocked_count', 0)} 条。\n"
            f"GSP 待查样例：\n{detail}"
        )
        try:
            return self.send_notification(message)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def run_poll_once(self, compute_rows: Optional[ComputeRows] = None) -> Dict[str, Any]:
        _ = compute_rows
        return self.probe_sheet_source()

    def _legacy_run_poll_once_with_pricing_disabled(self, compute_rows: ComputeRows) -> Dict[str, Any]:
        self.ensure_dirs()
        cfg = self.read_config(redacted=False)
        self._update_state({"last_poll_at": utc_now_iso()})
        source_path = str(cfg.get("sheet_source_path") or "").strip()
        if not source_path:
            return {"ok": True, "changed": False, "reason": "sheet_source_path is empty"}
        path = Path(source_path)
        if not path.exists() or not path.is_file():
            msg = f"sheet_source_path not found: {source_path}"
            self._update_state({"last_error": msg})
            raise HTTPException(status_code=400, detail=msg)
        digest = self._file_sha256(path)
        state = self.read_state()
        if state.get("last_source_hash") == digest:
            return {"ok": True, "changed": False, "source_hash": digest}
        pns = self._clean_pns(parse_pn_list_file(path))
        self._update_state({"last_source_hash": digest, "last_error": None})
        if not pns:
            return {"ok": True, "changed": True, "source_hash": digest, "count": 0}
        task = self.create_pricing_task(
            AgentPricingTaskReq(
                pns=pns,
                source=f"poller:{path}",
                notify=bool(cfg.get("notify_on_task_done", True)),
                apply_black_markup=bool(cfg.get("apply_black_markup", True)),
            ),
            compute_rows,
        )
        return {"ok": True, "changed": True, "source_hash": digest, "count": len(pns), "task": task}

    def start_poller(self, compute_rows: ComputeRows) -> None:
        self.ensure_dirs()
        if self._poller_thread and self._poller_thread.is_alive():
            return
        self._poller_stop.clear()
        self._poller_thread = threading.Thread(
            target=self._poller_loop,
            args=(compute_rows,),
            daemon=True,
            name="agent-file-poller",
        )
        self._poller_thread.start()

    def _poller_loop(self, compute_rows: ComputeRows) -> None:
        while not self._poller_stop.is_set():
            cfg = self.read_config(redacted=False)
            interval = int(cfg.get("poll_interval_seconds") or 60)
            if bool(cfg.get("enabled", True)) and bool(cfg.get("poller_enabled", False)):
                try:
                    self.probe_sheet_source()
                except Exception as e:
                    self._update_state({"last_error": f"{type(e).__name__}: {e}"})
            self._poller_stop.wait(max(10, min(3600, interval)))

    def _run_pricing_task(self, task_id: str, out_dir: Path, compute_rows: ComputeRows) -> None:
        state = self.read_task(task_id)
        try:
            state["status"] = "running"
            state["started_at"] = utc_now_iso()
            self._write_task_state(task_id, state)

            result = compute_rows(state["pns"], bool(state.get("apply_black_markup")))
            rows = list(result.get("rows") or [])
            frames = build_export_frames(rows)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = write_export_xlsx(frames, out_dir=out_dir, level="country")

            report = dict(result.get("report") or {})
            report.setdefault("count_total", len(rows))
            report.setdefault("count_not_found", len(report.get("not_found") or []))
            report["output_file"] = str(out_file)

            state["status"] = "done"
            state["finished_at"] = utc_now_iso()
            state["output_files"] = [str(out_file)]
            state["report"] = report
            state["error"] = None
            self._write_task_state(task_id, state)

            cfg = self.read_config(redacted=False)
            if bool(state.get("notify")) and bool(cfg.get("notify_on_task_done", True)):
                self.send_notification(
                    "自动定价任务完成："
                    f"{task_id}，共 {report.get('count_total', 0)} 条，"
                    f"未命中 {report.get('count_not_found', 0)} 条。"
                )
        except Exception as e:
            state["status"] = "failed"
            state["finished_at"] = utc_now_iso()
            state["error"] = f"{type(e).__name__}: {e}"
            self._write_task_state(task_id, state)
            cfg = self.read_config(redacted=False)
            if bool(state.get("notify")) and bool(cfg.get("notify_on_task_failed", True)):
                try:
                    self.send_notification(f"自动定价任务失败：{task_id}，{state['error']}")
                except Exception:
                    pass

    def _update_state(self, patch: Dict[str, Any]) -> None:
        with self._lock:
            state = self._read_json(self.state_path, {})
            state.update(patch)
            state["updated_at"] = utc_now_iso()
            self._write_json(self.state_path, state)

    def _signed_webhook_url(self, webhook_url: str, secret: str) -> str:
        if not secret:
            return webhook_url
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        sign = urllib.parse.quote_plus(
            base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest())
        )
        sep = "&" if "?" in webhook_url else "?"
        return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"

    def _redact_url(self, url: str) -> str:
        if not url:
            return ""
        parsed = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        redacted_qs = []
        for k, v in qs:
            if k.lower() in {"access_token", "token", "sign"} and v:
                redacted_qs.append((k, f"{v[:6]}...{v[-4:]}"))
            else:
                redacted_qs.append((k, v))
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(redacted_qs), parsed.fragment)
        )

    def _clean_pns(self, values: List[Any]) -> List[str]:
        out: List[str] = []
        seen = set()
        for v in values:
            s = str(v or "").strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _file_sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _materialize_sheet_source(self, source_path: str) -> tuple[Path, Dict[str, Any]]:
        if source_path.lower().startswith(("http://", "https://")):
            req = urllib.request.Request(source_path, headers={"User-Agent": "DahuaPricingAgent/0.1"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                suffix = self._suffix_from_content_type_or_url(content_type, source_path)
                target = self.sheet_cache_dir / f"latest{suffix}"
                target.write_bytes(data)
                return target, {
                    "mode": "http",
                    "status": int(resp.status),
                    "content_type": content_type,
                    "bytes": len(data),
                }

        path = Path(source_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(source_path)
        return path, {
            "mode": "file",
            "bytes": path.stat().st_size,
            "mtime_epoch": path.stat().st_mtime,
        }

    def _suffix_from_content_type_or_url(self, content_type: str, url: str) -> str:
        path_suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        if path_suffix in {".xlsx", ".xls", ".csv", ".txt"}:
            return path_suffix
        ctype = content_type.lower()
        if "spreadsheetml" in ctype or "excel" in ctype:
            return ".xlsx"
        if "csv" in ctype:
            return ".csv"
        if "text" in ctype:
            return ".txt"
        return ".bin"

    def _preview_table_file(self, path: Path) -> Dict[str, Any]:
        suffix = path.suffix.lower()
        try:
            if suffix in {".xlsx", ".xls"}:
                xls = pd.ExcelFile(path)
                sheets = []
                for sheet in xls.sheet_names[:8]:
                    df = pd.read_excel(xls, sheet_name=sheet, nrows=8)
                    sheets.append(self._df_preview(sheet, df))
                return {
                    "kind": "excel",
                    "sheet_count": len(xls.sheet_names),
                    "sheet_names": xls.sheet_names,
                    "sheets": sheets,
                }
            if suffix == ".csv":
                df = pd.read_csv(path, nrows=8)
                return {"kind": "csv", "sheets": [self._df_preview("csv", df)]}
            if suffix == ".txt":
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                return {"kind": "text", "line_count_preview": len(lines[:20]), "lines": lines[:20]}
            return {"kind": "binary", "bytes": path.stat().st_size}
        except Exception as e:
            return {
                "kind": "unreadable_table",
                "error": f"{type(e).__name__}: {e}",
                "bytes": path.stat().st_size if path.exists() else None,
            }

    def _df_preview(self, name: str, df: pd.DataFrame) -> Dict[str, Any]:
        clean = df.where(pd.notnull(df), None)
        return {
            "name": name,
            "rows_previewed": int(clean.shape[0]),
            "cols": int(clean.shape[1]),
            "columns": [str(c) for c in clean.columns[:20]],
            "rows": clean.head(5).to_dict(orient="records"),
        }

    def _summarize_push_payload(self, payload: Any) -> Dict[str, Any]:
        if isinstance(payload, dict):
            rows = payload.get("rows")
            sheets = payload.get("sheets")
            return {
                "type": "object",
                "keys": sorted([str(k) for k in payload.keys()])[:30],
                "row_count": len(rows) if isinstance(rows, list) else None,
                "sheet_count": len(sheets) if isinstance(sheets, list) else None,
            }
        if isinstance(payload, list):
            return {"type": "array", "row_count": len(payload)}
        return {"type": type(payload).__name__}

    def _extract_pushed_sheets(self, payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, dict):
            sheets = payload.get("sheets")
            if isinstance(sheets, list):
                out = []
                for i, item in enumerate(sheets):
                    if not isinstance(item, dict):
                        continue
                    rows = item.get("rows")
                    if not isinstance(rows, list):
                        continue
                    out.append(
                        {
                            "name": self._safe_text(item.get("name") or item.get("sheet") or f"sheet_{i + 1}"),
                            "rows": rows,
                        }
                    )
                return out
            rows = payload.get("rows")
            if isinstance(rows, list):
                return [{"name": self._safe_text(payload.get("sheet") or "active_sheet"), "rows": rows}]
        if isinstance(payload, list):
            return [{"name": "array_payload", "rows": payload}]
        return []

    def _parse_sheet_rows(self, sheet_name: str, rows: list[Any]) -> Dict[str, Any]:
        normalized_rows = [self._normalize_row(row) for row in rows if isinstance(row, list)]
        header_idx = self._find_header_row_index(normalized_rows)
        if header_idx is None:
            return {
                "name": sheet_name,
                "row_count": len(normalized_rows),
                "header_row_index": None,
                "headers": [],
                "tasks": [],
                "warnings": ["header row not found"],
            }

        raw_headers = normalized_rows[header_idx]
        mapped_headers = [self._map_header(cell) for cell in raw_headers]
        tasks: list[Dict[str, Any]] = []
        for idx in range(header_idx + 1, len(normalized_rows)):
            row = normalized_rows[idx]
            if self._row_is_empty(row):
                continue
            task = self._task_from_row(sheet_name, idx + 1, row, mapped_headers, raw_headers)
            if not task:
                continue
            tasks.append(task)

        return {
            "name": sheet_name,
            "row_count": len(normalized_rows),
            "header_row_index": header_idx + 1,
            "headers": raw_headers,
            "mapped_headers": mapped_headers,
            "tasks": tasks,
            "warnings": [],
        }

    def _normalize_row(self, row: list[Any]) -> list[Any]:
        return [self._json_scalar(v) for v in row]

    def _json_scalar(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            if isinstance(value, str):
                return value.replace("_x000a_", "\n").strip()
            return value
        return str(value).strip()

    def _find_header_row_index(self, rows: list[list[Any]]) -> Optional[int]:
        best_idx = None
        best_score = 0
        for i, row in enumerate(rows[:80]):
            mapped = [self._map_header(cell) for cell in row]
            score = sum(1 for x in mapped if x)
            if "pn" in mapped:
                score += 3
            if "description" in mapped:
                score += 2
            if "price_level" in mapped:
                score += 2
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx if best_score >= 4 else None

    def _task_from_row(
        self,
        sheet_name: str,
        row_index: int,
        row: list[Any],
        mapped_headers: list[Optional[str]],
        raw_headers: list[Any],
    ) -> Optional[Dict[str, Any]]:
        values: Dict[str, Any] = {}
        raw: Dict[str, Any] = {}
        for col_idx, value in enumerate(row):
            key = mapped_headers[col_idx] if col_idx < len(mapped_headers) else None
            raw_header = self._safe_text(raw_headers[col_idx]) if col_idx < len(raw_headers) else f"COL{col_idx + 1}"
            if raw_header:
                raw[raw_header] = value
            if not key:
                continue
            clean_value = self._safe_text(value) if not isinstance(value, bool) else value
            if clean_value in ("", None):
                continue
            values[key] = clean_value

        meaningful = [
            values.get("requester"),
            values.get("description"),
            values.get("internal_model"),
            values.get("pn"),
            values.get("price_level"),
            values.get("pla_no"),
            values.get("status"),
        ]
        if not any(self._safe_text(x) for x in meaningful):
            return None

        task = {
            "sheet": sheet_name,
            "row_index": row_index,
            "done_checked": bool(values.get("done_checked")) if isinstance(values.get("done_checked"), bool) else None,
            "requester": self._safe_text(values.get("requester")),
            "description": self._safe_text(values.get("description")),
            "product_line": self._safe_text(values.get("product_line")),
            "internal_model": self._safe_text(values.get("internal_model")),
            "pn": self._safe_text(values.get("pn")),
            "price_level": self._safe_text(values.get("price_level")),
            "customer_name": self._safe_text(values.get("customer_name")),
            "deadline": self._safe_text(values.get("deadline")),
            "pla_no": self._safe_text(values.get("pla_no")),
            "pla_numbers": self._extract_pla_numbers(values.get("pla_no")),
            "owner": self._safe_text(values.get("owner")),
            "status": self._safe_text(values.get("status")),
            "stage": self._safe_text(values.get("stage")),
            "note": self._safe_text(values.get("note")),
            "normalized_status": self._normalize_task_status(values),
            "raw": raw,
        }
        return task

    def _map_header(self, value: Any) -> Optional[str]:
        text = self._norm_header_text(value)
        if not text:
            return None
        rules = [
            ("done_checked", ("checkbox", "完成勾选", "done")),
            ("requester", ("demandeur", "请求发起人", "相关人员", "personnes concernees", "personnes concernées")),
            ("description", ("description de la demande", "请求任务描述", "需求描述", "任务描述")),
            ("product_line", ("product line", "产品线")),
            ("internal_model", ("reference interne", "référence interne", "内部型号", "internal model")),
            ("pn", ("part number", "pn码", "pn", "part no")),
            ("price_level", ("niveau d application du prix", "niveau d’application du prix", "价格应用层级", "应用层级")),
            ("customer_name", ("customer name", "客户名称")),
            ("deadline", ("date limite", "截止日期")),
            ("pla_no", ("pla no", "pla")),
            ("owner", ("负责执行人", "执行人", "owner", "assignee")),
            ("note", ("当前操作状态细节", "问题备注", "状态细节", "备注", "note", "detail")),
            ("status", ("状态", "status")),
            ("stage", ("阶段", "stage")),
        ]
        for key, needles in rules:
            if any(n in text for n in needles):
                return key
        return None

    def _norm_header_text(self, value: Any) -> str:
        s = self._safe_text(value).lower()
        if not s:
            return ""
        s = s.replace("_x000a_", " ")
        s = re.sub(r"[\r\n\t]+", " ", s)
        s = re.sub(r"[()（）_:/\\-]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _safe_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _row_is_empty(self, row: list[Any]) -> bool:
        return not any(self._safe_text(x) for x in row)

    def _normalize_task_status(self, values: Dict[str, Any]) -> str:
        text = " ".join(
            [
                self._safe_text(values.get("status")),
                self._safe_text(values.get("stage")),
                self._safe_text(values.get("note")),
            ]
        ).lower()
        if any(x in text for x in ("已完成", "完成", "结束", "done", "closed", "terminé", "termine")):
            return "completed"
        if any(x in text for x in ("阻塞", "异常", "无法", "缺", "待确认", "待决策", "blocked", "error")):
            return "blocked"
        if any(x in text for x in ("处理中", "进行中", "已接收", "running", "progress")):
            return "running"
        return "pending"

    def _is_completed_task(self, task: Dict[str, Any]) -> bool:
        return task.get("normalized_status") == "completed"

    def _is_blocked_task(self, task: Dict[str, Any]) -> bool:
        return task.get("normalized_status") == "blocked"

    def _is_pending_task(self, task: Dict[str, Any]) -> bool:
        # Business trigger: J column (PLA NO.) has a value and L column (状态)
        # is not completed. Do not trigger pricing/GSP actions here; this only
        # structures rows for downstream status checks and group notification.
        if not self._safe_text(task.get("pla_no")):
            return False
        return not self._is_completed_task(task)

    def _extract_pla_numbers(self, value: Any) -> list[str]:
        text = self._safe_text(value)
        if not text:
            return []
        seen = set()
        out: list[str] = []
        for m in re.finditer(r"\bPLA\d{8,}\b", text.upper()):
            pla = m.group(0)
            if pla in seen:
                continue
            seen.add(pla)
            out.append(pla)
        return out
