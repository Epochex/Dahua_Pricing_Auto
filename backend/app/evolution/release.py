from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

try:  # Linux production lock; tests retain the in-process lock.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


PHASES = {
    "proposed",
    "shadow",
    "shadow_ready",
    "canary",
    "canary_ready",
    "active",
    "rejected",
    "rolled_back",
}
TERMINAL_PHASES = {"rejected", "rolled_back"}
HARD_METRICS = (
    "constraint_violations",
    "illegal_transitions",
    "duplicate_effects",
)


class ReleaseError(RuntimeError):
    pass


class ReleaseConflict(ReleaseError):
    pass


class ReleaseValidationError(ReleaseError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReleaseController:
    """Durable promotion gates for workflow bundles.

    The controller never invokes a tool or an external system.  It only records
    whether a version is eligible for shadow/canary/active traffic.  Production
    effects remain disabled unless ``execution_enabled`` is explicitly set;
    this lets development and replay exercise the complete release state
    machine without contacting GSP, Windows agents, or notification channels.
    """

    def __init__(
        self,
        root: Path,
        *,
        execution_enabled: bool = False,
        min_shadow_cases: int = 20,
        min_canary_cases: int = 20,
        max_canary_fraction: float = 0.10,
        max_manual_review_delta: float = 0.02,
        max_p95_latency_ratio: float = 1.20,
    ):
        self.root = Path(root)
        self.state_path = self.root / "releases.json"
        self.lock_path = self.root / ".release.lock"
        self.execution_enabled = bool(execution_enabled)
        self.min_shadow_cases = max(1, int(min_shadow_cases))
        self.min_canary_cases = max(1, int(min_canary_cases))
        self.max_canary_fraction = max(0.0, min(float(max_canary_fraction), 1.0))
        self.max_manual_review_delta = float(max_manual_review_delta)
        self.max_p95_latency_ratio = float(max_p95_latency_ratio)
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

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": 1, "releases": {}, "active_by_environment": {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"cannot read release state: {type(exc).__name__}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("releases"), dict):
            raise ReleaseError("release state is invalid")
        value.setdefault("active_by_environment", {})
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
    def _require_text(value: Any, field: str) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 300:
            raise ReleaseValidationError(f"{field} is required and must be <= 300 characters")
        return text

    @staticmethod
    def _copy(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _offline_gate(summary: Mapping[str, Any]) -> None:
        if not bool(summary.get("passed")):
            raise ReleaseValidationError("offline evaluation did not pass")
        for key in HARD_METRICS:
            if int(summary.get(key) or 0) != 0:
                raise ReleaseValidationError(f"offline hard gate failed: {key}")

    def propose(
        self,
        *,
        candidate_bundle_id: str,
        baseline_bundle_id: str,
        evaluation_id: str,
        evaluation_summary: Mapping[str, Any],
        candidate_pins: Optional[Mapping[str, str]] = None,
        baseline_pins: Optional[Mapping[str, str]] = None,
        environment: str = "production",
        actor: str = "release-api",
        release_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        candidate = self._require_text(candidate_bundle_id, "candidate_bundle_id")
        baseline = self._require_text(baseline_bundle_id, "baseline_bundle_id")
        evaluation = self._require_text(evaluation_id, "evaluation_id")
        env = self._require_text(environment, "environment")
        actor_name = self._require_text(actor, "actor")
        if candidate == baseline:
            raise ReleaseValidationError("candidate and baseline must differ")
        self._offline_gate(evaluation_summary)
        clean_candidate_pins = {str(key): str(value) for key, value in dict(candidate_pins or {}).items()}
        clean_baseline_pins = {str(key): str(value) for key, value in dict(baseline_pins or {}).items()}
        if bool(clean_candidate_pins) != bool(clean_baseline_pins):
            raise ReleaseValidationError("candidate_pins and baseline_pins must be supplied together")
        if clean_candidate_pins and set(clean_candidate_pins) != set(clean_baseline_pins):
            raise ReleaseValidationError("candidate and baseline bundles must contain the same pin keys")
        stable_key = str(release_key or f"{env}:{candidate}:{evaluation}")
        release_id = "release-" + __import__("hashlib").sha256(stable_key.encode("utf-8")).hexdigest()[:24]
        with self._locked():
            state = self._read()
            existing = state["releases"].get(release_id)
            if existing:
                expected = {
                    "candidate_bundle_id": candidate,
                    "baseline_bundle_id": baseline,
                    "evaluation_id": evaluation,
                    "environment": env,
                    "candidate_pins": clean_candidate_pins,
                    "baseline_pins": clean_baseline_pins,
                }
                if any(existing.get(key) != value for key, value in expected.items()):
                    raise ReleaseConflict("release key was already used with different immutable inputs")
                return self._copy({**existing, "idempotent_replay": True})
            now = _now()
            release = {
                "schema_version": 1,
                "release_id": release_id,
                "environment": env,
                "candidate_bundle_id": candidate,
                "baseline_bundle_id": baseline,
                "evaluation_id": evaluation,
                "evaluation_summary": self._copy(dict(evaluation_summary)),
                "candidate_pins": clean_candidate_pins,
                "baseline_pins": clean_baseline_pins,
                "phase": "proposed",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "canary_fraction": 0.0,
                "execution_enabled": self.execution_enabled,
                "effects_allowed": False,
                "windows_agent_allowed": False,
                "notifications_allowed": False,
                "kill_switch": False,
                "windows": [],
                "audit": [{"at": now, "event": "release_proposed", "actor": actor_name}],
            }
            state["releases"][release_id] = release
            self._write(state)
            return self._copy(release)

    def get(self, release_id: str) -> Dict[str, Any]:
        with self._locked():
            release = self._read()["releases"].get(str(release_id))
            if not release:
                raise ReleaseValidationError("release not found")
            return self._copy(release)

    def list(self, *, environment: Optional[str] = None) -> Dict[str, Any]:
        with self._locked():
            state = self._read()
            rows = list(state["releases"].values())
            if environment:
                rows = [item for item in rows if item.get("environment") == environment]
            rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return {
                "count": len(rows),
                "releases": self._copy(rows),
                "active_by_environment": self._copy(state.get("active_by_environment") or {}),
            }

    @staticmethod
    def _transition(release: Dict[str, Any], phase: str, event: str, actor: str, **metadata: Any) -> None:
        if phase not in PHASES:
            raise ReleaseValidationError(f"invalid phase: {phase}")
        release["phase"] = phase
        release["revision"] = int(release.get("revision") or 0) + 1
        release["updated_at"] = _now()
        release.setdefault("audit", []).append(
            {"at": release["updated_at"], "event": event, "actor": actor, "metadata": metadata}
        )

    def start_shadow(self, release_id: str, *, actor: str = "release-api") -> Dict[str, Any]:
        with self._locked():
            state = self._read()
            release = state["releases"].get(str(release_id))
            if not release:
                raise ReleaseValidationError("release not found")
            if release.get("phase") == "shadow":
                return self._copy({**release, "idempotent_replay": True})
            if release.get("phase") != "proposed":
                raise ReleaseConflict("only proposed releases can enter shadow")
            release["effects_allowed"] = False
            release["windows_agent_allowed"] = False
            release["notifications_allowed"] = False
            self._transition(release, "shadow", "shadow_started", actor)
            self._write(state)
            return self._copy(release)

    def record_window(
        self,
        release_id: str,
        *,
        window_id: str,
        metrics: Mapping[str, Any],
        actor: str = "metrics-evaluator",
    ) -> Dict[str, Any]:
        wid = self._require_text(window_id, "window_id")
        with self._locked():
            state = self._read()
            release = state["releases"].get(str(release_id))
            if not release:
                raise ReleaseValidationError("release not found")
            if release.get("phase") not in {"shadow", "shadow_ready", "canary", "canary_ready", "active"}:
                raise ReleaseConflict("release is not accepting metric windows")
            windows = release.setdefault("windows", [])
            existing = next((item for item in windows if item.get("window_id") == wid), None)
            if existing:
                if existing.get("metrics") != dict(metrics):
                    raise ReleaseConflict("window_id was already recorded with different metrics")
                return self._copy({**release, "idempotent_replay": True})
            normalized = self._normalize_metrics(metrics)
            windows.append({"window_id": wid, "phase": release["phase"], "at": _now(), "metrics": normalized})

            violations = [key for key in HARD_METRICS if int(normalized.get(key) or 0) != 0]
            if violations:
                release["kill_switch"] = True
                release["effects_allowed"] = False
                release["windows_agent_allowed"] = False
                release["notifications_allowed"] = False
                target = "rolled_back" if release["phase"] in {"canary", "canary_ready", "active"} else "rejected"
                self._transition(release, target, "hard_gate_failed", actor, violations=violations, window_id=wid)
            else:
                self._evaluate_soft_gates(release, normalized, actor, wid)
            self._write(state)
            return self._copy(release)

    @staticmethod
    def _normalize_metrics(metrics: Mapping[str, Any]) -> Dict[str, Any]:
        out = dict(metrics)
        out["sample_count"] = max(0, int(out.get("sample_count") or 0))
        for key in HARD_METRICS:
            out[key] = max(0, int(out.get(key) or 0))
        for key in ("manual_review_delta", "p95_latency_ratio"):
            if key in out and out[key] is not None:
                out[key] = float(out[key])
        return out

    def _evaluate_soft_gates(self, release: Dict[str, Any], metrics: Dict[str, Any], actor: str, wid: str) -> None:
        phase = str(release.get("phase"))
        minimum = self.min_shadow_cases if phase == "shadow" else self.min_canary_cases
        if int(metrics.get("sample_count") or 0) < minimum:
            release["revision"] = int(release.get("revision") or 0) + 1
            release["updated_at"] = _now()
            release.setdefault("audit", []).append(
                {"at": release["updated_at"], "event": "window_recorded", "actor": actor, "metadata": {"window_id": wid}}
            )
            return
        manual_delta = float(metrics.get("manual_review_delta") or 0.0)
        latency_ratio = float(metrics.get("p95_latency_ratio") or 1.0)
        if manual_delta > self.max_manual_review_delta or latency_ratio > self.max_p95_latency_ratio:
            release["kill_switch"] = phase in {"canary", "canary_ready", "active"}
            release["effects_allowed"] = False
            target = "rolled_back" if release["kill_switch"] else "rejected"
            self._transition(
                release,
                target,
                "soft_gate_failed",
                actor,
                manual_review_delta=manual_delta,
                p95_latency_ratio=latency_ratio,
                window_id=wid,
            )
            return
        if phase == "shadow":
            self._transition(release, "shadow_ready", "shadow_gate_passed", actor, window_id=wid)
        elif phase == "canary":
            self._transition(release, "canary_ready", "canary_gate_passed", actor, window_id=wid)
        else:
            release["revision"] = int(release.get("revision") or 0) + 1
            release["updated_at"] = _now()

    def start_canary(
        self,
        release_id: str,
        *,
        fraction: float,
        actor: str,
        approved_by: str,
    ) -> Dict[str, Any]:
        canary_fraction = float(fraction)
        if not 0 < canary_fraction <= self.max_canary_fraction:
            raise ReleaseValidationError(f"canary fraction must be in (0, {self.max_canary_fraction}]")
        approver = self._require_text(approved_by, "approved_by")
        if approver == str(actor or "").strip():
            raise ReleaseValidationError("four-eyes approval requires a different approver")
        with self._locked():
            state = self._read()
            release = state["releases"].get(str(release_id))
            if not release:
                raise ReleaseValidationError("release not found")
            if release.get("phase") != "shadow_ready":
                raise ReleaseConflict("shadow gates have not passed")
            release["canary_fraction"] = canary_fraction
            # The eligibility is recorded, but external effects stay off in the
            # current development deployment unless explicitly enabled.
            release["effects_allowed"] = bool(self.execution_enabled)
            release["windows_agent_allowed"] = False
            release["notifications_allowed"] = False
            self._transition(release, "canary", "canary_started", actor, approved_by=approver)
            self._write(state)
            return self._copy(release)

    def promote(self, release_id: str, *, actor: str, approved_by: str) -> Dict[str, Any]:
        approver = self._require_text(approved_by, "approved_by")
        if approver == str(actor or "").strip():
            raise ReleaseValidationError("four-eyes approval requires a different approver")
        with self._locked():
            state = self._read()
            release = state["releases"].get(str(release_id))
            if not release:
                raise ReleaseValidationError("release not found")
            if release.get("phase") != "canary_ready":
                raise ReleaseConflict("canary gates have not passed")
            env = str(release["environment"])
            previous = state["active_by_environment"].get(env)
            state["active_by_environment"][env] = release["candidate_bundle_id"]
            release["effects_allowed"] = bool(self.execution_enabled)
            release["windows_agent_allowed"] = False
            release["notifications_allowed"] = False
            self._transition(release, "active", "release_promoted", actor, approved_by=approver, previous_bundle=previous)
            self._write(state)
            return self._copy(release)

    def rollback(self, release_id: str, *, reason: str, actor: str = "release-api") -> Dict[str, Any]:
        why = self._require_text(reason, "reason")
        with self._locked():
            state = self._read()
            release = state["releases"].get(str(release_id))
            if not release:
                raise ReleaseValidationError("release not found")
            if release.get("phase") in TERMINAL_PHASES:
                return self._copy({**release, "idempotent_replay": True})
            env = str(release["environment"])
            if state["active_by_environment"].get(env) == release.get("candidate_bundle_id"):
                state["active_by_environment"][env] = release["baseline_bundle_id"]
            release["kill_switch"] = True
            release["effects_allowed"] = False
            release["windows_agent_allowed"] = False
            release["notifications_allowed"] = False
            self._transition(release, "rolled_back", "release_rolled_back", actor, reason=why)
            self._write(state)
            return self._copy(release)
