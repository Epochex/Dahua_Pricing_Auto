from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from backend.app.mapping_evidence import EvidenceRecord

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class MappingDocumentStoreError(RuntimeError):
    pass


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


class MappingDocumentStore:
    """Versioned, immutable evidence records used by mapping retrieval."""

    def __init__(self, runtime_dir: Path):
        self.root = Path(runtime_dir) / "mapping-documents"
        self.state_path = self.root / "records.json"
        self.lock_path = self.root / ".records.lock"
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
        return {"schema_version": 1, "records": {}}

    def _read(self) -> Dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise MappingDocumentStoreError(
                f"cannot read mapping document store: {type(exc).__name__}"
            ) from exc
        if not isinstance(state, dict) or not isinstance(state.get("records"), dict):
            raise MappingDocumentStoreError("mapping document store is invalid")
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
    def _record(raw: Mapping[str, Any]) -> EvidenceRecord:
        attributes = raw.get("attributes") or {}
        if not isinstance(attributes, Mapping):
            raise MappingDocumentStoreError("attributes must be an object")
        record = EvidenceRecord(
            evidence_id=str(raw.get("evidence_id") or "").strip(),
            source_type=str(raw.get("source_type") or "").strip(),
            source_version=str(raw.get("source_version") or "").strip(),
            authority=str(raw.get("authority") or "").strip(),
            subject_ref=str(raw.get("subject_ref") or "").strip(),
            family_ref=str(raw.get("family_ref") or "").strip() or None,
            content=str(raw.get("content") or "").strip(),
            attributes={str(key): str(value) for key, value in attributes.items()},
            approved=bool(raw.get("approved", False)),
        )
        if not record.content:
            raise MappingDocumentStoreError("content is required")
        return record

    def register(self, raw: Mapping[str, Any]) -> Dict[str, Any]:
        try:
            record = self._record(raw)
        except ValueError as exc:
            raise MappingDocumentStoreError(str(exc)) from exc
        payload = record.to_dict()
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        stored = {**payload, "content_hash": f"sha256:{digest}"}
        with self._locked():
            state = self._read()
            existing = state["records"].get(record.evidence_id)
            if existing:
                if existing != stored:
                    raise MappingDocumentStoreError(
                        "evidence_id already exists with different immutable content"
                    )
                return _copy({**existing, "idempotent_replay": True})
            state["records"][record.evidence_id] = stored
            self._write(state)
            return _copy(stored)

    def list(
        self,
        *,
        subject_ref: Optional[str] = None,
        family_ref: Optional[str] = None,
        source_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._locked():
            rows = list(self._read()["records"].values())
        selected = [
            item
            for item in rows
            if (not subject_ref or item.get("subject_ref") == subject_ref)
            and (not family_ref or item.get("family_ref") == family_ref)
            and (not source_version or item.get("source_version") == source_version)
        ]
        selected.sort(key=lambda item: str(item.get("evidence_id") or ""))
        return {"count": len(selected), "records": _copy(selected)}

    def records(self) -> list[EvidenceRecord]:
        return [self._record(item) for item in self.list()["records"]]
