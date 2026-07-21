from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

try:  # Linux production lock; the process-local lock also protects threads.
    import fcntl
except ImportError:  # pragma: no cover - the backend is deployed on Linux
    fcntl = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
REASON_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

EVENT_FIELDS = {
    "event_id",
    "seq",
    "run_id",
    "session_id",
    "task_id",
    "workflow_version",
    "skill_version",
    "prompt_version",
    "pre_state",
    "action",
    "post_state",
    "input_hash",
    "evidence_hash",
    "effect_hash",
    "receipt_hash",
    "evidence_refs",
    "reason",
    "occurred_at",
}

HASH_FIELDS = (
    "input_hash",
    "evidence_hash",
    "effect_hash",
    "receipt_hash",
)

REQUIRED_TEXT_FIELDS = (
    "event_id",
    "run_id",
    "session_id",
    "task_id",
    "workflow_version",
    "skill_version",
    "prompt_version",
    "pre_state",
    "action",
    "post_state",
    "reason",
)


class EventStoreError(RuntimeError):
    """Base class for durable event store errors."""


class EventValidationError(EventStoreError):
    """An event would violate the safe event schema."""


class EventConflict(EventStoreError):
    """An event ID or sequence is already associated with other content."""


class EventCorruption(EventStoreError):
    """The durable log is damaged somewhere other than its recoverable tail."""


