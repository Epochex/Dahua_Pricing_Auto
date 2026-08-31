from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence


BENCHMARK_STRATA = {
    "normal_baseline",
    "historical_bad_case",
    "challenge_conflict",
    "unseen_variant",
    "should_refuse",
    "resource_budget",
}
VERIFICATION_STATUSES = {"PASS", "WARN", "BLOCK"}
TRIAGE_ACTIONS = {"confirm_correct", "correct_mapping", "investigate"}


class MappingBenchmarkError(ValueError):
    pass


def _tuple_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MappingBenchmarkError(f"{field} must be a list")
    rows = tuple(str(item or "").strip() for item in value)
    if any(not item for item in rows):
        raise MappingBenchmarkError(f"{field} contains an empty value")
    return rows


@dataclass(frozen=True)
class MappingBenchmarkCase:
    case_id: str
    stratum: str
    split: str
    input_ref: str
    input_hash: str
    expected_verification_status: str
    expected_signal_codes: tuple[str, ...]
    expected_triage_action: Optional[str]
    expected_agent_action: Optional[str]
    expected_candidate_category: Optional[str]
    candidate_must_be_null: bool
    must_not_auto_route: bool
    allowed_evidence_refs: tuple[str, ...]
    max_tool_calls: int
    max_latency_ms: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MappingBenchmarkCase":
        unknown = set(raw) - {
            "case_id",
            "stratum",
            "split",
            "input_ref",
            "input_hash",
            "expected_verification_status",
            "expected_signal_codes",
            "expected_triage_action",
            "expected_agent_action",
            "expected_candidate_category",
            "candidate_must_be_null",
            "must_not_auto_route",
            "allowed_evidence_refs",
            "max_tool_calls",
            "max_latency_ms",
        }
        if unknown:
            raise MappingBenchmarkError(f"unsupported case fields: {sorted(unknown)}")
        case_id = str(raw.get("case_id") or "").strip()
        stratum = str(raw.get("stratum") or "").strip()
        split = str(raw.get("split") or "").strip()
        input_ref = str(raw.get("input_ref") or "").strip()
        input_hash = str(raw.get("input_hash") or "").removeprefix("sha256:").strip().lower()
        status = str(raw.get("expected_verification_status") or "").strip().upper()
        triage = str(raw.get("expected_triage_action") or "").strip().lower() or None
        if not case_id or not input_ref or split not in {"baseline", "regression", "holdout", "shadow"}:
            raise MappingBenchmarkError("case_id, input_ref, and a valid split are required")
        if stratum not in BENCHMARK_STRATA:
            raise MappingBenchmarkError(f"unsupported stratum: {stratum}")
        if len(input_hash) != 64 or any(ch not in "0123456789abcdef" for ch in input_hash):
            raise MappingBenchmarkError("input_hash must be a sha256 digest")
        if status not in VERIFICATION_STATUSES:
            raise MappingBenchmarkError("invalid expected verification status")
        if triage is not None and triage not in TRIAGE_ACTIONS:
            raise MappingBenchmarkError("invalid expected triage action")
        max_tool_calls = int(raw.get("max_tool_calls") or 0)
        max_latency_ms = float(raw.get("max_latency_ms") or 0)
        if max_tool_calls < 0 or max_tool_calls > 20 or max_latency_ms <= 0:
            raise MappingBenchmarkError("invalid tool or latency budget")
        return cls(
            case_id=case_id,
            stratum=stratum,
            split=split,
            input_ref=input_ref,
            input_hash=input_hash,
            expected_verification_status=status,
            expected_signal_codes=_tuple_strings(
                raw.get("expected_signal_codes"), field="expected_signal_codes"
            ),
            expected_triage_action=triage,
            expected_agent_action=str(raw.get("expected_agent_action") or "").strip() or None,
            expected_candidate_category=(
                str(raw.get("expected_candidate_category") or "").strip().upper() or None
            ),
            candidate_must_be_null=bool(raw.get("candidate_must_be_null", False)),
            must_not_auto_route=bool(raw.get("must_not_auto_route", False)),
            allowed_evidence_refs=_tuple_strings(
                raw.get("allowed_evidence_refs"), field="allowed_evidence_refs"
            ),
            max_tool_calls=max_tool_calls,
            max_latency_ms=max_latency_ms,
        )


@dataclass(frozen=True)
class MappingBenchmarkObservation:
    case_id: str
    verification_status: str
    signal_codes: tuple[str, ...]
    triage_action: Optional[str]
    agent_action: Optional[str]
    candidate_category: Optional[str]
    auto_routed: bool
    evidence_refs: tuple[str, ...]
    tool_calls: int
    latency_ms: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MappingBenchmarkObservation":
        status = str(raw.get("verification_status") or "").strip().upper()
        if status not in VERIFICATION_STATUSES:
            raise MappingBenchmarkError("observation has invalid verification status")
        return cls(
            case_id=str(raw.get("case_id") or "").strip(),
            verification_status=status,
            signal_codes=_tuple_strings(raw.get("signal_codes"), field="signal_codes"),
            triage_action=str(raw.get("triage_action") or "").strip().lower() or None,
            agent_action=str(raw.get("agent_action") or "").strip() or None,
            candidate_category=str(raw.get("candidate_category") or "").strip().upper() or None,
            auto_routed=bool(raw.get("auto_routed", False)),
            evidence_refs=_tuple_strings(raw.get("evidence_refs"), field="evidence_refs"),
            tool_calls=int(raw.get("tool_calls") or 0),
            latency_ms=float(raw.get("latency_ms") or 0),
        )


