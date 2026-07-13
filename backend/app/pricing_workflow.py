from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from pydantic import BaseModel, Field

try:  # Linux production lock; the threading lock remains useful in unit tests.
    import fcntl
except ImportError:  # pragma: no cover - backend is deployed on Linux
    fcntl = None  # type: ignore[assignment]


TERMINAL_STATES = {"approved", "rejected", "failed"}
ACTIVE_STATES = {"pricing", "validating", "submitting", "verifying", "under_approval"}
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES | {"manual_review"}

ACTION_FOR_STATE = {
    "submitting": "submit_gsp",
    "verifying": "verify_gsp_submission",
    "under_approval": "check_gsp_approval",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


class WorkflowError(RuntimeError):
    pass


class WorkflowNotFound(WorkflowError):
    pass


class WorkflowConflict(WorkflowError):
    pass


class WorkflowValidationError(WorkflowError):
    pass


class PricingWorkflowCreateReq(BaseModel):
    pns: List[str] = Field(default_factory=list)
    source: str = Field(default="manual")
    notify: bool = Field(default=True)
    apply_black_markup: Optional[bool] = Field(default=None)
    idempotency_key: Optional[str] = Field(default=None, max_length=200)
    max_submission_attempts: int = Field(default=2, ge=1, le=5)
    max_verification_attempts: int = Field(default=3, ge=1, le=10)
    gsp_payload: Dict[str, Any] = Field(default_factory=dict)
    submission_authorized: bool = Field(default=False)
    token: Optional[str] = Field(default=None)


class PricingWorkflowClaimReq(BaseModel):
    token: Optional[str] = Field(default=None)
    worker_id: str = Field(default="windows-gsp-agent", min_length=1, max_length=200)
    capabilities: List[str] = Field(
        default_factory=lambda: ["submit_gsp", "verify_gsp_submission", "check_gsp_approval"]
    )
    lease_seconds: int = Field(default=120, ge=30, le=900)


class PricingWorkflowReportReq(BaseModel):
    token: Optional[str] = Field(default=None)
    worker_id: str = Field(default="windows-gsp-agent", min_length=1, max_length=200)
    lease_id: str = Field(min_length=1, max_length=200)
    report_id: str = Field(min_length=1, max_length=200)
    outcome: str = Field(min_length=1, max_length=100)
    pla_no: Optional[str] = Field(default=None, max_length=200)
    detail: Optional[str] = Field(default=None, max_length=2000)
    evidence: Dict[str, Any] = Field(default_factory=dict)


class PricingWorkflowRetryReq(BaseModel):
    token: Optional[str] = Field(default=None)
    reason: str = Field(min_length=1, max_length=1000)
    target_state: Optional[str] = Field(default=None)
    expected_version: Optional[int] = Field(default=None, ge=1)


class PricingWorkflowStore:
    """Durable workflow store for pricing and GSP side effects.

    Every mutation holds an OS file lock and atomically replaces state.json.  The
    worker receives a lease for exactly one external action.  An expired submit
    lease is treated as an unknown side effect and is moved to ``verifying``;
    it is never made directly claimable as another submit.
    """

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.tasks_dir = self.runtime_dir / "agent" / "tasks"
        self.lock_path = self.runtime_dir / "agent" / ".pricing-workflow.lock"
        self._thread_lock = threading.RLock()

    def ensure_dirs(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.ensure_dirs()
        with self._thread_lock:
            with self.lock_path.open("a+b") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _state_path(self, task_id: str) -> Path:
        safe_id = str(task_id or "").strip()
        if not safe_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in safe_id):
            raise WorkflowValidationError("invalid task_id")
        return self.tasks_dir / safe_id / "state.json"

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkflowNotFound("pricing workflow not found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"cannot read workflow state: {type(exc).__name__}") from exc
        if not isinstance(value, dict):
            raise WorkflowError("workflow state is not an object")
        return value

    @staticmethod
    def _write_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _normalize_pns(pns: List[str]) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for value in pns:
            pn = str(value or "").strip()
            key = pn.upper()
            if pn and key not in seen:
                seen.add(key)
                out.append(pn)
        if not out:
            raise WorkflowValidationError("pns is empty")
        if len(out) > 500:
            raise WorkflowValidationError("at most 500 unique PNs are allowed")
        return out

    @staticmethod
    def _request_dict(req: PricingWorkflowCreateReq) -> Dict[str, Any]:
        return req.model_dump() if hasattr(req, "model_dump") else req.dict()

    @staticmethod
    def _task_id_for_key(key: str) -> str:
        return "pricing-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _effect_key(task_id: str) -> str:
        return f"DPA:{task_id}"

    @staticmethod
    def _append_trace(
        task: Dict[str, Any],
        *,
        event: str,
        actor: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        trace = task.setdefault("trace", [])
        trace.append(
            {
                "seq": len(trace) + 1,
                "trace_id": f"step-{uuid.uuid4().hex}",
                "at": utc_now_iso(),
                "event": event,
                "actor": actor,
                "from_state": from_state if from_state is not None else task.get("state"),
                "to_state": to_state if to_state is not None else task.get("state"),
                "metadata": dict(metadata or {}),
            }
        )

    def _transition(
        self,
        task: Dict[str, Any],
        to_state: str,
        *,
        event: str,
        actor: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if to_state not in ALL_STATES:
            raise WorkflowValidationError(f"invalid target state: {to_state}")
        from_state = str(task.get("state") or "")
        task["state"] = to_state
        task["updated_at"] = utc_now_iso()
        task["version"] = int(task.get("version") or 0) + 1
        task["lease"] = None
        self._append_trace(
            task,
            event=event,
            actor=actor,
            from_state=from_state,
            to_state=to_state,
            metadata=metadata,
        )

    def create(
        self,
        req: PricingWorkflowCreateReq,
        compute_rows: Callable[[List[str], bool], Dict[str, Any]],
        *,
        default_apply_black_markup: bool = True,
    ) -> Dict[str, Any]:
        data = self._request_dict(req)
        pns = self._normalize_pns(list(data.get("pns") or []))
        supplied_key = str(data.get("idempotency_key") or "").strip()
        idempotency_key = supplied_key or f"generated:{uuid.uuid4().hex}"
        task_id = self._task_id_for_key(idempotency_key)
        state_path = self._state_path(task_id)

        with self._locked():
            if state_path.exists():
                existing = self._read_json(state_path)
                existing["idempotent_replay"] = True
                return existing

            apply_black_markup = data.get("apply_black_markup")
            if apply_black_markup is None:
                apply_black_markup = default_apply_black_markup
            now = utc_now_iso()
            task: Dict[str, Any] = {
                "schema_version": 1,
                "task_id": task_id,
                "idempotency_key": idempotency_key,
                "idempotency_key_supplied": bool(supplied_key),
                "effect_key": self._effect_key(task_id),
                "state": "pricing",
                "version": 1,
                "created_at": now,
                "updated_at": now,
                "input": {
                    "pns": pns,
                    "source": str(data.get("source") or "manual").strip() or "manual",
                    "notify": bool(data.get("notify", True)),
                    "apply_black_markup": bool(apply_black_markup),
                    "gsp_payload": dict(data.get("gsp_payload") or {}),
                },
                "pricing_result": None,
                "validation": None,
                "submission": {
                    "authorized": bool(data.get("submission_authorized", False)),
                    "attempts": 0,
                    "max_attempts": int(data.get("max_submission_attempts") or 2),
                    "last_outcome": None,
                    "pla_no": None,
                    "resume_only": False,
                },
                "verification": {
                    "attempts": 0,
                    "max_attempts": int(data.get("max_verification_attempts") or 3),
                    "last_outcome": None,
                },
                "approval": {"checks": 0, "status": None},
                "lease": None,
                "processed_reports": {},
                "last_error": None,
                "trace": [],
            }
            self._append_trace(task, event="workflow_created", actor="api", from_state=None, to_state="pricing")
            self._write_json(state_path, task)

            try:
                pricing_result = compute_rows(pns, bool(apply_black_markup))
                if not isinstance(pricing_result, dict):
                    raise TypeError("pricing result is not an object")
                task["pricing_result"] = pricing_result
                self._transition(task, "validating", event="pricing_completed", actor="pricing_engine")
                report = pricing_result.get("report") or {}
                rows = pricing_result.get("rows") or []
                not_found = list(report.get("not_found") or [])
                validation_errors: List[str] = []
                if len(rows) != len(pns):
                    validation_errors.append("pricing row count does not match PN count")
                if not_found:
                    validation_errors.append(f"{len(not_found)} PN(s) were not found")
                task["validation"] = {
                    "ok": not validation_errors,
                    "errors": validation_errors,
                    "not_found": not_found,
                    "validated_at": utc_now_iso(),
                    "gsp_payload_present": bool((task.get("input") or {}).get("gsp_payload")),
                }
                if validation_errors:
                    self._transition(
                        task,
                        "manual_review",
                        event="validation_failed",
                        actor="orchestrator",
                        metadata={"errors": validation_errors},
                    )
                elif bool((task.get("submission") or {}).get("authorized")):
                    self._transition(task, "submitting", event="validation_passed", actor="orchestrator")
                else:
                    self._transition(
                        task,
                        "manual_review",
                        event="submission_authorization_required",
                        actor="orchestrator",
                    )
            except Exception as exc:
                task["last_error"] = f"{type(exc).__name__}: {exc}"
                self._transition(
                    task,
                    "failed",
                    event="pricing_failed",
                    actor="pricing_engine",
                    metadata={"error_type": type(exc).__name__, "error": str(exc)[:1000]},
                )
            self._write_json(state_path, task)
            return task

    def read(self, task_id: str) -> Dict[str, Any]:
        with self._locked():
            return self._read_json(self._state_path(task_id))

    @staticmethod
    def redact(task: Dict[str, Any]) -> Dict[str, Any]:
        redacted = json.loads(json.dumps(task, ensure_ascii=False))
        task_input = redacted.get("input") or {}
        payload = task_input.get("gsp_payload")
        task_input["gsp_payload"] = {"present": bool(payload)}
        redacted["input"] = task_input
        return redacted

    def list(self, *, limit: int = 50, state: Optional[str] = None) -> Dict[str, Any]:
        if state and state not in ALL_STATES:
            raise WorkflowValidationError(f"invalid state filter: {state}")
        with self._locked():
            records: List[Dict[str, Any]] = []
            paths = sorted(self.tasks_dir.glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in paths:
                try:
                    task = self._read_json(path)
                except WorkflowError:
                    continue
                if task.get("schema_version") != 1 or (state and task.get("state") != state):
                    continue
                records.append(self.redact(task))
                if len(records) >= max(1, min(int(limit), 500)):
                    break
            return {"count": len(records), "tasks": records}

    def _recover_expired_leases(self) -> None:
        now = utc_now()
        for path in self.tasks_dir.glob("*/state.json"):
            try:
                task = self._read_json(path)
            except WorkflowError:
                continue
            lease = task.get("lease") or {}
            expires_at = _parse_iso(lease.get("expires_at"))
            if not lease or not expires_at or expires_at > now:
                continue
            action = str(lease.get("action") or "")
            if action == "submit_gsp" and task.get("state") == "submitting":
                task["submission"]["last_outcome"] = "lease_expired_result_unknown"
                self._transition(
                    task,
                    "verifying",
                    event="submit_lease_expired",
                    actor="orchestrator",
                    metadata={"lease_id": lease.get("lease_id"), "safety": "verify_before_retry"},
                )
            else:
                task["lease"] = None
                task["updated_at"] = utc_now_iso()
                task["version"] = int(task.get("version") or 0) + 1
                self._append_trace(
                    task,
                    event="lease_expired",
                    actor="orchestrator",
                    metadata={"action": action, "lease_id": lease.get("lease_id")},
                )
            self._write_json(path, task)

    def claim(
        self,
        *,
        worker_id: str,
        capabilities: List[str],
        lease_seconds: int,
    ) -> Dict[str, Any]:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise WorkflowValidationError("worker_id is empty")
        allowed_actions = {str(item or "").strip() for item in capabilities}
        lease_seconds = max(30, min(int(lease_seconds), 900))
        with self._locked():
            self._recover_expired_leases()
            paths = sorted(self.tasks_dir.glob("*/state.json"), key=lambda p: p.stat().st_mtime)
            for path in paths:
                try:
                    task = self._read_json(path)
                except WorkflowError:
                    continue
                state = str(task.get("state") or "")
                action = ACTION_FOR_STATE.get(state)
                if not action or action not in allowed_actions or task.get("lease"):
                    continue

                if action == "submit_gsp":
                    if not bool((task.get("submission") or {}).get("authorized")):
                        self._transition(
                            task,
                            "manual_review",
                            event="unauthorized_submission_blocked",
                            actor="orchestrator",
                        )
                        self._write_json(path, task)
                        continue
                    attempts = int((task.get("submission") or {}).get("attempts") or 0)
                    maximum = int((task.get("submission") or {}).get("max_attempts") or 1)
                    resume_only = bool((task.get("submission") or {}).get("resume_only"))
                    if attempts >= maximum and not resume_only:
                        self._transition(
                            task,
                            "manual_review",
                            event="submission_attempt_limit_reached",
                            actor="orchestrator",
                        )
                        self._write_json(path, task)
                        continue
                    if not resume_only:
                        task["submission"]["attempts"] = attempts + 1
                elif action == "verify_gsp_submission":
                    attempts = int((task.get("verification") or {}).get("attempts") or 0)
                    maximum = int((task.get("verification") or {}).get("max_attempts") or 1)
                    if attempts >= maximum:
                        self._transition(
                            task,
                            "manual_review",
                            event="verification_attempt_limit_reached",
                            actor="orchestrator",
                        )
                        self._write_json(path, task)
                        continue
                    task["verification"]["attempts"] = attempts + 1
                elif action == "check_gsp_approval":
                    task["approval"]["checks"] = int((task.get("approval") or {}).get("checks") or 0) + 1

                now = utc_now()
                lease = {
                    "lease_id": f"lease-{uuid.uuid4().hex}",
                    "worker_id": worker_id,
                    "action": action,
                    "claimed_at": now.isoformat(),
                    "expires_at": (now + timedelta(seconds=lease_seconds)).isoformat(),
                }
                task["lease"] = lease
                task["updated_at"] = now.isoformat()
                task["version"] = int(task.get("version") or 0) + 1
                self._append_trace(
                    task,
                    event="action_claimed",
                    actor=worker_id,
                    metadata={"action": action, "lease_id": lease["lease_id"]},
                )
                self._write_json(path, task)
                return {
                    "claimed": True,
                    "task_id": task["task_id"],
                    "state": task["state"],
                    "version": task["version"],
                    "effect_key": task["effect_key"],
                    "action": action,
                    "lease": lease,
                    "input": task["input"],
                    "pricing_result": task["pricing_result"],
                    "submission": task["submission"],
                    "verification": task["verification"],
                }
            return {"claimed": False, "task": None}

    @staticmethod
    def _remember_report(task: Dict[str, Any], report_id: str, response: Dict[str, Any]) -> None:
        reports = task.setdefault("processed_reports", {})
        reports[report_id] = {"processed_at": utc_now_iso(), "response": response}
        if len(reports) > 100:
            for key in list(reports)[: len(reports) - 100]:
                reports.pop(key, None)

    def report(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_id: str,
        report_id: str,
        outcome: str,
        pla_no: Optional[str] = None,
        detail: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        outcome = str(outcome or "").strip().lower()
        report_id = str(report_id or "").strip()
        with self._locked():
            path = self._state_path(task_id)
            task = self._read_json(path)
            prior = (task.get("processed_reports") or {}).get(report_id)
            if prior:
                response = dict(prior.get("response") or {})
                response["idempotent_replay"] = True
                return response

            lease = task.get("lease") or {}
            if lease.get("lease_id") != lease_id:
                raise WorkflowConflict("lease is missing, expired, or belongs to another action")
            if lease.get("worker_id") != worker_id:
                raise WorkflowConflict("lease belongs to another worker")
            action = str(lease.get("action") or "")
            state = str(task.get("state") or "")
            metadata = {
                "action": action,
                "outcome": outcome,
                "pla_no": str(pla_no or "").strip() or None,
                "detail": str(detail or "")[:1000] or None,
                "evidence": dict(evidence or {}),
            }

            if action == "submit_gsp" and state == "submitting":
                task["submission"]["last_outcome"] = outcome
                if outcome == "submitted" and pla_no:
                    task["submission"]["pla_no"] = str(pla_no).strip()
                    task["submission"]["resume_only"] = False
                    self._transition(task, "under_approval", event="gsp_submitted", actor=worker_id, metadata=metadata)
                elif outcome in {"unknown", "timeout", "transport_error"}:
                    self._transition(
                        task,
                        "verifying",
                        event="gsp_submit_result_unknown",
                        actor=worker_id,
                        metadata=metadata,
                    )
                elif outcome in {"manual_review", "invalid_payload", "rejected"}:
                    self._transition(task, "manual_review", event="gsp_submit_blocked", actor=worker_id, metadata=metadata)
                else:
                    raise WorkflowValidationError(f"invalid submit outcome: {outcome}")
            elif action == "verify_gsp_submission" and state == "verifying":
                task["verification"]["last_outcome"] = outcome
                if outcome == "found" and pla_no:
                    task["submission"]["pla_no"] = str(pla_no).strip()
                    self._transition(task, "under_approval", event="gsp_submission_found", actor=worker_id, metadata=metadata)
                elif outcome == "incomplete" and pla_no:
                    task["submission"]["pla_no"] = str(pla_no).strip()
                    if bool((evidence or {}).get("resumable")):
                        task["submission"]["resume_only"] = True
                        self._transition(
                            task,
                            "submitting",
                            event="gsp_application_found_incomplete",
                            actor=worker_id,
                            metadata={**metadata, "resume_only": True},
                        )
                    else:
                        self._transition(
                            task,
                            "manual_review",
                            event="gsp_application_resume_not_proven_safe",
                            actor=worker_id,
                            metadata=metadata,
                        )
                elif outcome == "absent":
                    task["submission"]["resume_only"] = False
                    attempts = int((task.get("submission") or {}).get("attempts") or 0)
                    maximum = int((task.get("submission") or {}).get("max_attempts") or 1)
                    if attempts < maximum:
                        self._transition(
                            task,
                            "submitting",
                            event="gsp_submission_confirmed_absent",
                            actor=worker_id,
                            metadata={**metadata, "safe_to_retry": True},
                        )
                    else:
                        self._transition(
                            task,
                            "manual_review",
                            event="gsp_submission_attempts_exhausted",
                            actor=worker_id,
                            metadata=metadata,
                        )
                elif outcome in {"unknown", "timeout", "transport_error", "manual_review"}:
                    self._transition(
                        task,
                        "manual_review",
                        event="gsp_verification_inconclusive",
                        actor=worker_id,
                        metadata=metadata,
                    )
                else:
                    raise WorkflowValidationError(f"invalid verification outcome: {outcome}")
            elif action == "check_gsp_approval" and state == "under_approval":
                task["approval"]["status"] = outcome
                if outcome == "approved":
                    self._transition(task, "approved", event="gsp_approved", actor=worker_id, metadata=metadata)
                elif outcome == "rejected":
                    self._transition(task, "rejected", event="gsp_rejected", actor=worker_id, metadata=metadata)
                elif outcome in {"under_approval", "pending"}:
                    task["lease"] = None
                    task["updated_at"] = utc_now_iso()
                    task["version"] = int(task.get("version") or 0) + 1
                    self._append_trace(
                        task,
                        event="gsp_still_under_approval",
                        actor=worker_id,
                        metadata=metadata,
                    )
                elif outcome in {"unknown", "timeout", "transport_error", "manual_review"}:
                    self._transition(task, "manual_review", event="gsp_approval_inconclusive", actor=worker_id, metadata=metadata)
                else:
                    raise WorkflowValidationError(f"invalid approval outcome: {outcome}")
            else:
                raise WorkflowConflict(f"action {action!r} is not valid while task is {state!r}")

            response = {
                "ok": True,
                "task_id": task["task_id"],
                "state": task["state"],
                "version": task["version"],
                "pla_no": (task.get("submission") or {}).get("pla_no"),
            }
            self._remember_report(task, report_id, response)
            self._write_json(path, task)
            return response

    def retry(
        self,
        task_id: str,
        *,
        reason: str,
        target_state: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._locked():
            path = self._state_path(task_id)
            task = self._read_json(path)
            if task.get("state") not in {"manual_review", "failed"}:
                raise WorkflowConflict("only manual_review or failed tasks can be retried")
            if expected_version is not None and int(task.get("version") or 0) != int(expected_version):
                raise WorkflowConflict("workflow version changed")
            if target_state is None:
                target_state = (
                    "verifying"
                    if int((task.get("submission") or {}).get("attempts") or 0)
                    else "submitting"
                )
            if target_state not in {"submitting", "verifying", "under_approval"}:
                raise WorkflowValidationError("invalid retry target state")
            if target_state == "submitting" and not bool((task.get("validation") or {}).get("ok")):
                raise WorkflowValidationError("cannot submit a task that did not pass validation; create a corrected task")
            if target_state == "submitting" and int((task.get("submission") or {}).get("attempts") or 0):
                raise WorkflowValidationError("cannot bypass verification after a prior submission attempt")
            if target_state == "verifying" and not int((task.get("submission") or {}).get("attempts") or 0):
                raise WorkflowValidationError("cannot verify before any submission attempt")
            if target_state == "under_approval" and not str((task.get("submission") or {}).get("pla_no") or "").strip():
                raise WorkflowValidationError("cannot check approval without a PLA number")
            if target_state == "submitting":
                task["submission"]["authorized"] = True
            self._transition(
                task,
                target_state,
                event="manual_retry_authorized",
                actor="operator",
                metadata={"reason": str(reason)[:1000]},
            )
            self._write_json(path, task)
            return task
