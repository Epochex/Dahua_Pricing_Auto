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
    run_id: Optional[str] = Field(default=None)
    queue_id: Optional[str] = Field(default=None)
    session_id: Optional[str] = Field(default=None)
    context_id: Optional[str] = Field(default=None)
    skill_call_id: Optional[str] = Field(default=None)
    dispatch_id: Optional[str] = Field(default=None)
    pla_no: str = Field(default="")
    status: str = Field(default="")
    ok: bool = Field(default=True)
    sheet: Optional[str] = Field(default=None)
    row_index: Optional[int] = Field(default=None)
    pn: Optional[str] = Field(default=None)
    requester: Optional[str] = Field(default=None)
    sheet_status: Optional[str] = Field(default=None)
    approval_current_step: Optional[str] = Field(default=None)
    approval_taskers: Optional[str] = Field(default=None)
    checked_at: Optional[str] = Field(default=None)
    source: str = Field(default="windows-desktop-agent")
    detail: Optional[str] = Field(default=None)
    error: Optional[str] = Field(default=None)
    raw: Any = Field(default=None)


class AgentSheetStatusUpdatesReq(BaseModel):
    token: Optional[str] = Field(default=None)
    limit: int = Field(default=100, ge=1, le=500)


class AgentSheetStatusUpdateAckReq(BaseModel):
    token: Optional[str] = Field(default=None)
    update_ids: List[str] = Field(default_factory=list)
    ok: bool = Field(default=True)
    error: Optional[str] = Field(default=None)
    applied_by: str = Field(default="alidocs-script")


class AgentReplayEvalReq(BaseModel):
    token: Optional[str] = Field(default=None)
    push_id: str = Field(default="latest")
    limit: int = Field(default=500, ge=1, le=5000)


