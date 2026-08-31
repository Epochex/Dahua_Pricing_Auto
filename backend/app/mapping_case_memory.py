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

from backend.app.mapping_evidence import EvidenceRecord

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


MEMORY_SCOPES = {"exact_subject", "model_family"}


class MappingCaseMemoryError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


class MappingCaseMemory:
    """Immutable, evidence-bearing memory derived from resolved cases.

    Agent conclusions enter reusable memory only after an action receipt has
    been recorded.  One-off decisions default to exact-subject scope and never
    publish a mapping rule automatically.
    """

    def __init__(self, runtime_dir: Path):
        self.root = Path(runtime_dir) / "mapping-case-memory"
        self.state_path = self.root / "entries.json"
        self.lock_path = self.root / ".entries.lock"
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
        return {"schema_version": 1, "entries": {}}

    def _read(self) -> Dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise MappingCaseMemoryError(f"cannot read mapping case memory: {type(exc).__name__}") from exc
        if not isinstance(state, dict) or not isinstance(state.get("entries"), dict):
            raise MappingCaseMemoryError("mapping case memory is invalid")
        return state

    def _write(self, state: Mapping[str, Any]) -> None:
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
    def _resolved_decision(case: Mapping[str, Any]) -> tuple[str, str, list[str]]:
        state = str(case.get("state") or "")
        verification = dict(case.get("verification") or {})
        selected = str(verification.get("selected_category") or "").strip().upper()
        triage = dict(case.get("triage") or {})
        if state == "resolved_correct":
            if not selected:
                raise MappingCaseMemoryError("confirmed case has no selected category")
            return selected, "human_confirmed", []
        if state == "resolved_corrected":
            corrected = str(triage.get("corrected_category") or "").strip().upper()
            if not corrected:
                raise MappingCaseMemoryError("corrected case has no category")
            return corrected, "human_corrected", []
        if state == "written_back":
            result = dict(case.get("agent_result") or {})
            candidate = str(result.get("candidate_category") or "").strip().upper()
            evidence_refs = [str(item) for item in result.get("evidence_refs") or []]
            if not candidate or not evidence_refs or not case.get("writebacks"):
                raise MappingCaseMemoryError("written-back agent case needs a candidate, evidence, and receipt")
            return candidate, "agent_result_written_back", evidence_refs
        raise MappingCaseMemoryError("only resolved or written-back cases can enter memory")

    def remember(
        self,
        case: Mapping[str, Any],
        *,
        family_ref: Optional[str],
        scope: str = "exact_subject",
        valid_until: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_scope = str(scope or "").strip().lower()
        if clean_scope not in MEMORY_SCOPES:
            raise MappingCaseMemoryError(f"scope must be one of {sorted(MEMORY_SCOPES)}")
        family = str(family_ref or "").strip() or None
        if clean_scope == "model_family" and not family:
            raise MappingCaseMemoryError("model_family scope requires family_ref")
        clean_valid_until = str(valid_until or "").strip() or None
        if clean_valid_until:
            try:
                expiry = datetime.fromisoformat(clean_valid_until.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MappingCaseMemoryError("valid_until must be an ISO-8601 timestamp") from exc
            if expiry.tzinfo is None:
                raise MappingCaseMemoryError("valid_until must include a timezone")
            if expiry <= datetime.now(timezone.utc):
                raise MappingCaseMemoryError("valid_until must be in the future")
        case_id = str(case.get("case_id") or "").strip()
        subject_ref = str(case.get("subject_ref") or "").strip()
        data_version = str(case.get("data_version") or "").strip()
        if not case_id or not subject_ref or not data_version:
            raise MappingCaseMemoryError("case identity is incomplete")
        resolved_category, decision_source, evidence_refs = self._resolved_decision(case)
        original_category = str(
            dict(case.get("verification") or {}).get("selected_category") or ""
        ).strip().upper() or None
        stable = f"{case_id}:{case.get('revision')}:{clean_scope}:{resolved_category}"
        entry_id = "mapping-memory-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
        entry = {
            "entry_id": entry_id,
            "case_id": case_id,
            "subject_ref": subject_ref,
            "family_ref": family,
            "data_version": data_version,
            "original_category": original_category,
            "resolved_category": resolved_category,
            "decision_source": decision_source,
            "evidence_refs": evidence_refs,
            "scope": clean_scope,
            "valid_from": _utc_now(),
            "valid_until": clean_valid_until,
            "automatic_publish": False,
        }
        with self._locked():
            state = self._read()
            existing = state["entries"].get(entry_id)
            if existing:
                return _copy({**existing, "idempotent_replay": True})
            state["entries"][entry_id] = entry
            self._write(state)
            return _copy(entry)

    def search(
        self,
        *,
        subject_ref: Optional[str] = None,
        family_ref: Optional[str] = None,
        data_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._locked():
            rows = list(self._read()["entries"].values())
        now = datetime.now(timezone.utc)
        selected = []
        for item in rows:
            if subject_ref and item.get("subject_ref") != subject_ref:
                continue
            if family_ref and item.get("family_ref") != family_ref:
                continue
            if data_version and item.get("data_version") != data_version:
                continue
            valid_until = item.get("valid_until")
            if valid_until:
                try:
                    expiry = datetime.fromisoformat(str(valid_until).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if expiry <= now:
                    continue
            selected.append(item)
        selected.sort(key=lambda item: str(item.get("valid_from") or ""), reverse=True)
        return {"count": len(selected), "entries": _copy(selected)}

    def as_evidence_records(self) -> list[EvidenceRecord]:
        rows = self.search()["entries"]
        return [
            EvidenceRecord(
                evidence_id=f"approved-case:{item['entry_id']}",
                source_type="mapping_case_memory",
                source_version=str(item["data_version"]),
                authority="approved_human_case",
                subject_ref=str(item["subject_ref"]),
                family_ref=item.get("family_ref"),
                content=f"Resolved product line: {item['resolved_category']}",
                attributes={
                    "product_line": str(item["resolved_category"]),
                    "scope": str(item["scope"]),
                    "decision_source": str(item["decision_source"]),
                },
                approved=True,
            )
            for item in rows
        ]

    def suggestions(self, *, minimum_occurrences: int = 3) -> Dict[str, Any]:
        minimum = max(2, int(minimum_occurrences))
        groups: Dict[tuple[str, str], list[str]] = {}
        with self._locked():
            rows = list(self._read()["entries"].values())
        for item in rows:
            family = str(item.get("family_ref") or "")
            category = str(item.get("resolved_category") or "")
            if family:
                groups.setdefault((family, category), []).append(str(item["entry_id"]))
        suggestions = [
            {
                "family_ref": family,
                "resolved_category": category,
                "case_count": len(entry_ids),
                "entry_ids": entry_ids,
                "eligible_for_rule_proposal": len(entry_ids) >= minimum,
                "automatic_publish": False,
            }
            for (family, category), entry_ids in sorted(groups.items())
        ]
        return {"minimum_occurrences": minimum, "suggestions": suggestions}
