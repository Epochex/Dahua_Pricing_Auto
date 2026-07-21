from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

try:  # The backend is deployed on Linux; the thread lock still covers tests.
    import fcntl
except ImportError:  # pragma: no cover - Windows never hosts this store
    fcntl = None  # type: ignore[assignment]


ARTIFACT_KINDS = {"workflow", "skill", "prompt", "policy"}
ARTIFACT_STATUSES = {"draft", "candidate", "active", "retired", "rejected"}
ALLOWED_TRANSITIONS = {
    "draft": {"candidate", "rejected"},
    "candidate": {"active", "rejected"},
    "active": {"retired"},
    "retired": set(),
    "rejected": set(),
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


class RegistryError(RuntimeError):
    pass


class RegistryNotFound(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


class RegistryValidationError(RegistryError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RegistryValidationError("artifact fields must be JSON serializable") from exc


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _deepcopy(value: Any) -> Any:
    # A JSON round trip prevents callers from retaining mutable references and
    # also keeps persisted values within the format accepted by the store.
    return json.loads(_canonical_json(value))


class EvolutionRegistry:
    """Immutable, durable registry for executable workflow artifacts.

    The registry deliberately stores definitions, not Python callables.  A task
    resolves an environment once and persists the returned ``pins``.  Later
    retries can pass those pins back to :meth:`resolve_bundle`, so an activation
    made in the meantime cannot change an in-flight task's behavior.

    Every mutation is protected by a process lock and persisted with atomic
    replacement.  Audit records form a hash chain so accidental history edits
    are detectable during startup/read.
    """

    SCHEMA_VERSION = 1

    def __init__(self, runtime_dir: Path, *, state_path: Optional[Path] = None):
        runtime_dir = Path(runtime_dir)
        self.state_path = Path(state_path) if state_path is not None else runtime_dir / "evolution" / "registry.json"
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a+b") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "schema_version": EvolutionRegistry.SCHEMA_VERSION,
            "artifacts": {},
            "bindings": {},
            "audit": [],
        }

    def _read_state(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty_state()
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"cannot read registry: {type(exc).__name__}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != self.SCHEMA_VERSION:
            raise RegistryError("unsupported or invalid registry schema")
        if not isinstance(raw.get("artifacts"), dict) or not isinstance(raw.get("bindings"), dict):
            raise RegistryError("invalid registry collections")
        if not isinstance(raw.get("audit"), list):
            raise RegistryError("invalid registry audit log")
        self._verify_audit_chain(raw["audit"])
        return raw

    def _write_state(self, state: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
            try:
                directory_fd = os.open(str(self.state_path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except (AttributeError, OSError):  # pragma: no cover - filesystem dependent
                pass
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _validate_identifier(label: str, value: Any) -> str:
        text = str(value or "").strip()
        if not _IDENTIFIER.fullmatch(text):
            raise RegistryValidationError(f"invalid {label}")
        return text

    @classmethod
    def _coordinates(cls, kind: Any, name: Any, version: Any) -> tuple[str, str, str, str]:
        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in ARTIFACT_KINDS:
            raise RegistryValidationError(f"invalid artifact kind: {clean_kind}")
        clean_name = cls._validate_identifier("artifact name", name)
        clean_version = cls._validate_identifier("artifact version", version)
        return clean_kind, clean_name, clean_version, f"{clean_kind}:{clean_name}@{clean_version}"

    @staticmethod
    def _binding_key(kind: str, name: str) -> str:
        return f"{kind}:{name}"

    @staticmethod
    def _append_audit(
        state: Dict[str, Any],
        *,
        event: str,
        actor: str,
        artifact_id: Optional[str] = None,
        environment: Optional[str] = None,
        reason: str = "",
        details: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        audit = state.setdefault("audit", [])
        previous_hash = audit[-1]["event_hash"] if audit else None
        record: Dict[str, Any] = {
            "seq": len(audit) + 1,
            "audit_id": f"audit-{uuid.uuid4().hex}",
            "at": _utc_now_iso(),
            "event": event,
            "actor": actor,
            "artifact_id": artifact_id,
            "environment": environment,
            "reason": reason,
            "details": _deepcopy(dict(details or {})),
            "previous_hash": previous_hash,
        }
        record["event_hash"] = _hash_json(record)
        audit.append(record)
        return record

    @staticmethod
    def _verify_audit_chain(audit: List[Any]) -> None:
        previous_hash: Optional[str] = None
        for expected_seq, value in enumerate(audit, start=1):
            if not isinstance(value, dict):
                raise RegistryError("invalid registry audit record")
            supplied_hash = value.get("event_hash")
            payload = dict(value)
            payload.pop("event_hash", None)
            if (
                value.get("seq") != expected_seq
                or value.get("previous_hash") != previous_hash
                or supplied_hash != _hash_json(payload)
            ):
                raise RegistryError("registry audit chain verification failed")
            previous_hash = supplied_hash

    def register(
        self,
        kind: str,
        name: str,
        version: str,
        content: Any,
        *,
        parent_version: Optional[str] = None,
        source: Any = "manual",
        created_by: str = "system",
        json_schema: Optional[Mapping[str, Any]] = None,
        business_constraints: Optional[Any] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        status: str = "draft",
    ) -> Dict[str, Any]:
        """Register an immutable artifact or replay the exact same request.

        Reusing ``kind/name/version`` with identical immutable fields is
        idempotent.  Reusing it with any changed field is rejected; creating a
        new version is the only update mechanism.
        """

        clean_kind, clean_name, clean_version, artifact_id = self._coordinates(kind, name, version)
        clean_creator = str(created_by or "").strip()
        if not clean_creator:
            raise RegistryValidationError("created_by is required")
        clean_status = str(status or "").strip().lower()
        if clean_status not in {"draft", "candidate"}:
            raise RegistryValidationError("new artifacts may only be draft or candidate")
        clean_parent = None
        if parent_version is not None:
            clean_parent = self._validate_identifier("parent version", parent_version)

        immutable: Dict[str, Any] = {
            "kind": clean_kind,
            "name": clean_name,
            "version": clean_version,
            "content": _deepcopy(content),
            "parent_version": clean_parent,
            "source": _deepcopy(source),
            "created_by": clean_creator,
            "json_schema": _deepcopy(dict(json_schema or {})),
            "business_constraints": _deepcopy(business_constraints if business_constraints is not None else {}),
            "metadata": _deepcopy(dict(metadata or {})),
        }
        content_hash = _hash_json(immutable["content"])
        artifact_hash = _hash_json(immutable)

        with self._locked():
            state = self._read_state()
            artifacts = state["artifacts"]
            existing = artifacts.get(artifact_id)
            if existing is not None:
                if existing.get("artifact_hash") != artifact_hash:
                    raise RegistryConflict(
                        f"artifact {artifact_id} is immutable; register a new version"
                    )
                result = _deepcopy(existing)
                result["idempotent_replay"] = True
                return result

            if clean_parent is not None:
                parent_id = f"{clean_kind}:{clean_name}@{clean_parent}"
                if parent_id not in artifacts:
                    raise RegistryValidationError(f"parent artifact not found: {parent_id}")
                if clean_parent == clean_version:
                    raise RegistryValidationError("artifact cannot be its own parent")

            now = _utc_now_iso()
            artifact = {
                "artifact_id": artifact_id,
                **immutable,
                "content_hash": content_hash,
                "artifact_hash": artifact_hash,
                "status": clean_status,
                "created_at": now,
                "updated_at": now,
            }
            artifacts[artifact_id] = artifact
            self._append_audit(
                state,
                event="artifact.registered",
                actor=clean_creator,
                artifact_id=artifact_id,
                details={
                    "status": clean_status,
                    "content_hash": content_hash,
                    "artifact_hash": artifact_hash,
                    "parent_version": clean_parent,
                },
            )
            self._write_state(state)
            result = _deepcopy(artifact)
            result["idempotent_replay"] = False
            return result

    def get(self, kind: str, name: str, version: str) -> Dict[str, Any]:
        _, _, _, artifact_id = self._coordinates(kind, name, version)
        with self._locked():
            state = self._read_state()
            artifact = state["artifacts"].get(artifact_id)
            if artifact is None:
                raise RegistryNotFound(f"artifact not found: {artifact_id}")
            return _deepcopy(artifact)

    def list(
        self,
        *,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clean_kind = None
        if kind is not None:
            clean_kind = str(kind).strip().lower()
            if clean_kind not in ARTIFACT_KINDS:
                raise RegistryValidationError(f"invalid artifact kind: {clean_kind}")
        clean_name = self._validate_identifier("artifact name", name) if name is not None else None
        clean_status = None
        if status is not None:
            clean_status = str(status).strip().lower()
            if clean_status not in ARTIFACT_STATUSES:
                raise RegistryValidationError(f"invalid artifact status: {clean_status}")
        with self._locked():
            artifacts = self._read_state()["artifacts"].values()
            result = [
                _deepcopy(item)
                for item in artifacts
                if (clean_kind is None or item.get("kind") == clean_kind)
                and (clean_name is None or item.get("name") == clean_name)
                and (clean_status is None or item.get("status") == clean_status)
            ]
        result.sort(key=lambda item: (item["kind"], item["name"], item["created_at"], item["version"]))
        return result

    def transition(
        self,
        kind: str,
        name: str,
        version: str,
        to_status: str,
        *,
        actor: str,
        reason: str,
    ) -> Dict[str, Any]:
        _, _, _, artifact_id = self._coordinates(kind, name, version)
        target = str(to_status or "").strip().lower()
        clean_actor = str(actor or "").strip()
        clean_reason = str(reason or "").strip()
        if target not in ARTIFACT_STATUSES:
            raise RegistryValidationError(f"invalid target status: {target}")
        if not clean_actor or not clean_reason:
            raise RegistryValidationError("actor and reason are required")
        if target == "active":
            raise RegistryValidationError("use activate() so an active artifact is environment-bound")

        with self._locked():
            state = self._read_state()
            artifact = state["artifacts"].get(artifact_id)
            if artifact is None:
                raise RegistryNotFound(f"artifact not found: {artifact_id}")
            current = str(artifact.get("status") or "")
            if target == current:
                return _deepcopy(artifact)
            if target not in ALLOWED_TRANSITIONS.get(current, set()):
                raise RegistryConflict(f"illegal artifact transition: {current} -> {target}")
            if current == "active" and self._bound_environments(state, artifact_id):
                raise RegistryConflict("cannot retire an artifact while an environment is bound to it")
            artifact["status"] = target
            artifact["updated_at"] = _utc_now_iso()
            self._append_audit(
                state,
                event="artifact.transitioned",
                actor=clean_actor,
                artifact_id=artifact_id,
                reason=clean_reason,
                details={"from_status": current, "to_status": target},
            )
            self._write_state(state)
            return _deepcopy(artifact)

    @staticmethod
    def _bound_environments(state: Mapping[str, Any], artifact_id: str) -> List[str]:
        environments: List[str] = []
        for environment, bindings in state.get("bindings", {}).items():
            if isinstance(bindings, dict) and any(
                isinstance(binding, dict) and binding.get("artifact_id") == artifact_id
                for binding in bindings.values()
            ):
                environments.append(str(environment))
        return environments

    def activate(
        self,
        kind: str,
        name: str,
        version: str,
        *,
        environment: str,
        actor: str,
        reason: str,
    ) -> Dict[str, Any]:
        clean_kind, clean_name, _, artifact_id = self._coordinates(kind, name, version)
        clean_environment = self._validate_identifier("environment", environment)
        clean_actor = str(actor or "").strip()
        clean_reason = str(reason or "").strip()
        if not clean_actor or not clean_reason:
            raise RegistryValidationError("actor and reason are required")
        binding_key = self._binding_key(clean_kind, clean_name)

        with self._locked():
            state = self._read_state()
            artifacts = state["artifacts"]
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                raise RegistryNotFound(f"artifact not found: {artifact_id}")
            if artifact.get("status") not in {"candidate", "active", "retired"}:
                raise RegistryConflict("only candidate, active, or retired artifacts can be activated")

            environment_bindings = state["bindings"].setdefault(clean_environment, {})
            previous = environment_bindings.get(binding_key)
            if isinstance(previous, dict) and previous.get("artifact_id") == artifact_id:
                return {
                    "artifact": _deepcopy(artifact),
                    "binding": _deepcopy(previous),
                    "idempotent_replay": True,
                }

            now = _utc_now_iso()
            if artifact.get("status") in {"candidate", "retired"}:
                prior_status = str(artifact.get("status"))
                artifact["status"] = "active"
                artifact["updated_at"] = now
                self._append_audit(
                    state,
                    event="artifact.transitioned",
                    actor=clean_actor,
                    artifact_id=artifact_id,
                    environment=clean_environment,
                    reason=clean_reason,
                    details={"from_status": prior_status, "to_status": "active"},
                )

            binding = {
                "artifact_id": artifact_id,
                "kind": clean_kind,
                "name": clean_name,
                "version": artifact["version"],
                "activated_at": now,
                "activated_by": clean_actor,
                "reason": clean_reason,
            }
            environment_bindings[binding_key] = binding
            self._append_audit(
                state,
                event="artifact.activated",
                actor=clean_actor,
                artifact_id=artifact_id,
                environment=clean_environment,
                reason=clean_reason,
                details={"binding": binding_key, "previous_artifact_id": (previous or {}).get("artifact_id") if isinstance(previous, dict) else None},
            )

            previous_id = previous.get("artifact_id") if isinstance(previous, dict) else None
            if previous_id and previous_id != artifact_id:
                old = artifacts.get(previous_id)
                if old is not None and old.get("status") == "active" and not self._bound_environments(state, previous_id):
                    old["status"] = "retired"
                    old["updated_at"] = now
                    self._append_audit(
                        state,
                        event="artifact.transitioned",
                        actor=clean_actor,
                        artifact_id=previous_id,
                        environment=clean_environment,
                        reason=f"superseded: {clean_reason}",
                        details={"from_status": "active", "to_status": "retired", "replacement": artifact_id},
                    )

            self._write_state(state)
            return {
                "artifact": _deepcopy(artifact),
                "binding": _deepcopy(binding),
                "idempotent_replay": False,
            }

    def activate_bundle(
        self,
        pins: Mapping[str, str],
        *,
        environment: str,
        actor: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Atomically bind a complete version set to one environment.

        All references are validated before a single state replacement.  A
        retired immutable artifact may be reactivated so rollback restores the
        exact previous bundle instead of manufacturing a new version.
        """

        clean_environment = self._validate_identifier("environment", environment)
        clean_actor = str(actor or "").strip()
        clean_reason = str(reason or "").strip()
        if not clean_actor or not clean_reason:
            raise RegistryValidationError("actor and reason are required")
        if not isinstance(pins, Mapping) or not pins:
            raise RegistryValidationError("bundle pins are required")

        normalized: Dict[str, str] = {}
        for raw_key, raw_version in pins.items():
            key = str(raw_key or "").strip()
            if ":" not in key:
                raise RegistryValidationError("pin keys must use kind:name")
            raw_kind, raw_name = key.split(":", 1)
            kind, name, _, artifact_id = self._coordinates(raw_kind, raw_name, raw_version)
            normalized[self._binding_key(kind, name)] = artifact_id

        with self._locked():
            state = self._read_state()
            artifacts = state["artifacts"]
            selected: Dict[str, Dict[str, Any]] = {}
            for key, artifact_id in normalized.items():
                artifact = artifacts.get(artifact_id)
                if artifact is None:
                    raise RegistryNotFound(f"artifact not found: {artifact_id}")
                if artifact.get("status") not in {"candidate", "active", "retired"}:
                    raise RegistryConflict(
                        f"artifact {artifact_id} with status {artifact.get('status')} cannot be activated"
                    )
                selected[key] = artifact

            now = _utc_now_iso()
            environment_bindings = state["bindings"].setdefault(clean_environment, {})
            previous_ids = {
                str(binding.get("artifact_id"))
                for binding in environment_bindings.values()
                if isinstance(binding, dict) and binding.get("artifact_id")
            }
            for stale_key in set(environment_bindings) - set(selected):
                environment_bindings.pop(stale_key, None)
            for key, artifact in selected.items():
                artifact_id = str(artifact["artifact_id"])
                prior_status = str(artifact.get("status") or "")
                if prior_status != "active":
                    artifact["status"] = "active"
                    artifact["updated_at"] = now
                    self._append_audit(
                        state,
                        event="artifact.transitioned",
                        actor=clean_actor,
                        artifact_id=artifact_id,
                        environment=clean_environment,
                        reason=clean_reason,
                        details={"from_status": prior_status, "to_status": "active", "atomic_bundle": True},
                    )
                previous = environment_bindings.get(key)
                environment_bindings[key] = {
                    "artifact_id": artifact_id,
                    "kind": artifact["kind"],
                    "name": artifact["name"],
                    "version": artifact["version"],
                    "activated_at": now,
                    "activated_by": clean_actor,
                    "reason": clean_reason,
                }
                self._append_audit(
                    state,
                    event="artifact.activated",
                    actor=clean_actor,
                    artifact_id=artifact_id,
                    environment=clean_environment,
                    reason=clean_reason,
                    details={
                        "binding": key,
                        "previous_artifact_id": (previous or {}).get("artifact_id")
                        if isinstance(previous, dict)
                        else None,
                        "atomic_bundle": True,
                    },
                )

            selected_ids = {str(item["artifact_id"]) for item in selected.values()}
            for previous_id in sorted(previous_ids - selected_ids):
                old = artifacts.get(previous_id)
                if old is not None and old.get("status") == "active" and not self._bound_environments(state, previous_id):
                    old["status"] = "retired"
                    old["updated_at"] = now
                    self._append_audit(
                        state,
                        event="artifact.transitioned",
                        actor=clean_actor,
                        artifact_id=previous_id,
                        environment=clean_environment,
                        reason=f"superseded: {clean_reason}",
                        details={"from_status": "active", "to_status": "retired", "atomic_bundle": True},
                    )

            bundle_hash = _hash_json(
                {key: artifact["artifact_hash"] for key, artifact in sorted(selected.items())}
            )
            self._append_audit(
                state,
                event="bundle.activated",
                actor=clean_actor,
                environment=clean_environment,
                reason=clean_reason,
                details={
                    "pins": {key: item["version"] for key, item in selected.items()},
                    "bundle_hash": bundle_hash,
                },
            )
            self._write_state(state)
            return {
                "environment": clean_environment,
                "pins": {key: item["version"] for key, item in selected.items()},
                "artifacts": {key: _deepcopy(item) for key, item in selected.items()},
                "bundle_hash": bundle_hash,
            }

    def resolve_bundle(
        self,
        environment: str,
        *,
        pins: Optional[Mapping[str, str]] = None,
        allow_candidate: bool = False,
    ) -> Dict[str, Any]:
        """Resolve current bindings or replay a task's exact version pins.

        Pin keys are ``kind:name`` and values are versions.  Active and retired
        artifacts are resolvable because an in-flight task must survive a later
        deployment.  ``allow_candidate`` is reserved for offline/shadow runs.
        """

        clean_environment = self._validate_identifier("environment", environment)
        with self._locked():
            state = self._read_state()
            bindings = state["bindings"].get(clean_environment, {})
            if not isinstance(bindings, dict):
                raise RegistryError("invalid environment bindings")
            resolved_refs: Dict[str, str] = {}
            for key, binding in bindings.items():
                if isinstance(binding, dict) and binding.get("artifact_id"):
                    resolved_refs[str(key)] = str(binding["artifact_id"])

            for raw_key, raw_version in dict(pins or {}).items():
                key = str(raw_key or "").strip()
                if ":" not in key:
                    raise RegistryValidationError("pin keys must use kind:name")
                pin_kind, pin_name = key.split(":", 1)
                pin_kind, pin_name, pin_version, artifact_id = self._coordinates(
                    pin_kind, pin_name, raw_version
                )
                normalized_key = self._binding_key(pin_kind, pin_name)
                resolved_refs[normalized_key] = artifact_id

            artifacts: Dict[str, Dict[str, Any]] = {}
            resolved_pins: Dict[str, str] = {}
            acceptable = {"active", "retired"} | ({"candidate"} if allow_candidate else set())
            for key, artifact_id in sorted(resolved_refs.items()):
                artifact = state["artifacts"].get(artifact_id)
                if artifact is None:
                    raise RegistryNotFound(f"bound artifact not found: {artifact_id}")
                if artifact.get("status") not in acceptable:
                    raise RegistryConflict(
                        f"artifact {artifact_id} with status {artifact.get('status')} cannot execute"
                    )
                artifacts[key] = _deepcopy(artifact)
                resolved_pins[key] = str(artifact["version"])

            return {
                "environment": clean_environment,
                "resolved_at": _utc_now_iso(),
                "pins": resolved_pins,
                "artifacts": artifacts,
                "bundle_hash": _hash_json(
                    {key: item["artifact_hash"] for key, item in sorted(artifacts.items())}
                ),
            }

    def list_audit(
        self,
        *,
        artifact_id: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._locked():
            audit = self._read_state()["audit"]
            return [
                _deepcopy(item)
                for item in audit
                if (artifact_id is None or item.get("artifact_id") == artifact_id)
                and (environment is None or item.get("environment") == environment)
            ]


# A descriptive alias keeps integrations readable without duplicating behavior.
SkillWorkflowRegistry = EvolutionRegistry


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ARTIFACT_KINDS",
    "ARTIFACT_STATUSES",
    "EvolutionRegistry",
    "RegistryConflict",
    "RegistryError",
    "RegistryNotFound",
    "RegistryValidationError",
    "SkillWorkflowRegistry",
]