class AgentToolBackendHeartbeatReq(BaseModel):
    token: Optional[str] = Field(default=None)
    backend_id: str = Field(default="")
    display_name: str = Field(default="")
    capabilities: List[str] = Field(default_factory=list)
    status: str = Field(default="online")
    load: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


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
        self.sheet_update_dir = self.agent_dir / "sheet_updates"
        self.trace_dir = self.agent_dir / "traces"
        self.event_dir = self.agent_dir / "events"
        self.session_dir = self.agent_dir / "sessions"
        self.context_dir = self.agent_dir / "contexts"
        self.skill_call_dir = self.agent_dir / "skill_calls"
        self.dispatch_dir = self.agent_dir / "dispatches"
        self.observation_dir = self.agent_dir / "observations"
        self.memory_update_dir = self.agent_dir / "memory_updates"
        self.memory_dir = self.agent_dir / "memory"
        self.skill_dir = self.agent_dir / "skills"
        self.tool_backend_dir = self.agent_dir / "tool_backends"
        self.reflection_dir = self.agent_dir / "reflections"
        self.eval_dir = self.agent_dir / "evals"
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
        self.sheet_update_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.event_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.skill_call_dir.mkdir(parents=True, exist_ok=True)
        self.dispatch_dir.mkdir(parents=True, exist_ok=True)
        self.observation_dir.mkdir(parents=True, exist_ok=True)
        self.memory_update_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / "pla").mkdir(parents=True, exist_ok=True)
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        self.tool_backend_dir.mkdir(parents=True, exist_ok=True)
        self.reflection_dir.mkdir(parents=True, exist_ok=True)
        self.eval_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_builtin_skills()
        self._ensure_builtin_tool_backends()
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

    def _list_json_records(self, directory: Path, *, limit: int = 50) -> list[Dict[str, Any]]:
        self.ensure_dirs()
        out: list[Dict[str, Any]] = []
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name.startswith("."):
                continue
            record = self._read_json(path, {})
            if isinstance(record, dict):
                out.append(record)
            if len(out) >= int(limit):
                break
        return out

    def _record_entity(
        self,
        directory: Path,
        *,
        id_field: str,
        prefix: str,
        payload: Dict[str, Any],
        id_parts: Optional[list[Any]] = None,
    ) -> Dict[str, Any]:
        self.ensure_dirs()
        now = utc_now_iso()
        record = dict(payload)
        entity_id = self._safe_text(record.get(id_field))
        if not entity_id:
            entity_id = self._make_id(prefix, *(id_parts or [now, uuid.uuid4().hex]))
        record[id_field] = entity_id
        record.setdefault("created_at", now)
        record["updated_at"] = now
        self._write_json(directory / f"{self._safe_id(entity_id)}.json", record)
        return record

    def _read_entity(self, directory: Path, entity_id: str, *, label: str) -> Dict[str, Any]:
        self.ensure_dirs()
        path = directory / f"{self._safe_id(entity_id)}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{label} not found")
        return self._read_json(path, {})

    def _update_trace_metadata(self, run_id: Optional[str], patch: Dict[str, Any]) -> None:
        if not run_id:
            return
        path = self._trace_path(run_id)
        trace = self._read_json(path, {})
        if not isinstance(trace, dict) or not trace:
            return
        meta = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
        meta.update(patch)
        trace["metadata"] = meta
        trace["updated_at"] = utc_now_iso()
        self._write_json(path, trace)

    def _record_business_event(
        self,
        *,
        source: str,
        event_type: str,
        source_ref: Optional[str] = None,
        intent: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
        payload: Any = None,
        event_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        safe_source = self._safe_text(source) or "unknown"
        safe_type = self._safe_text(event_type) or "business_event"
        payload_preview = payload
        if isinstance(payload_preview, str):
            payload_preview = payload_preview[:2000]
        return self._record_entity(
            self.event_dir,
            id_field="event_id",
            prefix="evt",
            id_parts=[safe_source, safe_type, source_ref, json.dumps(summary or {}, sort_keys=True, default=str)],
            payload={
                "event_id": event_id,
                "source": safe_source,
                "event_type": safe_type,
                "source_ref": self._safe_text(source_ref),
                "intent": self._safe_text(intent),
                "summary": summary or {},
                "payload_preview": payload_preview,
                "received_at": utc_now_iso(),
            },
        )

    def _start_agent_session(
        self,
        *,
        event: Dict[str, Any],
        session_type: str,
        intent: str,
        run_id: Optional[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        event_id = self._safe_text(event.get("event_id"))
        safe_type = self._safe_text(session_type) or "agent_session"
        session = self._record_entity(
            self.session_dir,
            id_field="session_id",
            prefix="ses",
            id_parts=[event_id, safe_type, intent, run_id, utc_now_iso()],
            payload={
                "session_type": safe_type,
                "intent": self._safe_text(intent),
                "status": "running",
                "event_id": event_id,
                "run_ids": [run_id] if run_id else [],
                "metadata": metadata or {},
                "started_at": utc_now_iso(),
                "ended_at": None,
            },
        )
        self._update_trace_metadata(run_id, {"event_id": event_id, "session_id": session.get("session_id")})
        return session

    def _finish_agent_session(
        self,
        session_id: Optional[str],
        *,
        status: str,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        sid = self._safe_text(session_id)
        if not sid:
            return
        path = self.session_dir / f"{self._safe_id(sid)}.json"
        session = self._read_json(path, {})
        if not isinstance(session, dict) or not session:
            return
        session["status"] = self._safe_text(status) or "completed"
        session["ended_at"] = utc_now_iso()
        session["updated_at"] = session["ended_at"]
        if summary:
            session["summary"] = summary
        self._write_json(path, session)

    def _patch_agent_session(
        self,
        session_id: Optional[str],
        *,
        status: Optional[str] = None,
        patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        sid = self._safe_text(session_id)
        if not sid:
            return
        path = self.session_dir / f"{self._safe_id(sid)}.json"
        session = self._read_json(path, {})
        if not isinstance(session, dict) or not session:
            return
        if status:
            session["status"] = self._safe_text(status)
        if patch:
            meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
            meta.update(patch)
            session["metadata"] = meta
        session["updated_at"] = utc_now_iso()
        self._write_json(path, session)

    def _record_context_package(
        self,
        *,
        run_id: str,
        session_id: Optional[str],
        event_id: Optional[str],
        intent: str,
        trigger_summary: Dict[str, Any],
        target_scope: Dict[str, Any],
        candidate_tasks: list[Dict[str, Any]],
        skill: Dict[str, Any],
        tool_backends: list[Dict[str, Any]],
        policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._record_entity(
            self.context_dir,
            id_field="context_id",
            prefix="ctx",
            id_parts=[run_id, intent, json.dumps(target_scope, sort_keys=True, default=str)],
            payload={
                "run_id": run_id,
                "session_id": self._safe_text(session_id),
                "event_id": self._safe_text(event_id),
                "intent": self._safe_text(intent),
                "trigger_summary": trigger_summary,
                "target_scope": target_scope,
                "candidate_tasks": candidate_tasks,
                "skill": skill,
                "tool_backends": tool_backends,
                "policy": policy,
            },
        )

    def _record_skill_call(
        self,
        *,
        run_id: str,
        session_id: Optional[str],
        context_id: Optional[str],
        skill_name: str,
        skill: Dict[str, Any],
        status: str = "planned",
    ) -> Dict[str, Any]:
        full_skill = self._read_json(self.skill_dir / f"{self._safe_id(skill_name)}.json", {})
        versions = full_skill.get("versions") if isinstance(full_skill.get("versions"), list) else []
        active = int(full_skill.get("active_version") or skill.get("active_version") or 1)
        version = next((v for v in versions if int(v.get("version") or 0) == active), {})
        return self._record_entity(
            self.skill_call_dir,
            id_field="skill_call_id",
            prefix="skillcall",
            id_parts=[run_id, skill_name, active],
            payload={
                "run_id": run_id,
                "session_id": self._safe_text(session_id),
                "context_id": self._safe_text(context_id),
                "skill": skill,
                "skill_name": self._safe_text(skill_name),
                "skill_version": active,
                "status": self._safe_text(status) or "planned",
                "risk_level": self._safe_text(version.get("risk_level")),
                "allowed_tools": version.get("allowed_tools") or [],
                "preconditions": version.get("preconditions") or [],
                "postconditions": version.get("postconditions") or [],
                "eval_criteria": version.get("eval_criteria") or [],
                "plan_graph": version.get("plan_graph") or [],
            },
        )

    def _record_dispatch_decision(
        self,
        *,
        run_id: str,
        session_id: Optional[str],
        context_id: Optional[str],
        skill_call_id: Optional[str],
        capability: str,
        selected_backend: Dict[str, Any],
    ) -> Dict[str, Any]:
        candidates = selected_backend.get("candidates") if isinstance(selected_backend.get("candidates"), list) else []
        selected_id = self._safe_text(selected_backend.get("backend_id"))
        reason = selected_backend.get("reason")
        if not reason:
            reason = "selected by capability and freshness-adjusted load" if selected_id else "no eligible backend"
        return self._record_entity(
            self.dispatch_dir,
            id_field="dispatch_id",
            prefix="dispatch",
            id_parts=[run_id, capability, selected_id, json.dumps(candidates, sort_keys=True, default=str)],
            payload={
                "run_id": run_id,
                "session_id": self._safe_text(session_id),
                "context_id": self._safe_text(context_id),
                "skill_call_id": self._safe_text(skill_call_id),
                "capability": self._safe_text(capability),
                "selected_backend": {k: v for k, v in selected_backend.items() if k != "candidates"},
                "candidate_backends": candidates,
                "reason": reason,
            },
        )

    def _record_observation(
        self,
        *,
        run_id: Optional[str],
        observation_type: str,
        source: str,
        subject: Dict[str, Any],
        ok: bool,
        raw_status: Optional[str] = None,
        normalized_status: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._record_entity(
            self.observation_dir,
            id_field="observation_id",
            prefix="obs",
            id_parts=[run_id, observation_type, source, json.dumps(subject, sort_keys=True, default=str), raw_status, error],
            payload={
                "run_id": self._safe_text(run_id),
                "observation_type": self._safe_text(observation_type),
                "source": self._safe_text(source),
                "subject": subject,
                "ok": bool(ok),
                "raw_status": self._safe_text(raw_status),
                "normalized_status": self._safe_text(normalized_status),
                "evidence": evidence or {},
                "error": self._safe_text(error),
                "observed_at": utc_now_iso(),
            },
        )

    def _record_memory_update(
        self,
        *,
        run_id: Optional[str],
        memory_type: str,
        subject: Dict[str, Any],
        event_type: str,
        before: Optional[Dict[str, Any]],
        after: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._record_entity(
            self.memory_update_dir,
            id_field="memory_update_id",
            prefix="memupd",
            id_parts=[run_id, memory_type, json.dumps(subject, sort_keys=True, default=str), event_type, utc_now_iso()],
            payload={
                "run_id": self._safe_text(run_id),
                "memory_type": self._safe_text(memory_type),
                "subject": subject,
                "event_type": self._safe_text(event_type),
                "before": before or {},
                "after": after,
            },
        )

    def _make_id(self, prefix: str, *parts: Any) -> str:
        raw = "|".join(self._safe_text(x) for x in parts if self._safe_text(x))
        if not raw:
            raw = uuid.uuid4().hex
        return f"{prefix}_{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"

    def _safe_id(self, value: Any) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "", self._safe_text(value))[:80]

    def _ratio(self, numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return round(float(numerator) / float(denominator), 4)

    def _trace_path(self, run_id: str) -> Path:
        return self.trace_dir / f"{self._safe_id(run_id)}.json"

    def _start_trace(
        self,
        *,
        source: str,
        intent: str,
        metadata: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> str:
        self.ensure_dirs()
        now = utc_now_iso()
        rid = self._safe_id(run_id) if run_id else self._make_id("run", source, intent, now, uuid.uuid4().hex)
        path = self._trace_path(rid)
        if not path.exists():
            self._write_json(
                path,
                {
                    "run_id": rid,
                    "source": source,
                    "intent": intent,
                    "status": "running",
                    "started_at": now,
                    "updated_at": now,
                    "ended_at": None,
                    "metadata": metadata or {},
                    "spans": [],
                    "scores": {},
                    "reflection_ids": [],
                },
            )
        return rid

    def _add_trace_span(
        self,
        run_id: Optional[str],
        name: str,
        *,
        status: str = "ok",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not run_id:
            return
        path = self._trace_path(run_id)
        trace = self._read_json(path, {})
        if not isinstance(trace, dict) or not trace:
            return
        span = {
            "span_id": self._make_id("span", run_id, name, utc_now_iso(), len(trace.get("spans") or [])),
            "name": name,
            "status": status,
            "at": utc_now_iso(),
            "metadata": metadata or {},
        }
        spans = trace.get("spans")
        if not isinstance(spans, list):
            spans = []
        spans.append(span)
        trace["spans"] = spans
        trace["updated_at"] = span["at"]
        if status in {"failed", "manual_review"}:
            trace["status"] = status
        self._write_json(path, trace)

    def _finish_trace(
        self,
        run_id: Optional[str],
        *,
        status: str = "completed",
        scores: Optional[Dict[str, Any]] = None,
        reflection_id: Optional[str] = None,
    ) -> None:
        if not run_id:
            return
        path = self._trace_path(run_id)
        trace = self._read_json(path, {})
        if not isinstance(trace, dict) or not trace:
            return
        now = utc_now_iso()
        trace["status"] = status
        trace["updated_at"] = now
        trace["ended_at"] = now
        if scores:
            old_scores = trace.get("scores") if isinstance(trace.get("scores"), dict) else {}
            old_scores.update(scores)
            trace["scores"] = old_scores
        if reflection_id:
            ids = trace.get("reflection_ids") if isinstance(trace.get("reflection_ids"), list) else []
            if reflection_id not in ids:
                ids.append(reflection_id)
            trace["reflection_ids"] = ids
        self._write_json(path, trace)

    def _ensure_builtin_skills(self) -> None:
        builtin = {
            "check_gsp_approval_status": {
                "name": "check_gsp_approval_status",
                "active_version": 1,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "versions": [
                    {
                        "version": 1,
                        "status": "active",
                        "created_at": utc_now_iso(),
                        "risk_level": "read_only",
                        "purpose": "Check GSP PLA approval state without modifying GSP.",
                        "allowed_tools": ["gsp.status_api", "gsp.browser_status_checker"],
                        "preconditions": ["PLA number exists", "row status is not completed"],
                        "postconditions": ["status result is persisted", "approved result may queue sheet write-back"],
                        "eval_criteria": ["GSP query succeeds", "status is grounded in tool output", "no GSP mutation occurs"],
                        "plan_graph": [
                            {
                                "step": "context_assembly",
                                "description": "Load the row context, PLA memory, current sheet status, and available GSP tool backends.",
                                "requires": ["PLA number exists", "latest parsed sheet snapshot exists"],
                            },
                            {
                                "step": "tool_backend_selection",
                                "description": "Choose an online backend with gsp.status_check capability and lowest declared load.",
                                "requires": ["backend heartbeat is fresh or fallback backend exists"],
                            },
                            {
                                "step": "gsp_query",
                                "description": "Read GSP approval status by API-first strategy with browser fallback delegated to the Windows agent.",
                                "requires": ["valid GSP login state or recoverable browser session"],
                            },
                            {
                                "step": "observation_normalization",
                                "description": "Normalize raw GSP status into approved / pending / failed evidence without mutating GSP.",
                                "requires": ["tool output is present"],
                            },
                            {
                                "step": "decision_and_memory_update",
                                "description": "Persist result, update PLA timeline memory, and queue Alidocs write-back only for approved status.",
                                "requires": ["row identity still matches the PLA"],
                            },
                        ],
                    }
                ],
            },
            "apply_alidocs_status_update": {
                "name": "apply_alidocs_status_update",
                "active_version": 1,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "versions": [
                    {
                        "version": 1,
                        "status": "active",
                        "created_at": utc_now_iso(),
                        "risk_level": "low_write",
                        "purpose": "Apply backend-approved L-column status updates from inside Alidocs.",
                        "allowed_tools": ["alidocs.status_writer"],
                        "preconditions": ["pending update exists", "target PLA still matches row context"],
                        "postconditions": ["write-back ACK is stored"],
                        "eval_criteria": ["cell updated", "ACK received", "no duplicate completion write"],
                        "plan_graph": [
                            {
                                "step": "fetch_pending_updates",
                                "description": "Fetch pending write-back records from the backend with a bounded limit.",
                                "requires": ["valid Alidocs script token"],
                            },
                            {
                                "step": "sheet_resolution",
                                "description": "Resolve the sheet by name and locate the L-column target cell.",
                                "requires": ["sheet exists in current workbook"],
                            },
                            {
                                "step": "writeback",
                                "description": "Write the approved completion status into the target cell.",
                                "requires": ["cell is writable by the current Alidocs session"],
                            },
                            {
                                "step": "ack_and_trace",
                                "description": "ACK success or failure back to the backend and append the trace span.",
                                "requires": ["backend is reachable"],
                            },
                        ],
                    }
                ],
            },
            "official_release_product_in_gsp": {
                "name": "official_release_product_in_gsp",
                "active_version": 1,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "versions": [
                    {
                        "version": 1,
                        "status": "draft",
                        "created_at": utc_now_iso(),
                        "risk_level": "high_write",
                        "purpose": "Officially release product records in GSP product manager flows.",
                        "allowed_tools": ["gsp.product_release_browser", "human.approval_gate"],
                        "preconditions": ["human approval required", "dry-run verification required"],
                        "postconditions": ["release state verified in GSP", "sheet audit note written"],
                        "eval_criteria": ["no unintended product mutation", "post-condition verified", "audit trail complete"],
                        "plan_graph": [
                            {
                                "step": "context_assembly",
                                "description": "Collect PN, product line, requester, current sale state, and related sheet row.",
                                "requires": ["PN exists", "request row is not already completed"],
                            },
                            {
                                "step": "dry_run_locate_product",
                                "description": "Open Product Management, locate exact PN, and capture current release/sale state without changes.",
                                "requires": ["Windows GSP browser backend is online"],
                            },
                            {
                                "step": "risk_gate",
                                "description": "Require explicit human approval before any GSP mutation.",
                                "requires": ["dry-run evidence is attached", "approver identity is recorded"],
                            },
                            {
                                "step": "official_release",
                                "description": "Perform official release only for the verified product row.",
                                "requires": ["human approval gate passed"],
                            },
                            {
                                "step": "postcondition_check",
                                "description": "Re-read GSP state, write audit evidence, and update Memory only after verification.",
                                "requires": ["release state can be re-read from GSP"],
                            },
                        ],
                    }
                ],
            },
        }
        for name, payload in builtin.items():
            path = self.skill_dir / f"{name}.json"
            if not path.exists():
                self._write_json(path, payload)
                continue
            current = self._read_json(path, {})
            if not isinstance(current, dict) or not current:
                self._write_json(path, payload)
                continue
            changed = False
            for key in ("name", "active_version", "created_at"):
                if key not in current and key in payload:
                    current[key] = payload[key]
                    changed = True
            current["updated_at"] = current.get("updated_at") or payload.get("updated_at") or utc_now_iso()
            versions = current.get("versions") if isinstance(current.get("versions"), list) else []
            default_versions = payload.get("versions") if isinstance(payload.get("versions"), list) else []
            for default_version in default_versions:
                version_no = int(default_version.get("version") or 0)
                existing = next((v for v in versions if int(v.get("version") or 0) == version_no), None)
                if existing is None:
                    versions.append(default_version)
                    changed = True
                    continue
                for key, value in default_version.items():
                    if key not in existing:
                        existing[key] = value
                        changed = True
            current["versions"] = versions
            if changed:
                current["updated_at"] = utc_now_iso()
                self._write_json(path, current)

    def _ensure_builtin_tool_backends(self) -> None:
        builtin = {
            "backend-local": {
                "backend_id": "backend-local",
                "display_name": "Linux Hermes Core",
                "status": "online",
                "capabilities": [
                    "sheet.push_parse",
                    "memory.pla_timeline",
                    "trace.store",
                    "reflection.candidate",
                    "eval.replay",
                ],
                "load": 0.0,
                "metadata": {
                    "kind": "core",
                    "selection_policy": "always available for backend-native capabilities",
                },
            },
            "alidocs-script": {
                "backend_id": "alidocs-script",
                "display_name": "Alidocs Sheet Script",
                "status": "external",
                "capabilities": ["alidocs.status_writeback"],
                "load": 0.0,
                "metadata": {
                    "kind": "script",
                    "selection_policy": "triggered from Alidocs schedule/manual run",
                },
            },
        }
        now = utc_now_iso()
        for backend_id, payload in builtin.items():
            path = self.tool_backend_dir / f"{backend_id}.json"
            if path.exists():
                current = self._read_json(path, {})
                if isinstance(current, dict) and current:
                    changed = False
                    for key, value in payload.items():
                        if key not in current:
                            current[key] = value
                            changed = True
                    if changed:
                        current["updated_at"] = now
                        self._write_json(path, current)
                    continue
            record = {
                **payload,
                "created_at": now,
                "updated_at": now,
                "last_heartbeat_at": now,
            }
            self._write_json(path, record)

    def _skill_version(self, name: str) -> Dict[str, Any]:
        self.ensure_dirs()
        path = self.skill_dir / f"{self._safe_id(name)}.json"
        skill = self._read_json(path, {})
        versions = skill.get("versions") if isinstance(skill.get("versions"), list) else []
        active = int(skill.get("active_version") or 1)
        chosen = next((v for v in versions if int(v.get("version") or 0) == active), None)
        return {
            "name": self._safe_text(skill.get("name")) or name,
            "active_version": active,
            "risk_level": self._safe_text((chosen or {}).get("risk_level")),
            "status": self._safe_text((chosen or {}).get("status")),
        }

    def _pla_timeline_path(self, pla_no: str) -> Path:
        safe = self._safe_id(self._safe_text(pla_no).upper())
        return self.memory_dir / "pla" / f"{safe}.json"

    def _update_pla_timeline(
        self,
        pla_no: str,
        event_type: str,
        *,
        event: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        pla = self._safe_text(pla_no).upper()
        if not pla:
            return {}
        path = self._pla_timeline_path(pla)
        now = utc_now_iso()
        timeline = self._read_json(path, {})
        if not timeline:
            timeline = {
                "pla_no": pla,
                "first_seen": now,
                "last_seen": now,
                "sightings": [],
                "status_events": [],
                "writebacks": [],
                "reflections": [],
                "runs": [],
            }
        before_compact = dict(timeline.get("compact_memory") or {})
        timeline["last_seen"] = now
        if run_id:
            runs = timeline.get("runs") if isinstance(timeline.get("runs"), list) else []
            if run_id not in runs:
                runs.append(run_id)
            timeline["runs"] = runs[-100:]
        record = {"event_type": event_type, "at": now, "run_id": run_id, **(event or {})}
        bucket = "status_events"
        if event_type in {"sheet_sighting", "queue_selected"}:
            bucket = "sightings"
        elif event_type in {"sheet_writeback_queued", "sheet_writeback_ack"}:
            bucket = "writebacks"
        elif event_type == "reflection":
            bucket = "reflections"
        values = timeline.get(bucket) if isinstance(timeline.get(bucket), list) else []
        values.append(record)
        timeline[bucket] = values[-200:]
        timeline["compact_memory"] = self._compact_pla_timeline(timeline)
        self._write_json(path, timeline)
        self._record_memory_update(
            run_id=run_id,
            memory_type="pla_timeline",
            subject={"pla_no": pla},
            event_type=event_type,
            before=before_compact,
            after=timeline.get("compact_memory") or {},
        )
        return timeline

    def _compact_pla_timeline(self, timeline: Dict[str, Any]) -> Dict[str, Any]:
        sightings = timeline.get("sightings") if isinstance(timeline.get("sightings"), list) else []
        status_events = timeline.get("status_events") if isinstance(timeline.get("status_events"), list) else []
        writebacks = timeline.get("writebacks") if isinstance(timeline.get("writebacks"), list) else []
        reflections = timeline.get("reflections") if isinstance(timeline.get("reflections"), list) else []
        runs = timeline.get("runs") if isinstance(timeline.get("runs"), list) else []

        latest_sighting = sightings[-1] if sightings else {}
        latest_status = status_events[-1] if status_events else {}
        latest_writeback = writebacks[-1] if writebacks else {}

        unresolved_writebacks = [
            w for w in writebacks if self._safe_text(w.get("state")) in {"pending", "failed"} or not w.get("state")
        ]
        approved_count = sum(1 for e in status_events if self._is_approved_gsp_status(e.get("status")))
        failed_count = sum(1 for e in status_events if not bool(e.get("ok", True)))

        return {
            "pla_no": self._safe_text(timeline.get("pla_no")),
            "first_seen": timeline.get("first_seen"),
            "last_seen": timeline.get("last_seen"),
            "latest_sheet": latest_sighting.get("sheet") or latest_status.get("sheet") or latest_writeback.get("sheet"),
            "latest_row_index": latest_sighting.get("row_index")
            or latest_status.get("row_index")
            or latest_writeback.get("row_index"),
            "latest_pn": latest_sighting.get("pn") or latest_status.get("pn"),
            "latest_requester": latest_sighting.get("requester") or latest_status.get("requester"),
            "latest_gsp_status": latest_status.get("status"),
            "latest_gsp_ok": latest_status.get("ok"),
            "writeback_state": latest_writeback.get("state") or ("pending" if unresolved_writebacks else ""),
            "latest_writeback_cell": latest_writeback.get("cell"),
            "run_count": len(runs),
            "sighting_count": len(sightings),
            "status_event_count": len(status_events),
            "approved_status_count": approved_count,
            "failed_status_count": failed_count,
            "writeback_count": len(writebacks),
            "reflection_count": len(reflections),
            "needs_attention": bool(failed_count or unresolved_writebacks or reflections),
        }

    def _select_tool_backend(self, capability: str) -> Dict[str, Any]:
        self.ensure_dirs()
        cap = self._safe_text(capability)
        candidates: list[Dict[str, Any]] = []
        now_ts = time.time()
        for path in self.tool_backend_dir.glob("*.json"):
            record = self._read_json(path, {})
            if not isinstance(record, dict) or not record:
                continue
            caps = [self._safe_text(x) for x in (record.get("capabilities") or [])]
            status = self._safe_text(record.get("status")).lower()
            if cap and cap not in caps:
                continue
            if status not in {"online", "external", "degraded"}:
                continue
            stale_penalty = 0.0
            heartbeat = self._safe_text(record.get("last_heartbeat_at"))
            if heartbeat:
                try:
                    heartbeat_ts = datetime.fromisoformat(heartbeat.replace("Z", "+00:00")).timestamp()
                    if now_ts - heartbeat_ts > 900:
                        stale_penalty = 0.5
                except Exception:
                    stale_penalty = 0.5
            else:
                stale_penalty = 0.5
            load = float(record.get("load") or 0.0)
            record["_selection_score"] = round(load + stale_penalty, 4)
            candidates.append(record)
        if not candidates:
            return {
                "backend_id": "",
                "status": "missing",
                "capability": cap,
                "reason": "no backend advertises requested capability",
                "candidates": [],
            }
        compact_candidates = [
            {
                "backend_id": self._safe_text(c.get("backend_id")),
                "display_name": self._safe_text(c.get("display_name")),
                "status": self._safe_text(c.get("status")),
                "load": c.get("load"),
                "last_heartbeat_at": c.get("last_heartbeat_at"),
                "selection_score": c.get("_selection_score"),
            }
            for c in candidates
        ]
        chosen = sorted(
            candidates,
            key=lambda r: (
                float(r.get("_selection_score") or 0.0),
                self._safe_text(r.get("backend_id")),
            ),
        )[0]
        return {
            "backend_id": self._safe_text(chosen.get("backend_id")),
            "display_name": self._safe_text(chosen.get("display_name")),
            "status": self._safe_text(chosen.get("status")),
            "capability": cap,
            "load": chosen.get("load"),
            "last_heartbeat_at": chosen.get("last_heartbeat_at"),
            "selection_score": chosen.get("_selection_score"),
            "reason": "selected by capability and freshness-adjusted load",
            "candidates": compact_candidates,
        }

    def _create_reflection_candidate(
        self,
        *,
        run_id: Optional[str],
        kind: str,
        skill: str,
        summary: str,
        evidence: Dict[str, Any],
        risk_level: str = "low",
        proposed_update: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.ensure_dirs()
        reflection_id = self._make_id("refl", run_id, kind, skill, json.dumps(evidence, sort_keys=True, default=str))
        payload = {
            "reflection_id": reflection_id,
            "state": "proposed",
            "kind": kind,
            "skill": skill,
            "risk_level": risk_level,
            "summary": summary,
            "evidence": evidence,
            "proposed_update": proposed_update or {},
            "created_at": utc_now_iso(),
            "run_id": run_id,
        }
        self._write_json(self.reflection_dir / f"{reflection_id}.json", payload)
        if evidence.get("pla_no"):
            self._update_pla_timeline(
                self._safe_text(evidence.get("pla_no")),
                "reflection",
                event={"reflection_id": reflection_id, "summary": summary, "kind": kind},
                run_id=run_id,
            )
        self._finish_trace(run_id, status="completed", reflection_id=reflection_id)
        return payload

    def _read_recent_gsp_results(self, *, limit: int = 500) -> list[Dict[str, Any]]:
        self.ensure_dirs()
        out: list[Dict[str, Any]] = []
        jsonl = self.gsp_status_dir / "results.jsonl"
        if jsonl.exists():
            try:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
                for line in reversed(lines[-limit:]):
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(item, dict):
                        out.append(item)
                    if len(out) >= limit:
                        break
                return out
            except Exception:
                pass
        return self._list_json_records(self.gsp_status_dir, limit=limit)

    def _reflect_on_gsp_result(
        self, payload: Dict[str, Any], sheet_update: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        pla_no = self._safe_text(payload.get("pla_no"))
        status = self._safe_text(payload.get("status"))
        run_id = self._safe_text(payload.get("run_id"))
        evidence = {
            "pla_no": pla_no,
            "status": status,
            "ok": bool(payload.get("ok")),
            "sheet": payload.get("sheet"),
            "row_index": payload.get("row_index"),
            "result_id": payload.get("result_id"),
            "sheet_update": sheet_update,
            "error": payload.get("error"),
        }
        if not bool(payload.get("ok")):
            return self._create_reflection_candidate(
                run_id=run_id,
                kind="executor_reliability",
                skill="check_gsp_approval_status",
                summary="GSP status query failed; review executor health, auth state, or fallback policy.",
                evidence=evidence,
                risk_level="medium",
                proposed_update={
                    "action": "consider_browser_fallback_or_manual_review",
                    "condition": "same error repeats for this executor or PLA",
                },
            )
        if self._is_approved_gsp_status(status) and not bool(sheet_update.get("queued")):
            return self._create_reflection_candidate(
                run_id=run_id,
                kind="writeback_policy_gap",
                skill="apply_alidocs_status_update",
                summary="GSP returned approved but no sheet write-back was queued.",
                evidence=evidence,
                risk_level="medium",
                proposed_update={
                    "action": "inspect_row_identity_or_completion_guard",
                    "condition": sheet_update.get("reason"),
                },
            )
        if self._safe_text(sheet_update.get("reason")) == "sheet row is already completed":
            return self._create_reflection_candidate(
                run_id=run_id,
                kind="duplicate_suppression",
                skill="check_gsp_approval_status",
                summary="Approved PLA was already completed in the sheet; duplicate write-back avoided.",
                evidence=evidence,
                risk_level="low",
                proposed_update={
                    "action": "keep_idempotency_memory",
                    "condition": "same PLA appears in future checks",
                },
            )
        return None

    def _task_state_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "state.json"

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

    def list_agent_traces(self, limit: int = 50) -> Dict[str, Any]:
        self.ensure_dirs()
        items = self._list_json_records(self.trace_dir, limit=limit)
        return {"ok": True, "count": len(items), "traces": items}

    def read_agent_trace(self, run_id: str) -> Dict[str, Any]:
        self.ensure_dirs()
        safe_id = self._safe_id(run_id)
        path = self.trace_dir / f"{safe_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="agent trace not found")
        return self._read_json(path, {})

    def list_business_events(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.event_dir, limit=limit)
        return {"ok": True, "count": len(items), "events": items}

    def read_business_event(self, event_id: str) -> Dict[str, Any]:
        return self._read_entity(self.event_dir, event_id, label="business event")

    def list_agent_sessions(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.session_dir, limit=limit)
        return {"ok": True, "count": len(items), "sessions": items}

    def read_agent_session(self, session_id: str) -> Dict[str, Any]:
        return self._read_entity(self.session_dir, session_id, label="agent session")

    def list_context_packages(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.context_dir, limit=limit)
        return {"ok": True, "count": len(items), "contexts": items}

    def read_context_package(self, context_id: str) -> Dict[str, Any]:
        return self._read_entity(self.context_dir, context_id, label="context package")

    def list_skill_calls(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.skill_call_dir, limit=limit)
        return {"ok": True, "count": len(items), "skill_calls": items}

    def list_dispatch_decisions(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.dispatch_dir, limit=limit)
        return {"ok": True, "count": len(items), "dispatches": items}

    def list_observations(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.observation_dir, limit=limit)
        return {"ok": True, "count": len(items), "observations": items}

    def list_memory_updates(self, limit: int = 50) -> Dict[str, Any]:
        items = self._list_json_records(self.memory_update_dir, limit=limit)
        return {"ok": True, "count": len(items), "memory_updates": items}

    def list_agent_reflections(self, limit: int = 50) -> Dict[str, Any]:
        self.ensure_dirs()
        items = self._list_json_records(self.reflection_dir, limit=limit)
        return {"ok": True, "count": len(items), "reflections": items}

    def list_agent_skills(self) -> Dict[str, Any]:
        self.ensure_dirs()
        items = self._list_json_records(self.skill_dir, limit=500)
        return {"ok": True, "count": len(items), "skills": items}

    def read_agent_skill(self, skill_name: str) -> Dict[str, Any]:
        self.ensure_dirs()
        path = self.skill_dir / f"{self._safe_id(skill_name)}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="agent skill not found")
        return self._read_json(path, {})

    def list_tool_backends(self) -> Dict[str, Any]:
        self.ensure_dirs()
        items = self._list_json_records(self.tool_backend_dir, limit=500)
        items.sort(key=lambda x: self._safe_text(x.get("backend_id")))
        return {"ok": True, "count": len(items), "tool_backends": items}

    def record_tool_backend_heartbeat(self, req: AgentToolBackendHeartbeatReq) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        self.ensure_dirs()
        backend_id = self._safe_id(req.backend_id) or self._make_id("backend", req.display_name, req.capabilities)
        capabilities = sorted({self._safe_text(x) for x in req.capabilities if self._safe_text(x)})
        if not capabilities:
            raise HTTPException(status_code=400, detail="capabilities is empty")
        now = utc_now_iso()
        path = self.tool_backend_dir / f"{backend_id}.json"
        old = self._read_json(path, {})
        created_at = old.get("created_at") if isinstance(old, dict) else None
        record = {
            "backend_id": backend_id,
            "display_name": self._safe_text(req.display_name) or backend_id,
            "status": self._safe_text(req.status).lower() or "online",
            "capabilities": capabilities,
            "load": float(req.load),
            "metadata": req.metadata if isinstance(req.metadata, dict) else {},
            "created_at": created_at or now,
            "updated_at": now,
            "last_heartbeat_at": now,
        }
        self._write_json(path, record)
        self._update_state({"last_tool_backend_heartbeat": record})
        return {"ok": True, "tool_backend": record}

    def read_pla_timeline(self, pla_no: str) -> Dict[str, Any]:
        self.ensure_dirs()
        pla = self._safe_text(pla_no).upper()
        path = self._pla_timeline_path(pla)
        if not path.exists():
            raise HTTPException(status_code=404, detail="PLA timeline not found")
        return self._read_json(path, {})

    def run_replay_eval(self, req: AgentReplayEvalReq) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        parsed = self.read_parsed_sheet_push(req.push_id or "latest")
        tasks = list(parsed.get("tasks") or [])[: int(req.limit)]
        gsp_tasks = list(parsed.get("gsp_check_tasks") or [])[: int(req.limit)]
        updates = self._read_sheet_updates()

        expected_queue: set[tuple[str, int, str]] = set()
        for task in tasks:
            if not self._is_pending_task(task):
                continue
            for pla in task.get("pla_numbers") or []:
                pla_no = self._safe_text(pla)
                if pla_no:
                    expected_queue.add((self._safe_text(task.get("sheet")), int(task.get("row_index") or 0), pla_no))

        actual_queue: set[tuple[str, int, str]] = set()
        for task in gsp_tasks:
            for pla in task.get("pla_numbers") or []:
                pla_no = self._safe_text(pla)
                if pla_no:
                    actual_queue.add((self._safe_text(task.get("sheet")), int(task.get("row_index") or 0), pla_no))

        approved_results = self._read_recent_gsp_results(limit=5000)
        approved_keys = {
            (
                self._safe_text(r.get("sheet")),
                int(r.get("row_index") or 0),
                self._safe_text(r.get("pla_no")),
            )
            for r in approved_results
            if bool(r.get("ok")) and self._is_approved_gsp_status(r.get("status"))
        }
        applied_or_pending = {
            (
                self._safe_text(u.get("sheet")),
                int(u.get("row_index") or 0),
                self._safe_text(u.get("pla_no")),
            )
            for u in updates
            if self._safe_text(u.get("state")) in {"pending", "applied"}
        }

        missing_queue = sorted(expected_queue - actual_queue)
        extra_queue = sorted(actual_queue - expected_queue)
        missing_writeback = sorted(approved_keys - applied_or_pending)
        false_writeback = sorted(applied_or_pending - approved_keys)
        scores = {
            "row_selection_precision": self._ratio(len(actual_queue - set(extra_queue)), max(1, len(actual_queue))),
            "row_selection_recall": self._ratio(len(expected_queue - set(missing_queue)), max(1, len(expected_queue))),
            "approved_writeback_recall": self._ratio(len(approved_keys - set(missing_writeback)), max(1, len(approved_keys))),
            "false_writeback_count": len(false_writeback),
        }
        eval_id = self._make_id("eval", parsed.get("push_id"), utc_now_iso())
        payload = {
            "eval_id": eval_id,
            "type": "replay_eval",
            "created_at": utc_now_iso(),
            "push_id": parsed.get("push_id"),
            "task_count": len(tasks),
            "expected_queue_count": len(expected_queue),
            "actual_queue_count": len(actual_queue),
            "approved_result_count": len(approved_keys),
            "writeback_count": len(applied_or_pending),
            "scores": scores,
            "failures": {
                "missing_queue": [list(x) for x in missing_queue[:100]],
                "extra_queue": [list(x) for x in extra_queue[:100]],
                "missing_writeback": [list(x) for x in missing_writeback[:100]],
                "false_writeback": [list(x) for x in false_writeback[:100]],
            },
        }
        self._write_json(self.eval_dir / f"{eval_id}.json", payload)
        self._update_state({"last_replay_eval": payload})
        return {"ok": True, **payload}

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

    def handle_dingtalk_event(
        self,
        payload: Dict[str, Any],
        *,
        headers: Optional[Dict[str, Any]] = None,
        source: str = "hermes",
    ) -> Dict[str, Any]:
        self.ensure_dirs()
        headers = headers or {}

        if not self._verify_inbound_event_secret(headers):
            raise HTTPException(status_code=401, detail="invalid inbound event token")

        text = self._extract_event_text(payload)
        command = self._parse_approval_command(text)
        event = self._record_business_event(
            source=source,
            event_type="incoming_message",
            source_ref=self._safe_text(payload.get("msgId") or payload.get("messageId") or payload.get("conversationId")),
            intent="check_gsp_approval_status" if command.get("matched") else "",
            summary={
                "matched": bool(command.get("matched")),
                "reason": command.get("reason"),
                "sheet": command.get("sheet"),
                "limit": command.get("limit"),
            },
            payload={"text": text[:2000], "raw_keys": sorted([str(k) for k in payload.keys()])},
        )
        if not command.get("matched"):
            return {
                "ok": True,
                "matched": False,
                "reason": command.get("reason") or "not a known command",
                "source": source,
                "event_id": event.get("event_id"),
                "raw_text": text,
            }

        token = self._sheet_push_token()
        if not token:
            return {
                "ok": False,
                "matched": False,
                "reason": "agent token is not configured",
                "source": source,
                "command": command,
            }

        queue = self.gsp_status_queue(
            AgentGspQueueReq(
                token=token,
                limit=int(command.get("limit") or 200),
                sheet=command.get("sheet"),
            ),
            trigger_event=event,
            session_type="chat_command",
        )

        event_record = {
            "source": source,
            "received_at": utc_now_iso(),
            "raw_text": text[:800],
            "command_match": command,
            "triggered_tasks": queue.get("count", 0),
            "sheet": command.get("sheet"),
            "limit": int(command.get("limit") or 200),
        }
        self._update_state({"last_inbound_event": event_record})

        return {
            "ok": True,
            "matched": True,
            "source": source,
            "event_id": event.get("event_id"),
            "command": command,
            "queue": queue,
            "event": event_record,
        }

    def _verify_inbound_event_secret(self, headers: Dict[str, Any]) -> bool:
        expected = str(os.getenv("DAHUA_AGENT_INBOUND_TOKEN") or "").strip()
        if not expected:
            return True

        values: list[str] = []
        for k, v in headers.items():
            if not isinstance(v, str):
                continue
            key = str(k).lower()
            if key in {
                "x-inbound-token",
                "x-hermes-token",
                "x-dingtalk-token",
                "x-event-token",
                "authorization",
                "x-api-key",
            }:
                values.append(v)
            if key in {"x-event-signature", "x-hermes-signature", "x-dingtalk-signature"}:
                values.append(v)

        if not values:
            return False

        for value in values:
            s = value.strip()
            if s == expected:
                return True
            if s.lower().startswith("bearer ") and s[7:].strip() == expected:
                return True
        return False

    def _extract_event_text(self, payload: Dict[str, Any]) -> str:
        def _coerce(val: Any) -> str:
            if val is None:
                return ""
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                nested = [
                    val.get("text"),
                    val.get("content"),
                    val.get("msg"),
                    val.get("message"),
                ]
                return " ".join(part for part in (_coerce(v) for v in nested) if part).strip()
            if isinstance(val, list):
                return " ".join(part for part in (_coerce(v) for v in val) if part).strip()
            return str(val)

        candidates = [
            payload.get("text"),
            payload.get("content"),
            payload.get("message"),
            payload.get("msg"),
            payload.get("textContent"),
            payload.get("event"),
            payload.get("raw"),
        ]
        text = " ".join(part for part in (_coerce(v) for v in candidates) if part).strip()

        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict):
                text = " ".join([text, _coerce(data)]).strip()
            event_content = payload.get("event")
            if isinstance(event_content, dict):
                text = " ".join([text, _coerce(event_content)]).strip()

        return text.strip()

    def _parse_approval_command(self, text: str) -> Dict[str, Any]:
        norm = self._safe_text(text)
        if not norm:
            return {"matched": False, "reason": "empty message"}

        lowered = norm.lower()
        has_action = any(
            k in norm
            for k in [
                "查审批",
                "查一下审批",
                "帮我查",
                "帮我查一下",
                "查询审批",
                "审批状态",
                "pla",
                "gsp",
                "审批",
            ]
        )
        if not has_action:
            return {"matched": False, "reason": "no command keyword"}

        sheet: Optional[str] = None
        m = re.search(r"\b(\d{4}\.\d{2})\b", norm)
        if m:
            sheet = m.group(1)

        limit = 200
        m = re.search(r"(?:前|查|处理|limit|只查)\s*(\d{1,3})\s*(?:条|个|项)?", lowered)
        if m:
            try:
                limit = min(max(1, int(m.group(1))), 500)
            except Exception:
                pass

        return {
            "matched": True,
            "text": norm,
            "sheet": sheet,
            "limit": limit,
            "keywords": [k for k in ["查审批", "查一下", "帮我查", "审批", "PLA", "GSP"] if k.lower() in lowered],
        }

    def create_pricing_task(
        self,
        req: AgentPricingTaskReq,
        compute_rows: ComputeRows,
    ) -> Dict[str, Any]:
        _ = req
        _ = compute_rows
        raise HTTPException(status_code=403, detail="agent pricing task is disabled in sheet-probe phase")

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

    def gsp_status_queue(
        self,
        req: AgentGspQueueReq,
        *,
        trigger_event: Optional[Dict[str, Any]] = None,
        session_type: str = "approval_check",
    ) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        parsed = self.read_parsed_sheet_push("latest")
        skill = self._skill_version("check_gsp_approval_status")
        event = trigger_event or self._record_business_event(
            source="backend",
            event_type="gsp_status_queue_request",
            source_ref=parsed.get("push_id"),
            intent="check_gsp_approval_status",
            summary={"sheet": req.sheet, "limit": int(req.limit), "push_id": parsed.get("push_id")},
            payload={},
        )
        selected_backend = self._select_tool_backend("gsp.status_check")
        run_id = self._start_trace(
            source="backend-gsp-queue",
            intent="check_gsp_approval_status",
            metadata={
                "event_id": event.get("event_id"),
                "push_id": parsed.get("push_id"),
                "sheet": req.sheet,
                "limit": int(req.limit),
                "skill": skill,
                "selected_tool_backend": selected_backend,
            },
        )
        session = self._start_agent_session(
            event=event,
            session_type=session_type,
            intent="check_gsp_approval_status",
            run_id=run_id,
            metadata={"push_id": parsed.get("push_id"), "sheet": req.sheet, "limit": int(req.limit)},
        )
        tasks = parsed.get("gsp_check_tasks") or []
        sheet_filter = self._safe_text(req.sheet)
        if sheet_filter:
            tasks = [t for t in tasks if self._safe_text(t.get("sheet")) == sheet_filter]
        task_backend = {k: v for k, v in selected_backend.items() if k != "candidates"}

        queued: list[Dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for task in tasks:
            for pla_no in task.get("pla_numbers") or []:
                pla = self._safe_text(pla_no)
                key = (self._safe_text(task.get("sheet")), str(task.get("row_index") or ""), pla)
                if not pla or key in seen:
                    continue
                seen.add(key)
                queue_id = hashlib.sha1("|".join(key).encode("utf-8")).hexdigest()[:16]
                queued.append(
                    {
                        "queue_id": queue_id,
                        "run_id": run_id,
                        "session_id": session.get("session_id"),
                        "skill": skill,
                        "selected_tool_backend": task_backend,
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
                self._update_pla_timeline(
                    pla,
                    "queue_selected",
                    event={
                        "queue_id": queue_id,
                        "session_id": session.get("session_id"),
                        "sheet": task.get("sheet"),
                        "row_index": task.get("row_index"),
                        "pn": task.get("pn"),
                        "requester": task.get("requester"),
                        "sheet_status": task.get("status"),
                        "source_push_id": parsed.get("push_id"),
                    },
                    run_id=run_id,
                )
                if len(queued) >= int(req.limit):
                    break
            if len(queued) >= int(req.limit):
                break

        context_tasks: list[Dict[str, Any]] = []
        for item in queued[: int(req.limit)]:
            timeline = self._read_json(self._pla_timeline_path(self._safe_text(item.get("pla_no"))), {})
            context_tasks.append(
                {
                    "queue_id": item.get("queue_id"),
                    "sheet": item.get("sheet"),
                    "row_index": item.get("row_index"),
                    "pla_no": item.get("pla_no"),
                    "pn": item.get("pn"),
                    "requester": item.get("requester"),
                    "sheet_status": item.get("sheet_status"),
                    "compact_memory": (timeline.get("compact_memory") if isinstance(timeline, dict) else {}) or {},
                }
            )
        context = self._record_context_package(
            run_id=run_id,
            session_id=session.get("session_id"),
            event_id=event.get("event_id"),
            intent="check_gsp_approval_status",
            trigger_summary=event.get("summary") or {},
            target_scope={"sheet": sheet_filter, "limit": int(req.limit), "source_push_id": parsed.get("push_id")},
            candidate_tasks=context_tasks,
            skill=skill,
            tool_backends=selected_backend.get("candidates") or [],
            policy={
                "allow_gsp_query": True,
                "allow_sheet_writeback": True,
                "allow_group_notify": not bool(self.read_config(redacted=False).get("dry_run")),
                "failure_to_reflection": True,
            },
        )
        skill_call = self._record_skill_call(
            run_id=run_id,
            session_id=session.get("session_id"),
            context_id=context.get("context_id"),
            skill_name="check_gsp_approval_status",
            skill=skill,
        )
        dispatch = self._record_dispatch_decision(
            run_id=run_id,
            session_id=session.get("session_id"),
            context_id=context.get("context_id"),
            skill_call_id=skill_call.get("skill_call_id"),
            capability="gsp.status_check",
            selected_backend=selected_backend,
        )
        for item in queued:
            item["context_id"] = context.get("context_id")
            item["skill_call_id"] = skill_call.get("skill_call_id")
            item["dispatch_id"] = dispatch.get("dispatch_id")
        self._patch_agent_session(
            session.get("session_id"),
            status="queued",
            patch={
                "context_id": context.get("context_id"),
                "skill_call_id": skill_call.get("skill_call_id"),
                "dispatch_id": dispatch.get("dispatch_id"),
                "queued_count": len(queued),
            },
        )

        self._add_trace_span(
            run_id,
            "task_filter",
            metadata={
                "event_id": event.get("event_id"),
                "session_id": session.get("session_id"),
                "context_id": context.get("context_id"),
                "skill_call_id": skill_call.get("skill_call_id"),
                "dispatch_id": dispatch.get("dispatch_id"),
                "candidate_count": len(tasks),
                "queued_count": len(queued),
                "sheet_filter": sheet_filter,
                "selected_tool_backend": selected_backend,
            },
        )
        self._update_trace_metadata(
            run_id,
            {
                "event_id": event.get("event_id"),
                "session_id": session.get("session_id"),
                "context_id": context.get("context_id"),
                "skill_call_id": skill_call.get("skill_call_id"),
                "dispatch_id": dispatch.get("dispatch_id"),
            },
        )
        self._finish_trace(run_id, status="queued", scores={"queued_count": len(queued)})
        return {
            "ok": True,
            "run_id": run_id,
            "event_id": event.get("event_id"),
            "session_id": session.get("session_id"),
            "context_id": context.get("context_id"),
            "skill_call_id": skill_call.get("skill_call_id"),
            "dispatch_id": dispatch.get("dispatch_id"),
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
        payload["run_id"] = self._safe_text(payload.get("run_id"))
        payload["queue_id"] = self._safe_text(payload.get("queue_id"))
        payload["session_id"] = self._safe_text(payload.get("session_id"))
        payload["context_id"] = self._safe_text(payload.get("context_id"))
        payload["skill_call_id"] = self._safe_text(payload.get("skill_call_id"))
        payload["dispatch_id"] = self._safe_text(payload.get("dispatch_id"))
        payload["received_at"] = utc_now_iso()
        if not payload["checked_at"]:
            payload["checked_at"] = payload["received_at"]
        if not payload["pla_no"]:
            raise HTTPException(status_code=400, detail="pla_no is empty")
        if not payload["run_id"]:
            payload["run_id"] = self._start_trace(
                source=payload.get("source") or "windows-desktop-agent",
                intent="check_gsp_approval_status",
                metadata={
                    "pla_no": payload["pla_no"],
                    "sheet": payload.get("sheet"),
                    "row_index": payload.get("row_index"),
                    "skill": self._skill_version("check_gsp_approval_status"),
                    "recovered_trace": True,
                },
            )
        trace_meta: Dict[str, Any] = {}
        if payload.get("run_id"):
            trace = self._read_json(self._trace_path(payload["run_id"]), {})
            trace_meta = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
        for key in ("session_id", "context_id", "skill_call_id", "dispatch_id"):
            if not payload.get(key):
                payload[key] = self._safe_text(trace_meta.get(key))

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

        observation = self._record_observation(
            run_id=payload.get("run_id"),
            observation_type="gsp_status_result",
            source=payload.get("source") or "windows-desktop-agent",
            subject={
                "pla_no": payload.get("pla_no"),
                "sheet": payload.get("sheet"),
                "row_index": payload.get("row_index"),
                "queue_id": payload.get("queue_id"),
            },
            ok=bool(payload.get("ok")),
            raw_status=payload.get("status"),
            normalized_status="approved"
            if self._is_approved_gsp_status(payload.get("status"))
            else self._safe_text(payload.get("status")),
            evidence={
                "result_id": result_id,
                "approval_current_step": payload.get("approval_current_step"),
                "approval_taskers": payload.get("approval_taskers"),
                "detail": payload.get("detail"),
            },
            error=payload.get("error"),
        )
        sheet_update = self._maybe_queue_sheet_status_update(payload)
        self._update_state(
            {
                "last_gsp_status_result": {
                    "result_id": result_id,
                    "observation_id": observation.get("observation_id"),
                    "pla_no": payload["pla_no"],
                    "status": payload["status"],
                    "ok": bool(payload.get("ok")),
                    "sheet_update": sheet_update,
                    "received_at": payload["received_at"],
                    "path": str(result_file),
                },
                "last_error": None,
            }
        )
        self._update_pla_timeline(
            payload["pla_no"],
            "gsp_status_result",
            event={
                "result_id": result_id,
                "observation_id": observation.get("observation_id"),
                "queue_id": payload.get("queue_id"),
                "status": payload["status"],
                "ok": bool(payload.get("ok")),
                "sheet": payload.get("sheet"),
                "row_index": payload.get("row_index"),
                "pn": payload.get("pn"),
                "requester": payload.get("requester"),
                "approval_current_step": payload.get("approval_current_step"),
                "approval_taskers": payload.get("approval_taskers"),
                "error": payload.get("error"),
            },
            run_id=payload.get("run_id"),
        )
        self._add_trace_span(
            payload.get("run_id"),
            "gsp_status_result",
            status="ok" if bool(payload.get("ok")) else "failed",
            metadata={
                "result_id": result_id,
                "observation_id": observation.get("observation_id"),
                "pla_no": payload["pla_no"],
                "status": payload["status"],
                "sheet_update": sheet_update,
            },
        )
        self._patch_agent_session(
            payload.get("session_id"),
            status="observed",
            patch={
                "last_observation_id": observation.get("observation_id"),
                "last_result_id": result_id,
                "last_result_status": payload.get("status"),
            },
        )
        reflection = self._reflect_on_gsp_result(payload, sheet_update)
        trace_status = "completed" if bool(payload.get("ok")) else "failed"
        self._finish_trace(
            payload.get("run_id"),
            status=trace_status,
            scores={
                "gsp_query_success": bool(payload.get("ok")),
                "approved_status": self._is_approved_gsp_status(payload.get("status")),
                "writeback_queued": bool(sheet_update.get("queued")),
            },
            reflection_id=(reflection or {}).get("reflection_id"),
        )
        return {
            "ok": True,
            "run_id": payload.get("run_id"),
            "session_id": payload.get("session_id"),
            "context_id": payload.get("context_id"),
            "skill_call_id": payload.get("skill_call_id"),
            "dispatch_id": payload.get("dispatch_id"),
            "observation_id": observation.get("observation_id"),
            "result_id": result_id,
            "path": str(result_file),
            "sheet_update": sheet_update,
            "reflection": reflection,
        }

    def sheet_status_updates(self, req: AgentSheetStatusUpdatesReq) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        updates = self._read_sheet_updates()
        pending = [u for u in updates if self._safe_text(u.get("state")) == "pending"]
        pending.sort(key=lambda u: self._safe_text(u.get("created_at")))
        return {
            "ok": True,
            "generated_at": utc_now_iso(),
            "count": min(len(pending), int(req.limit)),
            "updates": pending[: int(req.limit)],
        }

    def ack_sheet_status_updates(self, req: AgentSheetStatusUpdateAckReq) -> Dict[str, Any]:
        self._check_desktop_agent_token(req.token)
        ids = {self._safe_text(x) for x in req.update_ids if self._safe_text(x)}
        if not ids:
            raise HTTPException(status_code=400, detail="update_ids is empty")
        now = utc_now_iso()
        updates = self._read_sheet_updates()
        acked = 0
        with self._lock:
            for update in updates:
                if self._safe_text(update.get("update_id")) not in ids:
                    continue
                update["state"] = "applied" if bool(req.ok) else "failed"
                update["acked_at"] = now
                update["applied_by"] = self._safe_text(req.applied_by) or "alidocs-script"
                update["ack_error"] = self._safe_text(req.error)
                self._update_pla_timeline(
                    self._safe_text(update.get("pla_no")),
                    "sheet_writeback_ack",
                    event={
                        "update_id": update.get("update_id"),
                        "cell": update.get("cell"),
                        "sheet": update.get("sheet"),
                        "row_index": update.get("row_index"),
                        "state": update["state"],
                        "error": update["ack_error"],
                        "applied_by": update["applied_by"],
                    },
                    run_id=self._safe_text(update.get("source_run_id")),
                )
                self._add_trace_span(
                    self._safe_text(update.get("source_run_id")),
                    "sheet_writeback_ack",
                    status="ok" if bool(req.ok) else "failed",
                    metadata={
                        "update_id": update.get("update_id"),
                        "cell": update.get("cell"),
                        "state": update["state"],
                        "error": update["ack_error"],
                    },
                )
                run_id = self._safe_text(update.get("source_run_id"))
                observation = self._record_observation(
                    run_id=run_id,
                    observation_type="sheet_writeback_ack",
                    source=update["applied_by"],
                    subject={
                        "update_id": update.get("update_id"),
                        "pla_no": update.get("pla_no"),
                        "sheet": update.get("sheet"),
                        "cell": update.get("cell"),
                    },
                    ok=bool(req.ok),
                    raw_status=update["state"],
                    normalized_status=update["state"],
                    evidence={
                        "new_status": update.get("new_status"),
                        "acked_at": update.get("acked_at"),
                    },
                    error=update["ack_error"],
                )
                trace = self._read_json(self._trace_path(run_id), {}) if run_id else {}
                trace_meta = trace.get("metadata") if isinstance(trace.get("metadata"), dict) else {}
                self._patch_agent_session(
                    self._safe_text(trace_meta.get("session_id")),
                    status="writeback_observed",
                    patch={
                        "last_writeback_observation_id": observation.get("observation_id"),
                        "last_writeback_state": update["state"],
                    },
                )
                acked += 1
            self._write_sheet_updates(updates)
        return {"ok": True, "acked": acked, "state": "applied" if bool(req.ok) else "failed"}

    def _maybe_queue_sheet_status_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not bool(payload.get("ok")):
            return {"queued": False, "reason": "gsp query failed"}
        if not self._is_approved_gsp_status(payload.get("status")):
            return {"queued": False, "reason": "gsp status is not approved"}

        sheet = self._safe_text(payload.get("sheet"))
        row_index = payload.get("row_index")
        pla_no = self._safe_text(payload.get("pla_no"))
        if not sheet or not row_index or not pla_no:
            return {"queued": False, "reason": "sheet, row_index, or pla_no missing"}

        latest_task = self._find_latest_sheet_task(sheet, int(row_index), pla_no)
        old_status = self._safe_text(payload.get("sheet_status")) or self._safe_text(
            (latest_task or {}).get("status")
        )
        if self._is_completed_status_text(old_status):
            return {"queued": False, "reason": "sheet row is already completed"}

        dedupe_src = f"{sheet}|{row_index}|{pla_no}|L|已完成"
        update_id = hashlib.sha1(dedupe_src.encode("utf-8")).hexdigest()[:16]
        now = utc_now_iso()
        record = {
            "update_id": update_id,
            "state": "pending",
            "created_at": now,
            "sheet": sheet,
            "row_index": int(row_index),
            "col": "L",
            "cell": f"L{int(row_index)}",
            "pla_no": pla_no,
            "old_status": old_status,
            "new_status": "已完成",
            "gsp_status": self._safe_text(payload.get("status")),
            "approval_current_step": self._safe_text(payload.get("approval_current_step")),
            "approval_taskers": self._safe_text(payload.get("approval_taskers")),
            "source_result_id": self._safe_text(payload.get("result_id")),
            "source_run_id": self._safe_text(payload.get("run_id")),
        }
        if latest_task:
            record["pn"] = latest_task.get("pn")
            record["requester"] = latest_task.get("requester")

        with self._lock:
            updates = self._read_sheet_updates()
            for existing in updates:
                if self._safe_text(existing.get("update_id")) == update_id:
                    if self._safe_text(existing.get("state")) == "pending":
                        return {"queued": False, "reason": "update already pending", "update_id": update_id}
                    if self._safe_text(existing.get("state")) == "applied":
                        return {"queued": False, "reason": "update already applied", "update_id": update_id}
                    existing.clear()
                    existing.update(record)
                    self._write_sheet_updates(updates)
                    return {
                        "queued": True,
                        "reason": "failed update requeued",
                        "update_id": update_id,
                        "cell": record["cell"],
                        "new_status": record["new_status"],
                    }
            updates.append(record)
            self._write_sheet_updates(updates)
        self._update_pla_timeline(
            pla_no,
            "sheet_writeback_queued",
            event={
                "update_id": update_id,
                "cell": record["cell"],
                "sheet": sheet,
                "row_index": int(row_index),
                "old_status": old_status,
                "new_status": record["new_status"],
                "source_result_id": record["source_result_id"],
            },
            run_id=self._safe_text(payload.get("run_id")),
        )
        return {"queued": True, "update_id": update_id, "cell": record["cell"], "new_status": record["new_status"]}

    def _read_sheet_updates(self) -> list[Dict[str, Any]]:
        self.ensure_dirs()
        path = self.sheet_update_dir / "updates.json"
        data = self._read_json(path, [])
        return data if isinstance(data, list) else []

    def _write_sheet_updates(self, updates: list[Dict[str, Any]]) -> None:
        self.ensure_dirs()
        path = self.sheet_update_dir / "updates.json"
        self._write_json(path, updates)
        pending = [u for u in updates if self._safe_text(u.get("state")) == "pending"]
        self._write_json(self.sheet_update_dir / "pending.json", pending)

    def _find_latest_sheet_task(self, sheet: str, row_index: int, pla_no: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = self.read_parsed_sheet_push("latest")
        except Exception:
            return None
        for task in parsed.get("tasks") or []:
            if self._safe_text(task.get("sheet")) != sheet:
                continue
            if int(task.get("row_index") or 0) != int(row_index):
                continue
            pla_numbers = [self._safe_text(x) for x in (task.get("pla_numbers") or [])]
            if pla_no in pla_numbers or self._safe_text(task.get("pla_no")) == pla_no:
                return task
        return None

    def _is_approved_gsp_status(self, value: Any) -> bool:
        text = self._safe_text(value).strip().lower()
        compact = re.sub(r"[\s_\-]+", "", text)
        if compact in {"approved", "approve"}:
            return True
        return any(x in text for x in ("审批通过", "已批准", "已审批", "審批通過"))

    def _is_completed_status_text(self, value: Any) -> bool:
        text = self._safe_text(value).strip().lower()
        if not text:
            return False
        compact = re.sub(r"[\s_\-]+", "", text)
        return any(
            x in compact
            for x in (
                "已完成",
                "完成",
                "done",
                "closed",
                "complete",
                "completed",
                "finished",
                "termine",
                "terminé",
            )
        )

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
        event = self._record_business_event(
            source=payload["source"],
            event_type="sheet_push",
            source_ref=push_id,
            intent="sheet_push_ingest",
            summary=summary,
            payload={"push_id": push_id, "source": payload["source"]},
        )
        run_id = self._start_trace(
            source=str(req.source or "alidocs-script"),
            intent="sheet_push_ingest",
            metadata={"event_id": event.get("event_id"), "push_id": push_id, "summary": parsed.get("summary") or {}},
        )
        session = self._start_agent_session(
            event=event,
            session_type="sheet_sync",
            intent="sheet_push_ingest",
            run_id=run_id,
            metadata={"push_id": push_id, "source": payload["source"]},
        )
        observation = self._record_observation(
            run_id=run_id,
            observation_type="sheet_parse_result",
            source=payload["source"],
            subject={"push_id": push_id},
            ok=True,
            raw_status="parsed",
            normalized_status="parsed",
            evidence=parsed.get("summary") or {},
        )
        self._add_trace_span(
            run_id,
            "sheet_parse",
            metadata={
                "event_id": event.get("event_id"),
                "session_id": session.get("session_id"),
                "observation_id": observation.get("observation_id"),
                "sheet_count": (parsed.get("summary") or {}).get("sheet_count"),
                "task_count": (parsed.get("summary") or {}).get("task_count"),
                "gsp_check_count": (parsed.get("summary") or {}).get("gsp_check_count"),
            },
        )
        for task in (parsed.get("gsp_check_tasks") or [])[:500]:
            for pla in task.get("pla_numbers") or []:
                self._update_pla_timeline(
                    self._safe_text(pla),
                    "sheet_sighting",
                    event={
                        "push_id": push_id,
                        "sheet": task.get("sheet"),
                        "row_index": task.get("row_index"),
                        "pn": task.get("pn"),
                        "requester": task.get("requester"),
                        "status": task.get("status"),
                        "stage": task.get("stage"),
                    },
                    run_id=run_id,
                )
        self._finish_trace(
            run_id,
            status="completed",
            scores={
                "parsed_task_count": (parsed.get("summary") or {}).get("task_count", 0),
                "gsp_check_count": (parsed.get("summary") or {}).get("gsp_check_count", 0),
            },
        )
        self._finish_agent_session(
            session.get("session_id"),
            status="completed",
            summary={
                "push_id": push_id,
                "observation_id": observation.get("observation_id"),
                "parsed_summary": parsed.get("summary") or {},
            },
        )
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
            "run_id": run_id,
            "event_id": event.get("event_id"),
            "session_id": session.get("session_id"),
            "observation_id": observation.get("observation_id"),
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
        return task.get("normalized_status") == "completed" or self._is_completed_status_text(task.get("status"))

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