def load_mapping_benchmark(path: Path) -> list[MappingBenchmarkCase]:
    rows: list[MappingBenchmarkCase] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MappingBenchmarkError(f"invalid JSON on line {line_number}") from exc
        if not isinstance(raw, dict):
            raise MappingBenchmarkError(f"line {line_number} must contain an object")
        case = MappingBenchmarkCase.from_mapping(raw)
        if case.case_id in seen:
            raise MappingBenchmarkError(f"duplicate case_id: {case.case_id}")
        seen.add(case.case_id)
        rows.append(case)
    if not rows:
        raise MappingBenchmarkError("benchmark is empty")
    return rows


class MappingBenchmarkEvaluator:
    def __init__(
        self,
        *,
        minimum_case_pass_rate: float = 0.9,
        minimum_required_signal_recall: float = 0.95,
    ):
        if not 0 <= minimum_case_pass_rate <= 1:
            raise ValueError("minimum_case_pass_rate must be in [0, 1]")
        if not 0 <= minimum_required_signal_recall <= 1:
            raise ValueError("minimum_required_signal_recall must be in [0, 1]")
        self.minimum_case_pass_rate = float(minimum_case_pass_rate)
        self.minimum_required_signal_recall = float(minimum_required_signal_recall)

    @staticmethod
    def _mean(rows: Sequence[bool]) -> float:
        return sum(1 for item in rows if item) / len(rows) if rows else 1.0

    def evaluate(
        self,
        cases: Sequence[MappingBenchmarkCase],
        observations: Iterable[MappingBenchmarkObservation],
    ) -> Dict[str, Any]:
        observation_rows = list(observations)
        observed = {item.case_id: item for item in observation_rows}
        if len(observed) != len(observation_rows):
            raise MappingBenchmarkError("duplicate observation case_id")
        expected_ids = {item.case_id for item in cases}
        if set(observed) != expected_ids:
            missing = sorted(expected_ids - set(observed))
            extra = sorted(set(observed) - expected_ids)
            raise MappingBenchmarkError(f"observation coverage mismatch; missing={missing}, extra={extra}")

        rows: list[Dict[str, Any]] = []
        total_required_signals = 0
        matched_required_signals = 0
        for case in cases:
            obs = observed[case.case_id]
            required = set(case.expected_signal_codes)
            found = set(obs.signal_codes)
            total_required_signals += len(required)
            matched_required_signals += len(required & found)
            checks = {
                "verification_status": obs.verification_status == case.expected_verification_status,
                "required_signals": required.issubset(found),
                "triage_action": obs.triage_action == case.expected_triage_action,
                "agent_action": (
                    case.expected_agent_action is None or obs.agent_action == case.expected_agent_action
                ),
                "candidate_category": (
                    obs.candidate_category is None
                    if case.candidate_must_be_null
                    else (
                        case.expected_candidate_category is None
                        or obs.candidate_category == case.expected_candidate_category
                    )
                ),
            }
            unsupported_evidence = sorted(set(obs.evidence_refs) - set(case.allowed_evidence_refs))
            unsafe_auto_route = case.must_not_auto_route and obs.auto_routed
            budget_violation = obs.tool_calls > case.max_tool_calls
            latency_violation = obs.latency_ms > case.max_latency_ms
            case_passed = (
                all(checks.values())
                and not unsupported_evidence
                and not unsafe_auto_route
                and not budget_violation
                and not latency_violation
            )
            rows.append(
                {
                    "case_id": case.case_id,
                    "stratum": case.stratum,
                    "split": case.split,
                    "passed": case_passed,
                    "checks": checks,
                    "unsupported_evidence_refs": unsupported_evidence,
                    "unsafe_auto_route": unsafe_auto_route,
                    "budget_violation": budget_violation,
                    "latency_violation": latency_violation,
                    "tool_calls": obs.tool_calls,
                    "latency_ms": obs.latency_ms,
                }
            )

        pass_rate = self._mean([bool(item["passed"]) for item in rows])
        signal_recall = (
            matched_required_signals / total_required_signals if total_required_signals else 1.0
        )
        by_stratum: Dict[str, Dict[str, Any]] = {}
        for stratum in sorted({item.stratum for item in cases}):
            selected = [row for row in rows if row["stratum"] == stratum]
            by_stratum[stratum] = {
                "case_count": len(selected),
                "pass_rate": self._mean([bool(item["passed"]) for item in selected]),
            }

        unsafe_count = sum(1 for item in rows if item["unsafe_auto_route"])
        unsupported_count = sum(len(item["unsupported_evidence_refs"]) for item in rows)
        budget_count = sum(1 for item in rows if item["budget_violation"])
        latency_count = sum(1 for item in rows if item["latency_violation"])
        gates = {
            "zero_unsafe_auto_routes": unsafe_count == 0,
            "zero_unsupported_evidence": unsupported_count == 0,
            "zero_tool_budget_violations": budget_count == 0,
            "zero_latency_budget_violations": latency_count == 0,
            "minimum_case_pass_rate": pass_rate >= self.minimum_case_pass_rate,
            "minimum_required_signal_recall": signal_recall >= self.minimum_required_signal_recall,
        }
        return {
            "schema_version": 1,
            "case_count": len(cases),
            "case_pass_rate": pass_rate,
            "required_signal_recall": signal_recall,
            "unsafe_auto_route_count": unsafe_count,
            "unsupported_evidence_count": unsupported_count,
            "tool_budget_violation_count": budget_count,
            "latency_budget_violation_count": latency_count,
            "by_stratum": by_stratum,
            "gates": gates,
            "eligible_for_release": all(gates.values()),
            "cases": rows,
        }
