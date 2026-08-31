from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


TRIAGE_ACTIONS = {"confirm_correct", "correct_mapping", "investigate"}
CHECKPOINT_STATUSES = {"succeeded", "failed", "skipped"}
WRITEBACK_KINDS = {"current_request", "exact_pn_case", "review_queue"}
TERMINAL_STATES = {
    "resolved_correct",
    "resolved_corrected",
    "investigation_complete",
    "written_back",
}


class InvestigationError(RuntimeError):
    pass


class InvestigationNotFound(InvestigationError):
    pass


class InvestigationConflict(InvestigationError):
    pass


class InvestigationValidationError(InvestigationError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _safe_ref(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500 or any(ch in text for ch in ("\n", "\r", "\x00")):
        raise InvestigationValidationError(f"{name} must be a non-empty opaque reference")
    return text


def _safe_code(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 120 or not all(ch.isalnum() or ch in "._:-" for ch in text):
        raise InvestigationValidationError(f"{name} must be a stable code")
    return text


def _refs(name: str, values: Sequence[Any], *, maximum: int = 100) -> list[str]:
    if len(values) > maximum:
        raise InvestigationValidationError(f"{name} supports at most {maximum} references")
    return [_safe_ref(name, item) for item in values]


def _bounded_object(name: str, value: Optional[Mapping[str, Any]], *, maximum: int) -> Dict[str, Any]:
    result = _copy(dict(value or {}))
    if len(_canonical(result)) > maximum:
        raise InvestigationValidationError(f"{name} exceeds {maximum} serialized characters")
    return result


def _bounded_text(name: str, value: Any, *, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum or "\x00" in text:
        raise InvestigationValidationError(f"{name} exceeds its text boundary")
    return text


def _bounded_texts(name: str, values: Sequence[Any], *, count: int, maximum: int) -> list[str]:
    if len(values) > count:
        raise InvestigationValidationError(f"{name} supports at most {count} items")
    return [_bounded_text(name, item, maximum=maximum) for item in values if str(item or "").strip()]


class MappingInvestigationStore:
    """Durable investigation cases with immutable step history.

    Human triage occurs once.  Choosing ``investigate`` transfers the case to
    the agent.  Agent completion does not create a second mandatory approval
    state.  Operators may inspect, correct, retry, or write back from any
    recorded checkpoint; corrections append new history and never rewrite the
    original observation.
    """

    def __init__(self, runtime_dir: Path, *, durable_writes: bool = True):
        self.root = Path(runtime_dir) / "mapping-investigations"
        self.state_path = self.root / "cases.json"
        self.lock_path = self.root / ".cases.lock"
        self._thread_lock = threading.RLock()
        self.durable_writes = bool(durable_writes)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a+b") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"schema_version": 1, "cases": {}}

    def _read(self) -> Dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise InvestigationError(f"cannot read investigation store: {type(exc).__name__}") from exc
        if not isinstance(state, dict) or not isinstance(state.get("cases"), dict):
            raise InvestigationError("investigation store is invalid")
        return state

    def _write(self, state: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                if self.durable_writes:
                    os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _event(case: Dict[str, Any], kind: str, *, actor: str, payload: Mapping[str, Any]) -> None:
        case["events"].append(
            {
                "seq": len(case["events"]) + 1,
                "kind": _safe_code("event kind", kind),
                "actor": _safe_ref("actor", actor),
                "payload": _copy(dict(payload)),
                "occurred_at": _utc_now(),
            }
        )

    @staticmethod
    def _require_revision(case: Mapping[str, Any], expected_revision: int) -> None:
        if int(case.get("revision") or 0) != int(expected_revision):
            raise InvestigationConflict(
                f"case revision is {case.get('revision')}, expected {expected_revision}"
            )

    @staticmethod
    def _get_case(state: Mapping[str, Any], case_id: str) -> Dict[str, Any]:
        case = state["cases"].get(str(case_id))
        if not isinstance(case, dict):
            raise InvestigationNotFound("investigation case not found")
        return case

    @staticmethod
    def _advance(case: Dict[str, Any], state_name: str) -> None:
        case["state"] = state_name
        case["revision"] = int(case["revision"]) + 1
        case["updated_at"] = _utc_now()

    def create(
        self,
        *,
        subject_ref: str,
        data_version: str,
        verification: Mapping[str, Any],
        input_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        subject = _safe_ref("subject_ref", subject_ref)
        version = _safe_ref("data_version", data_version)
        verification_copy = _copy(dict(verification))
        verification_hash = _digest(verification_copy)
        stable = f"{subject}:{version}:{verification_hash}"
        case_id = "mapping-case-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
        now = _utc_now()
        case = {
            "case_id": case_id,
            "subject_ref": subject,
            "input_hash": str(input_hash or "").removeprefix("sha256:") or None,
            "data_version": version,
            "verification_hash": verification_hash,
            "verification": verification_copy,
            "state": "awaiting_triage",
            "revision": 1,
            "triage": None,
            "checkpoints": [],
            "checkpoint_corrections": [],
            "retries": [],
            "agent_result": None,
            "writebacks": [],
            "reconciliation_required": False,
            "events": [],
            "created_at": now,
            "updated_at": now,
        }
        self._event(case, "case_created", actor="mapping-verifier", payload={"verification_hash": verification_hash})
        with self._locked():
            state = self._read()
            existing = state["cases"].get(case_id)
            if existing:
                return _copy({**existing, "idempotent_replay": True})
            state["cases"][case_id] = case
            self._write(state)
        return _copy(case)

    def get(self, case_id: str) -> Dict[str, Any]:
        with self._locked():
            return _copy(self._get_case(self._read(), case_id))

    def list(self, *, state_name: Optional[str] = None) -> Dict[str, Any]:
        with self._locked():
            rows = list(self._read()["cases"].values())
        if state_name:
            rows = [item for item in rows if item.get("state") == state_name]
        rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return {"count": len(rows), "cases": _copy(rows)}

    def triage(
        self,
        case_id: str,
        *,
        action: str,
        reviewer: str,
        rationale_code: str,
        expected_revision: int,
        corrected_category: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_action = str(action or "").strip().lower()
        if clean_action not in TRIAGE_ACTIONS:
            raise InvestigationValidationError(f"action must be one of {sorted(TRIAGE_ACTIONS)}")
        reviewer_ref = _safe_ref("reviewer", reviewer)
        rationale = _safe_code("rationale_code", rationale_code)
        corrected = str(corrected_category or "").strip().upper() or None
        if clean_action == "correct_mapping" and not corrected:
            raise InvestigationValidationError("correct_mapping requires corrected_category")
        if clean_action != "correct_mapping" and corrected:
            raise InvestigationValidationError("corrected_category is only valid for correct_mapping")

        with self._locked():
            state = self._read()
            case = self._get_case(state, case_id)
            self._require_revision(case, expected_revision)
            if case["state"] != "awaiting_triage":
                raise InvestigationConflict("human triage is allowed exactly once")
            case["triage"] = {
                "action": clean_action,
                "reviewer": reviewer_ref,
                "rationale_code": rationale,
                "corrected_category": corrected,
                "decided_at": _utc_now(),
            }
            next_state = {
                "confirm_correct": "resolved_correct",
                "correct_mapping": "resolved_corrected",
                "investigate": "investigating",
            }[clean_action]
            self._event(case, "human_triage", actor=reviewer_ref, payload=case["triage"])
            self._advance(case, next_state)
            self._write(state)
            return _copy(case)

    def append_checkpoint(
        self,
        case_id: str,
        *,
        tool_name: str,
        status: str,
        summary_code: str,
        input_refs: Sequence[str],
        output_refs: Sequence[str],
        idempotency_key: str,
        expected_revision: int,
        planner_decision: Optional[Mapping[str, Any]] = None,
        observation_summary: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        clean_status = str(status or "").strip().lower()
        if clean_status not in CHECKPOINT_STATUSES:
            raise InvestigationValidationError(f"status must be one of {sorted(CHECKPOINT_STATUSES)}")
        tool = _safe_code("tool_name", tool_name)
        summary = _safe_code("summary_code", summary_code)
        idem = _safe_ref("idempotency_key", idempotency_key)
        normalized_inputs = _refs("input_refs", input_refs)
        normalized_outputs = _refs("output_refs", output_refs)
        normalized_decision = _bounded_object(
            "planner_decision", planner_decision, maximum=4000
        )
        normalized_observation = _bounded_object(
            "observation_summary", observation_summary, maximum=8000
        )

        with self._locked():
            state = self._read()
            case = self._get_case(state, case_id)
            self._require_revision(case, expected_revision)
            if case["state"] != "investigating":
                raise InvestigationConflict("checkpoints require an investigating case")
            existing = next(
                (item for item in case["checkpoints"] if item["idempotency_key"] == idem),
                None,
            )
            requested = {
                "tool_name": tool,
                "status": clean_status,
                "summary_code": summary,
                "input_refs": normalized_inputs,
                "output_refs": normalized_outputs,
                "idempotency_key": idem,
                "planner_decision": normalized_decision,
                "observation_summary": normalized_observation,
            }
            if existing:
                comparable = {key: existing.get(key, {} if key in {"planner_decision", "observation_summary"} else None) for key in requested}
                if comparable != requested:
                    raise InvestigationConflict("idempotency key already has different checkpoint data")
                return _copy({**case, "idempotent_replay": True})
            checkpoint = {
                "checkpoint_id": f"checkpoint-{len(case['checkpoints']) + 1}",
                **requested,
                "recorded_at": _utc_now(),
            }
            case["checkpoints"].append(checkpoint)
            self._event(case, "checkpoint_recorded", actor="mapping-agent", payload=checkpoint)
            self._advance(case, "investigating")
            self._write(state)
            return _copy(case)

    def complete_agent_result(
        self,
        case_id: str,
        *,
        candidate_category: Optional[str],
        recommended_action: str,
        stop_reason: str,
        evidence_refs: Sequence[str],
        counter_evidence_refs: Sequence[str],
        unresolved_codes: Sequence[str],
        expected_revision: int,
        metrics: Optional[Mapping[str, Any]] = None,
        finding_codes: Sequence[str] = (),
        investigation_summary: str = "",
        recommended_next_steps: Sequence[str] = (),
    ) -> Dict[str, Any]:
        metrics_copy = _copy(dict(metrics or {}))
        for key, value in metrics_copy.items():
            if not isinstance(key, str) or not isinstance(value, (int, float, str, bool, type(None))):
                raise InvestigationValidationError("metrics must contain scalar JSON values")
        result = {
            "candidate_category": str(candidate_category or "").strip().upper() or None,
            "recommended_action": _safe_code("recommended_action", recommended_action),
            "stop_reason": _safe_code("stop_reason", stop_reason),
            "evidence_refs": _refs("evidence_refs", evidence_refs),
            "counter_evidence_refs": _refs("counter_evidence_refs", counter_evidence_refs),
            "unresolved_codes": [_safe_code("unresolved_code", item) for item in unresolved_codes],
            "finding_codes": [_safe_code("finding_code", item) for item in finding_codes],
            "investigation_summary": _bounded_text(
                "investigation_summary", investigation_summary, maximum=4000
            ),
            "recommended_next_steps": _bounded_texts(
                "recommended_next_steps",
                recommended_next_steps,
                count=20,
                maximum=500,
            ),
            "metrics": metrics_copy,
            "completed_at": _utc_now(),
        }
        if result["candidate_category"] and not result["evidence_refs"]:
            raise InvestigationValidationError("a candidate category requires supporting evidence")
        with self._locked():
            state = self._read()
            case = self._get_case(state, case_id)
            self._require_revision(case, expected_revision)
            if case["state"] != "investigating":
                raise InvestigationConflict("agent completion requires an investigating case")
            case["agent_result"] = result
            self._event(case, "agent_investigation_completed", actor="mapping-agent", payload=result)
            self._advance(case, "investigation_complete")
            self._write(state)
            return _copy(case)

    def correct_checkpoint(
        self,
        case_id: str,
        *,
        checkpoint_id: str,
        reviewer: str,
        reason_code: str,
        corrected_output_refs: Sequence[str],
        expected_revision: int,
    ) -> Dict[str, Any]:
        checkpoint_ref = _safe_code("checkpoint_id", checkpoint_id)
        reviewer_ref = _safe_ref("reviewer", reviewer)
        correction = {
            "checkpoint_id": checkpoint_ref,
            "reviewer": reviewer_ref,
            "reason_code": _safe_code("reason_code", reason_code),
            "corrected_output_refs": _refs("corrected_output_refs", corrected_output_refs),
            "corrected_at": _utc_now(),
        }
        with self._locked():
            state = self._read()
            case = self._get_case(state, case_id)
            self._require_revision(case, expected_revision)
            if not any(item["checkpoint_id"] == checkpoint_ref for item in case["checkpoints"]):
                raise InvestigationNotFound("checkpoint not found")
            case["checkpoint_corrections"].append(correction)
            case["agent_result"] = None
            if case["writebacks"]:
                case["reconciliation_required"] = True
            self._event(case, "checkpoint_corrected", actor=reviewer_ref, payload=correction)
            self._advance(case, "investigating")
            self._write(state)
            return _copy(case)

    def retry_from_checkpoint(
        self,
        case_id: str,
        *,
        checkpoint_id: str,
        requested_by: str,
        reason_code: str,
        expected_revision: int,
    ) -> Dict[str, Any]:
        checkpoint_ref = _safe_code("checkpoint_id", checkpoint_id)
        actor = _safe_ref("requested_by", requested_by)
        retry = {
            "retry_id": f"retry-{uuid.uuid4().hex[:12]}",
            "checkpoint_id": checkpoint_ref,
            "reason_code": _safe_code("reason_code", reason_code),
            "requested_by": actor,
            "requested_at": _utc_now(),
        }
        with self._locked():
            state = self._read()
            case = self._get_case(state, case_id)
            self._require_revision(case, expected_revision)
            if not any(item["checkpoint_id"] == checkpoint_ref for item in case["checkpoints"]):
                raise InvestigationNotFound("checkpoint not found")
            case["retries"].append(retry)
            case["agent_result"] = None
            if case["writebacks"]:
                case["reconciliation_required"] = True
            self._event(case, "checkpoint_retry_requested", actor=actor, payload=retry)
            self._advance(case, "investigating")
            self._write(state)
            return _copy(case)

    def record_writeback(
        self,
        case_id: str,
        *,
        writeback_kind: str,
        target_ref: str,
        action_receipt_ref: str,
        written_by: str,
        expected_revision: int,
    ) -> Dict[str, Any]:
        clean_kind = str(writeback_kind or "").strip().lower()
        if clean_kind not in WRITEBACK_KINDS:
            raise InvestigationValidationError(f"writeback_kind must be one of {sorted(WRITEBACK_KINDS)}")
        actor = _safe_ref("written_by", written_by)
        writeback = {
            "writeback_id": f"writeback-{uuid.uuid4().hex[:12]}",
            "kind": clean_kind,
            "target_ref": _safe_ref("target_ref", target_ref),
            "action_receipt_ref": _safe_ref("action_receipt_ref", action_receipt_ref),
            "written_by": actor,
            "written_at": _utc_now(),
        }
        with self._locked():
            state = self._read()
            case = self._get_case(state, case_id)
            self._require_revision(case, expected_revision)
            if case["state"] not in TERMINAL_STATES:
                raise InvestigationConflict("writeback requires a resolved or completed investigation")
            case["writebacks"].append(writeback)
            case["reconciliation_required"] = False
            self._event(case, "writeback_recorded", actor=actor, payload=writeback)
            self._advance(case, "written_back")
            self._write(state)
            return _copy(case)
