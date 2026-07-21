from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from .event_store import sha256_digest

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class CaseMemoryError(RuntimeError):
    pass


class CaseMemory:
    """Review queue for converting interventions into evaluation evidence.

    An intervention is never a skill by itself.  The store retains a hash and
    workflow reference, requires an explicit accept/reject review, and exposes
    repeated accepted reason clusters as *suggestions* only.  It cannot publish
    an artifact or change a production binding.
    """

    def __init__(self, runtime_dir: Path):
        self.root = Path(runtime_dir) / "evolution" / "case-memory"
        self.state_path = self.root / "cases.json"
        self.lock_path = self.root / ".cases.lock"
        self._thread_lock = threading.RLock()

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
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise CaseMemoryError(f"cannot read case memory: {type(exc).__name__}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("cases"), dict):
            raise CaseMemoryError("case memory is invalid")
        return value

    def _write(self, state: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _copy(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))

    def observe(self, task: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        state_name = str(task.get("state") or "")
        if state_name not in {"manual_review", "failed", "rejected"}:
            return None
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise CaseMemoryError("task_id is required")
        trace = [item for item in (task.get("trace") or []) if isinstance(item, Mapping)]
        reason = str((trace[-1] if trace else {}).get("event") or state_name)
        input_hash = sha256_digest(task.get("input") or {})
        stable = f"{task_id}:{input_hash}:{reason}"
        case_id = "case-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
        context = dict(task.get("execution_context") or {})
        now = datetime.now(timezone.utc).isoformat()
        observed = {
            "case_id": case_id,
            "task_id": task_id,
            "run_id": context.get("run_id"),
            "session_id": context.get("session_id"),
            "bundle_hash": context.get("bundle_hash"),
            "pins": dict(context.get("pins") or {}),
            "input_ref": f"workflow-task:{task_id}",
            "input_hash": input_hash,
            "observed_terminal_state": state_name,
            "reason_code": reason,
            "status": "observed",
            "observed_at": now,
            "review": None,
        }
        with self._locked():
            state = self._read()
            existing = state["cases"].get(case_id)
            if existing:
                return self._copy({**existing, "idempotent_replay": True})
            state["cases"][case_id] = observed
            self._write(state)
            return self._copy(observed)

    def review(
        self,
        case_id: str,
        *,
        disposition: str,
        reviewer: str,
        rationale: str,
        expected_terminal_state: Optional[str] = None,
        expected_action: Optional[str] = None,
        strata: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        clean_disposition = str(disposition or "").strip().lower()
        if clean_disposition not in {"accepted", "rejected"}:
            raise CaseMemoryError("disposition must be accepted or rejected")
        if not str(reviewer or "").strip() or not str(rationale or "").strip():
            raise CaseMemoryError("reviewer and rationale are required")
        if clean_disposition == "accepted" and not (expected_terminal_state or expected_action):
            raise CaseMemoryError("accepted cases require an expected state or action")
        with self._locked():
            state = self._read()
            case = state["cases"].get(str(case_id))
            if not case:
                raise CaseMemoryError("case not found")
            review = {
                "disposition": clean_disposition,
                "reviewer": str(reviewer).strip(),
                "rationale": str(rationale).strip()[:1000],
                "expected_terminal_state": str(expected_terminal_state or "").strip() or None,
                "expected_action": str(expected_action or "").strip() or None,
                "strata": {str(key): str(value) for key, value in dict(strata or {}).items()},
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
            if case.get("review"):
                comparable = dict(case["review"])
                comparable.pop("reviewed_at", None)
                requested = dict(review)
                requested.pop("reviewed_at", None)
                if comparable != requested:
                    raise CaseMemoryError("case review is immutable")
                return self._copy({**case, "idempotent_replay": True})
            case["status"] = clean_disposition
            case["review"] = review
            self._write(state)
            return self._copy(case)

    def list(self, *, status: Optional[str] = None) -> Dict[str, Any]:
        with self._locked():
            rows = list(self._read()["cases"].values())
        if status:
            rows = [item for item in rows if item.get("status") == status]
        rows.sort(key=lambda item: str(item.get("observed_at") or ""), reverse=True)
        return {"count": len(rows), "cases": self._copy(rows)}

    def evaluation_cases(self) -> Dict[str, Any]:
        accepted = self.list(status="accepted")["cases"]
        cases = []
        for item in accepted:
            review = item["review"]
            cases.append(
                {
                    "case_id": item["case_id"],
                    "input_ref": item["input_ref"],
                    "input_hash": str(item["input_hash"]).removeprefix("sha256:"),
                    "expected_terminal_state": review.get("expected_terminal_state"),
                    "expected_action": review.get("expected_action"),
                    "strata": review.get("strata") or {},
                }
            )
        return {"count": len(cases), "cases": cases}

    def suggestions(self, *, minimum_occurrences: int = 3) -> Dict[str, Any]:
        minimum = max(2, int(minimum_occurrences))
        groups: Dict[str, list[str]] = {}
        for item in self.list(status="accepted")["cases"]:
            groups.setdefault(str(item.get("reason_code") or "unknown"), []).append(item["case_id"])
        rows = [
            {
                "reason_code": reason,
                "case_count": len(case_ids),
                "case_ids": case_ids,
                "eligible_for_candidate_proposal": len(case_ids) >= minimum,
                "automatic_publish": False,
            }
            for reason, case_ids in sorted(groups.items())
        ]
        return {"minimum_occurrences": minimum, "suggestions": rows}
