from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


HARD_CONSTRAINTS = (
    "price_boundary",
    "illegal_transition",
    "duplicate_effect",
)


class EvaluationError(ValueError):
    """Raised when an evaluation definition or adapter result is invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise EvaluationError(f"case data must be JSON-compatible, got {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class EvaluationCase:
    """An immutable replay case.

    Raw input is available to the in-process adapter but is never written to the
    evaluation artifact.  Audits use ``input_ref`` and a SHA-256 input digest.
    An input may be omitted when the adapter resolves a protected ``input_ref``.
    """

    case_id: str
    input: Mapping[str, Any] = field(default_factory=dict)
    expected_terminal_state: Optional[str] = None
    expected_action: Optional[str] = None
    hard_constraints: tuple[str, ...] = HARD_CONSTRAINTS
    strata: Mapping[str, str] = field(default_factory=dict)
    input_ref: Optional[str] = None
    input_hash: Optional[str] = None

    def __post_init__(self) -> None:
        case_id = str(self.case_id or "").strip()
        if not case_id:
            raise EvaluationError("case_id is required")
        constraints = tuple(dict.fromkeys(str(item) for item in self.hard_constraints))
        unknown = sorted(set(constraints) - set(HARD_CONSTRAINTS))
        if unknown:
            raise EvaluationError(f"unknown hard constraints: {', '.join(unknown)}")

        frozen_input = _freeze(dict(self.input))
        frozen_strata = MappingProxyType(
            {str(key): str(value) for key, value in sorted(dict(self.strata).items())}
        )
        supplied_hash = str(self.input_hash or "").strip().lower()
        if supplied_hash and (len(supplied_hash) != 64 or any(ch not in "0123456789abcdef" for ch in supplied_hash)):
            raise EvaluationError("input_hash must be a lowercase SHA-256 hex digest")
        computed_hash = _sha256(_thaw(frozen_input)) if frozen_input else supplied_hash
        if supplied_hash and frozen_input and supplied_hash != computed_hash:
            raise EvaluationError("input_hash does not match input")
        if not frozen_input and not (self.input_ref or computed_hash):
            raise EvaluationError("case requires input, input_ref, or input_hash")
        if self.expected_terminal_state is None and self.expected_action is None:
            raise EvaluationError("case requires an expected terminal state or action")

        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "input", frozen_input)
        object.__setattr__(self, "hard_constraints", constraints)
        object.__setattr__(self, "strata", frozen_strata)
        object.__setattr__(self, "input_ref", str(self.input_ref or "").strip() or None)
        object.__setattr__(self, "input_hash", computed_hash or None)

    def audit_manifest(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "input_ref": self.input_ref,
            "input_hash": self.input_hash,
            "expected_terminal_state": self.expected_terminal_state,
            "expected_action": self.expected_action,
            "hard_constraints": list(self.hard_constraints),
            "strata": dict(self.strata),
        }


@dataclass(frozen=True)
class EvaluationDataset:
    dataset_id: str
    version: str
    cases: tuple[EvaluationCase, ...]

    def __post_init__(self) -> None:
        cases = tuple(self.cases)
        if not str(self.dataset_id or "").strip() or not str(self.version or "").strip():
            raise EvaluationError("dataset_id and version are required")
        if not cases:
            raise EvaluationError("dataset must contain at least one case")
        ids = [case.case_id for case in cases]
        if len(ids) != len(set(ids)):
            raise EvaluationError("dataset contains duplicate case_id")
        object.__setattr__(self, "dataset_id", str(self.dataset_id).strip())
        object.__setattr__(self, "version", str(self.version).strip())
        object.__setattr__(self, "cases", cases)

    @property
    def digest(self) -> str:
        return _sha256(self.manifest())

    def manifest(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "cases": [case.audit_manifest() for case in self.cases],
        }


@dataclass(frozen=True)
class AdapterResult:
    terminal_state: str
    action: str
    success: bool
    latency_ms: float
    manual_review: Optional[bool] = None
    hard_violations: Mapping[str, int] = field(default_factory=dict)
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        violations = {name: 0 for name in HARD_CONSTRAINTS}
        for name, count in dict(self.hard_violations).items():
            if name not in violations:
                raise EvaluationError(f"adapter returned unknown hard violation: {name}")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise EvaluationError(f"hard violation count for {name} must be a non-negative integer")
            violations[name] = count
        if not math.isfinite(float(self.latency_ms)) or float(self.latency_ms) < 0:
            raise EvaluationError("latency_ms must be finite and non-negative")
        object.__setattr__(self, "terminal_state", str(self.terminal_state or "").strip())
        object.__setattr__(self, "action", str(self.action or "").strip())
        object.__setattr__(self, "success", bool(self.success))
        object.__setattr__(self, "latency_ms", float(self.latency_ms))
        object.__setattr__(self, "hard_violations", MappingProxyType(violations))

    @classmethod
    def from_value(cls, value: AdapterResult | Mapping[str, Any]) -> AdapterResult:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise EvaluationError("adapter must return AdapterResult or a mapping")
        allowed = {
            "terminal_state",
            "action",
            "success",
            "latency_ms",
            "manual_review",
            "hard_violations",
            "detail",
        }
        unknown = set(value) - allowed
        if unknown:
            raise EvaluationError(
                "unsupported adapter result fields (LLM/self scores are not evaluation gates): "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        return cls(**dict(value))


Adapter = Callable[[EvaluationCase], AdapterResult | Mapping[str, Any]]


@dataclass(frozen=True)
class EvaluationPolicy:
    min_cases: int = 30
    min_stratum_cases: int = 10
    max_accuracy_drop: float = 0.0
    max_manual_rate_increase: float = 0.02
    max_success_rate_drop: float = 0.0
    max_p95_latency_increase_ratio: float = 0.20
    gate_strata: bool = True

    def __post_init__(self) -> None:
        if self.min_cases < 1 or self.min_stratum_cases < 1:
            raise EvaluationError("minimum sample sizes must be positive")
        for name in ("max_accuracy_drop", "max_manual_rate_increase", "max_success_rate_drop"):
            value = float(getattr(self, name))
            if value < 0 or value > 1:
                raise EvaluationError(f"{name} must be between 0 and 1")
        if self.max_p95_latency_increase_ratio < 0:
            raise EvaluationError("max_p95_latency_increase_ratio must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_cases": self.min_cases,
            "min_stratum_cases": self.min_stratum_cases,
            "max_accuracy_drop": self.max_accuracy_drop,
            "max_manual_rate_increase": self.max_manual_rate_increase,
            "max_success_rate_drop": self.max_success_rate_drop,
            "max_p95_latency_increase_ratio": self.max_p95_latency_increase_ratio,
            "gate_strata": self.gate_strata,
        }


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _is_accurate(case: EvaluationCase, result: AdapterResult) -> bool:
    checks: list[bool] = []
    if case.expected_terminal_state is not None:
        checks.append(result.terminal_state == case.expected_terminal_state)
    if case.expected_action is not None:
        checks.append(result.action == case.expected_action)
    return bool(checks) and all(checks)


def _metric_summary(rows: Sequence[tuple[EvaluationCase, AdapterResult]]) -> dict[str, Any]:
    count = len(rows)
    accurate = sum(_is_accurate(case, result) for case, result in rows)
    manual = sum(
        result.manual_review if result.manual_review is not None else result.terminal_state == "manual_review"
        for _, result in rows
    )
    success = sum(result.success for _, result in rows)
    latencies = [result.latency_ms for _, result in rows]
    violations = {
        name: sum(
            result.hard_violations[name]
            for _, result in rows
        )
        for name in HARD_CONSTRAINTS
    }
    denominator = count or 1
    return {
        "count": count,
        "business_accuracy": accurate / denominator,
        "manual_review_rate": manual / denominator,
        "success_rate": success / denominator,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
        "hard_violations": violations,
    }


def _delta(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "business_accuracy": candidate["business_accuracy"] - baseline["business_accuracy"],
        "manual_review_rate": candidate["manual_review_rate"] - baseline["manual_review_rate"],
        "success_rate": candidate["success_rate"] - baseline["success_rate"],
        "latency_ms": {
            name: candidate["latency_ms"][name] - baseline["latency_ms"][name]
            for name in ("p50", "p95", "p99")
        },
    }


class EvaluationRunner:
    """Deterministic paired evaluator for workflow/skill releases.

    Adapters run offline against the exact same ordered immutable cases.  The
    release decision is derived only from structured outcomes and configured
    thresholds; model-written scores or explanations are intentionally rejected.
    """

    def __init__(self, policy: Optional[EvaluationPolicy] = None):
        self.policy = policy or EvaluationPolicy()

    @staticmethod
    def _replay(dataset: EvaluationDataset, adapter: Adapter) -> list[tuple[EvaluationCase, AdapterResult]]:
        rows: list[tuple[EvaluationCase, AdapterResult]] = []
        for case in dataset.cases:
            try:
                result = AdapterResult.from_value(adapter(case))
            except Exception as exc:
                result = AdapterResult(
                    terminal_state="failed",
                    action="adapter_error",
                    success=False,
                    latency_ms=0,
                    manual_review=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            rows.append((case, result))
        return rows

    @staticmethod
    def _stratum_keys(cases: Iterable[EvaluationCase]) -> list[tuple[str, str]]:
        return sorted({(key, value) for case in cases for key, value in case.strata.items()})

    def _threshold_reasons(
        self,
        baseline: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        scope: str,
    ) -> list[str]:
        reasons: list[str] = []
        if candidate["business_accuracy"] < baseline["business_accuracy"] - self.policy.max_accuracy_drop:
            reasons.append(f"{scope}:business_accuracy_regressed")
        if candidate["manual_review_rate"] > baseline["manual_review_rate"] + self.policy.max_manual_rate_increase:
            reasons.append(f"{scope}:manual_review_rate_regressed")
        if candidate["success_rate"] < baseline["success_rate"] - self.policy.max_success_rate_drop:
            reasons.append(f"{scope}:success_rate_regressed")
        baseline_p95 = baseline["latency_ms"]["p95"]
        candidate_p95 = candidate["latency_ms"]["p95"]
        allowed_p95 = baseline_p95 * (1 + self.policy.max_p95_latency_increase_ratio)
        if candidate_p95 > allowed_p95 and candidate_p95 > baseline_p95:
            reasons.append(f"{scope}:p95_latency_regressed")
        return reasons

    def evaluate(
        self,
        dataset: EvaluationDataset,
        baseline_adapter: Adapter,
        candidate_adapter: Adapter,
        *,
        baseline_version: str,
        candidate_version: str,
        output_path: Optional[Path] = None,
    ) -> dict[str, Any]:
        baseline_rows = self._replay(dataset, baseline_adapter)
        candidate_rows = self._replay(dataset, candidate_adapter)
        baseline = _metric_summary(baseline_rows)
        candidate = _metric_summary(candidate_rows)
        reasons: list[str] = []

        if len(dataset.cases) < self.policy.min_cases:
            reasons.append("overall:minimum_sample_not_met")
        for name, count in candidate["hard_violations"].items():
            if count:
                reasons.append(f"overall:hard_gate:{name}")
        reasons.extend(self._threshold_reasons(baseline, candidate, scope="overall"))

        strata: dict[str, Any] = {}
        for key, value in self._stratum_keys(dataset.cases):
            baseline_subset = [(case, result) for case, result in baseline_rows if case.strata.get(key) == value]
            candidate_subset = [(case, result) for case, result in candidate_rows if case.strata.get(key) == value]
            baseline_summary = _metric_summary(baseline_subset)
            candidate_summary = _metric_summary(candidate_subset)
            eligible = len(candidate_subset) >= self.policy.min_stratum_cases
            scope = f"stratum:{key}={value}"
            stratum_reasons: list[str] = []
            if self.policy.gate_strata and eligible:
                for name, count in candidate_summary["hard_violations"].items():
                    if count:
                        stratum_reasons.append(f"{scope}:hard_gate:{name}")
                stratum_reasons.extend(self._threshold_reasons(baseline_summary, candidate_summary, scope=scope))
                reasons.extend(stratum_reasons)
            strata[scope] = {
                "eligible_for_gate": eligible,
                "baseline": baseline_summary,
                "candidate": candidate_summary,
                "paired_delta": _delta(baseline_summary, candidate_summary),
                "reasons": stratum_reasons,
            }

        paired_cases = []
        for (case, baseline_result), (_, candidate_result) in zip(baseline_rows, candidate_rows):
            paired_cases.append(
                {
                    "case_id": case.case_id,
                    "input_ref": case.input_ref,
                    "input_hash": case.input_hash,
                    "strata": dict(case.strata),
                    "baseline": self._result_for_audit(case, baseline_result),
                    "candidate": self._result_for_audit(case, candidate_result),
                }
            )

        unique_reasons = list(dict.fromkeys(reasons))
        artifact: dict[str, Any] = {
            "schema_version": 1,
            "evaluation_id": f"eval-{uuid.uuid4().hex}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset": {
                "dataset_id": dataset.dataset_id,
                "version": dataset.version,
                "digest": dataset.digest,
                "case_count": len(dataset.cases),
                "manifest": dataset.manifest(),
            },
            "versions": {
                "baseline": str(baseline_version),
                "candidate": str(candidate_version),
            },
            "policy": self.policy.as_dict(),
            "decision": "promote" if not unique_reasons else "reject",
            "reasons": unique_reasons,
            "baseline": baseline,
            "candidate": candidate,
            "paired_delta": _delta(baseline, candidate),
            "strata": strata,
            "paired_cases": paired_cases,
            "gate_authority": "deterministic_metrics_only",
        }
        artifact["audit_digest"] = _sha256(artifact)
        if output_path is not None:
            self.write_artifact(Path(output_path), artifact)
        return artifact

    @staticmethod
    def _result_for_audit(case: EvaluationCase, result: AdapterResult) -> dict[str, Any]:
        return {
            "terminal_state": result.terminal_state,
            "action": result.action,
            "success": result.success,
            "manual_review": result.manual_review
            if result.manual_review is not None
            else result.terminal_state == "manual_review",
            "business_accurate": _is_accurate(case, result),
            "latency_ms": result.latency_ms,
            "hard_violations": dict(result.hard_violations),
            "detail": result.detail,
        }

    @staticmethod
    def write_artifact(path: Path, artifact: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(dict(artifact), handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def verify_artifact(artifact: Mapping[str, Any]) -> bool:
        payload = dict(artifact)
        expected = str(payload.pop("audit_digest", ""))
        return bool(expected) and expected == _sha256(payload)


__all__ = [
    "Adapter",
    "AdapterResult",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationError",
    "EvaluationPolicy",
    "EvaluationRunner",
    "HARD_CONSTRAINTS",
]