class ReplayValidationError(EventStoreError):
    """The event sequence cannot be deterministically replayed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(value: Any) -> str:
    """Return a stable digest without retaining the original business value."""

    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventValidationError("occurred_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EventValidationError("occurred_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _safe_identifier(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_PATTERN.fullmatch(text):
        raise EventValidationError(f"{name} must be a non-sensitive stable identifier")
    return text


def _normalize_event(raw: Mapping[str, Any]) -> Dict[str, Any]:
    unknown = set(raw) - EVENT_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise EventValidationError(
            f"raw or unrecognized event fields are forbidden; store hashes/references instead: {names}"
        )

    event: Dict[str, Any] = {}
    for name in REQUIRED_TEXT_FIELDS:
        if name == "reason":
            reason = str(raw.get(name) or "").strip()
            if not REASON_PATTERN.fullmatch(reason):
                raise EventValidationError("reason must be a stable non-sensitive reason code")
            event[name] = reason
        else:
            event[name] = _safe_identifier(name, raw.get(name))

    try:
        seq = int(raw.get("seq"))
    except (TypeError, ValueError) as exc:
        raise EventValidationError("seq must be a positive integer") from exc
    if seq < 1 or isinstance(raw.get("seq"), bool):
        raise EventValidationError("seq must be a positive integer")
    event["seq"] = seq

    for name in HASH_FIELDS:
        value = raw.get(name)
        if value is None:
            event[name] = None
            continue
        digest = str(value).strip().lower()
        if not HASH_PATTERN.fullmatch(digest):
            raise EventValidationError(f"{name} must be a sha256 digest, not a raw value")
        event[name] = digest

    refs = raw.get("evidence_refs", [])
    if not isinstance(refs, list) or len(refs) > 100:
        raise EventValidationError("evidence_refs must be a list with at most 100 references")
    normalized_refs: List[str] = []
    for ref in refs:
        text = str(ref or "").strip()
        if not text or len(text) > 500 or any(char in text for char in ("\n", "\r", "\x00")):
            raise EventValidationError("evidence_refs contains an invalid reference")
        normalized_refs.append(text)
    event["evidence_refs"] = normalized_refs
    event["occurred_at"] = _parse_time(raw.get("occurred_at"))
    return event


class EventStore:
    """Append-only, checksummed workflow event store with deterministic replay.

    Only hashes and opaque references are accepted for business input and
    evidence.  This makes accidental persistence of prices, credentials, or
    customer data structurally harder than relying on log redaction after the
    fact.
    """

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.store_dir = self.runtime_dir / "evolution" / "events"
        self.log_path = self.store_dir / "events.jsonl"
        self.lock_path = self.store_dir / ".events.lock"
        self._thread_lock = threading.RLock()

    def ensure_dirs(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        self.ensure_dirs()
        with self._thread_lock:
            with self.lock_path.open("a+b") as lock_file:
                if fcntl is not None:
                    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(lock_file.fileno(), mode)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _record_for(event: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "event": dict(event),
            "record_hash": sha256_digest(event),
        }

    @staticmethod
    def _decode_record(line: bytes) -> Dict[str, Any]:
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventCorruption("event log contains invalid JSON") from exc
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise EventCorruption("event log contains an unsupported record")
        raw_event = record.get("event")
        if not isinstance(raw_event, dict):
            raise EventCorruption("event log record has no event object")
        try:
            event = _normalize_event(raw_event)
        except EventValidationError as exc:
            raise EventCorruption(f"persisted event is invalid: {exc}") from exc
        if record.get("record_hash") != sha256_digest(event):
            raise EventCorruption("event log record checksum mismatch")
        return event

    def _read_unlocked(self, *, repair_tail: bool = False) -> tuple[List[Dict[str, Any]], bool]:
        try:
            data = self.log_path.read_bytes()
        except FileNotFoundError:
            return [], False
        except OSError as exc:
            raise EventStoreError(f"cannot read event log: {type(exc).__name__}") from exc

        lines = data.splitlines(keepends=True)
        events: List[Dict[str, Any]] = []
        valid_end = 0
        damaged_tail = False
        for index, line in enumerate(lines):
            payload = line[:-1] if line.endswith(b"\n") else line
            if payload.endswith(b"\r"):
                payload = payload[:-1]
            try:
                event = self._decode_record(payload)
            except EventCorruption:
                if index != len(lines) - 1:
                    raise
                damaged_tail = True
                break
            events.append(event)
            valid_end += len(line)

        if repair_tail and damaged_tail:
            try:
                with self.log_path.open("r+b") as handle:
                    handle.truncate(valid_end)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise EventStoreError(f"cannot repair event log tail: {type(exc).__name__}") from exc
        return events, damaged_tail

    def append(self, raw_event: Mapping[str, Any]) -> Dict[str, Any]:
        """Durably append one event and deduplicate retries by ``event_id``.

        Missing ``seq`` and ``occurred_at`` are assigned while holding the
        process-wide lock.  Reusing an event ID with different content raises
        :class:`EventConflict` instead of silently accepting ambiguous history.
        """

        raw = dict(raw_event)
        event_id = _safe_identifier("event_id", raw.get("event_id"))
        with self._locked(exclusive=True):
            events, damaged_tail = self._read_unlocked(repair_tail=True)
            existing = next((item for item in events if item["event_id"] == event_id), None)
            if existing is not None:
                raw.setdefault("seq", existing["seq"])
                raw.setdefault("occurred_at", existing["occurred_at"])
                candidate = _normalize_event(raw)
                if candidate != existing:
                    raise EventConflict(f"event_id {event_id!r} already has different content")
                return {"event": dict(existing), "idempotent_replay": True, "tail_repaired": damaged_tail}

            task_id = _safe_identifier("task_id", raw.get("task_id"))
            task_events = [item for item in events if item["task_id"] == task_id]
            expected_seq = max((int(item["seq"]) for item in task_events), default=0) + 1
            if raw.get("seq") is None:
                raw["seq"] = expected_seq
            elif raw.get("seq") != expected_seq:
                raise EventConflict(
                    f"task {task_id!r} expects seq {expected_seq}, got {raw.get('seq')!r}"
                )
            raw.setdefault("occurred_at", _utc_now_iso())
            event = _normalize_event(raw)

            record = self._record_for(event)
            encoded = _canonical_bytes(record) + b"\n"
            try:
                with self.log_path.open("ab", buffering=0) as handle:
                    handle.write(encoded)
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise EventStoreError(f"cannot append event: {type(exc).__name__}") from exc
            return {"event": dict(event), "idempotent_replay": False, "tail_repaired": damaged_tail}

    def query(
        self,
        *,
        event_id: Optional[str] = None,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        workflow_version: Optional[str] = None,
        action: Optional[str] = None,
        post_state: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        filters = {
            "event_id": event_id,
            "run_id": run_id,
            "session_id": session_id,
            "task_id": task_id,
            "workflow_version": workflow_version,
            "action": action,
            "post_state": post_state,
        }
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            raise EventValidationError("limit must be a positive integer")
        with self._locked(exclusive=False):
            events, _ = self._read_unlocked()
        selected = [
            dict(event)
            for event in events
            if all(value is None or event.get(name) == value for name, value in filters.items())
        ]
        return selected[:limit] if limit is not None else selected

    @staticmethod
    def _validate_replay(
        events: Sequence[Mapping[str, Any]],
        *,
        allowed_transitions: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> Dict[str, Any]:
        if not events:
            raise ReplayValidationError("task has no events")
        ordered = sorted(events, key=lambda item: int(item["seq"]))
        task_id = str(ordered[0]["task_id"])
        previous_post: Optional[str] = None
        for expected_seq, event in enumerate(ordered, start=1):
            if event.get("task_id") != task_id:
                raise ReplayValidationError("replay contains events from multiple tasks")
            if event.get("seq") != expected_seq:
                raise ReplayValidationError(
                    f"task {task_id!r} has illegal sequence: expected {expected_seq}, got {event.get('seq')!r}"
                )
            pre_state = str(event.get("pre_state") or "")
            post_state = str(event.get("post_state") or "")
            if previous_post is not None and pre_state != previous_post:
                raise ReplayValidationError(
                    f"task {task_id!r} state chain breaks at seq {expected_seq}: "
                    f"expected pre_state {previous_post!r}, got {pre_state!r}"
                )
            if allowed_transitions is not None:
                allowed = set(allowed_transitions.get(pre_state, ()))
                if post_state not in allowed:
                    raise ReplayValidationError(
                        f"illegal transition at seq {expected_seq}: {pre_state!r} -> {post_state!r}"
                    )
            previous_post = post_state
        return {
            "task_id": task_id,
            "initial_state": ordered[0]["pre_state"],
            "current_state": ordered[-1]["post_state"],
            "event_count": len(ordered),
            "last_seq": ordered[-1]["seq"],
            "events": [dict(item) for item in ordered],
        }

    def rebuild_task(
        self,
        task_id: str,
        *,
        allowed_transitions: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> Dict[str, Any]:
        events = self.query(task_id=_safe_identifier("task_id", task_id))
        return self._validate_replay(events, allowed_transitions=allowed_transitions)

    def export_replay_bundle(
        self,
        task_id: str,
        destination: Optional[Path] = None,
        *,
        allowed_transitions: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> Dict[str, Any]:
        """Export a deterministic, hash-addressed replay bundle.

        The bundle deliberately contains no generated timestamp: identical
        event history produces byte-for-byte identical output.
        """

        replay = self.rebuild_task(task_id, allowed_transitions=allowed_transitions)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "task_id": replay["task_id"],
            "initial_state": replay["initial_state"],
            "current_state": replay["current_state"],
            "event_count": replay["event_count"],
            "events": replay["events"],
        }
        bundle = dict(payload)
        bundle["bundle_hash"] = sha256_digest(payload)
        if destination is not None:
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                with tmp.open("wb") as handle:
                    handle.write(_canonical_bytes(bundle) + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        return bundle
