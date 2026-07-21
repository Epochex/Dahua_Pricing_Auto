from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .evaluation import EvaluationCase, EvaluationDataset, EvaluationPolicy, EvaluationRunner
from .case_memory import CaseMemory
from .event_store import EventStore, sha256_digest
from .registry import EvolutionRegistry
from .release import ReleaseController


BASELINE_PINS = {
    "workflow:enterprise-pricing": "1.0.0",
    "skill:price-validate": "1.0.0",
    "prompt:pricing-decision": "1.0.0",
    "policy:pricing-safety": "1.0.0",
}


class EvolutionControlPlane:
    """Join observability, versioning, evaluation, and release decisions.

    This component is deliberately control-plane only: it cannot call GSP,
    Windows workers, Google, or a notification provider.  The deployed default
    is shadow-only, and every workflow pins the immutable artifact bundle it
    started with.
    """

    def __init__(self, runtime_dir: Path, *, execution_enabled: bool = False):
        self.runtime_dir = Path(runtime_dir)
        self.evolution_dir = self.runtime_dir / "evolution"
        self.evaluations_dir = self.evolution_dir / "evaluations"
        self.registry = EvolutionRegistry(self.runtime_dir)
        self.events = EventStore(self.runtime_dir)
        self.cases = CaseMemory(self.runtime_dir)
        self.releases = ReleaseController(
            self.evolution_dir / "release-control",
            execution_enabled=execution_enabled,
        )

    def ensure_baseline(self) -> Dict[str, Any]:
        definitions = (
            (
                "workflow",
                "enterprise-pricing",
                {
                    "states": [
                        "pricing", "validating", "submitting", "verifying",
                        "under_approval", "approved", "rejected", "failed", "manual_review",
                    ],
                    "progress_invariant": "new_fact_or_budget_or_terminal",
                    "unknown_submit_result": "verify_before_retry",
                    "external_effects_default": False,
                },
            ),
            (
                "skill",
                "price-validate",
                {
                    "steps": ["normalize_pn", "calculate_price", "validate_result", "route_terminal"],
                    "max_replans_without_new_evidence": 0,
                    "submission_authorized": False,
                },
            ),
            (
                "prompt",
                "pricing-decision",
                {
                    "mode": "structured_reason_code",
                    "allowed_outputs": ["advance", "bounded_retry", "manual_review", "failed"],
                    "llm_is_not_gate_authority": True,
                },
            ),
            (
                "policy",
                "pricing-safety",
                {
                    "hard_gates": {
                        "price_boundary": 0,
                        "illegal_transition": 0,
                        "duplicate_effect": 0,
                    },
                    "notifications_allowed": False,
                    "windows_agent_allowed": False,
                    "four_eyes_for_canary": True,
                },
            ),
        )
        for kind, name, content in definitions:
            self.registry.register(
                kind,
                name,
                "1.0.0",
                content,
                source="built-in-baseline",
                created_by="evolution-control-plane",
                business_constraints=(content.get("hard_gates") if isinstance(content, dict) else {}),
                status="candidate",
            )
        try:
            current = self.registry.resolve_bundle("production")
            if current.get("pins"):
                return current
        except Exception:
            pass
        return self.registry.activate_bundle(
            BASELINE_PINS,
            environment="production",
            actor="evolution-control-plane",
            reason="initial safe baseline",
        )

    def resolve_execution_context(
        self,
        *,
        session_id: Optional[str] = None,
        pins: Optional[Mapping[str, str]] = None,
        environment: str = "production",
    ) -> Dict[str, Any]:
        bundle = self.registry.resolve_bundle(environment, pins=pins)
        clean_session = str(session_id or f"session-{uuid.uuid4().hex}").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", clean_session):
            raise ValueError("session_id must be a non-sensitive stable identifier")
        return {
            "run_id": f"run-{uuid.uuid4().hex}",
            "session_id": clean_session,
            "environment": environment,
            "bundle_hash": bundle["bundle_hash"],
            "pins": bundle["pins"],
        }

    @staticmethod
    def _safe_ref(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        normalized = re.sub(r"[^A-Za-z0-9_.:/-]+", "-", text)[:200].strip("-")
        return normalized or None

    def capture_workflow(self, task: Mapping[str, Any]) -> Dict[str, Any]:
        context = dict(task.get("execution_context") or {})
        pins = dict(context.get("pins") or {})
        task_id = str(task.get("task_id") or "")
        run_id = str(context.get("run_id") or f"run-{task_id}")
        session_id = str(context.get("session_id") or f"session-{task_id}")
        workflow_version = str(pins.get("workflow:enterprise-pricing") or "legacy-1.0.0")
        skill_version = str(pins.get("skill:price-validate") or "legacy-1.0.0")
        prompt_version = str(pins.get("prompt:pricing-decision") or "legacy-1.0.0")
        source_ref = self._safe_ref((task.get("input") or {}).get("source"))
        captured = 0
        replayed = 0
        for trace in task.get("trace") or []:
            if not isinstance(trace, Mapping):
                continue
            metadata = dict(trace.get("metadata") or {})
            receipt_value = {
                key: metadata.get(key)
                for key in ("report_id", "lease_id", "outcome", "pla_no")
                if metadata.get(key) is not None
            }
            result = self.events.append(
                {
                    "event_id": str(trace.get("trace_id") or f"event-{uuid.uuid4().hex}"),
                    "run_id": run_id,
                    "session_id": session_id,
                    "task_id": task_id,
                    "workflow_version": workflow_version,
                    "skill_version": skill_version,
                    "prompt_version": prompt_version,
                    "pre_state": str(trace.get("from_state") or "none"),
                    "action": str(trace.get("event") or "unknown"),
                    "post_state": str(trace.get("to_state") or task.get("state") or "unknown"),
                    "input_hash": sha256_digest(task.get("input") or {}),
                    "evidence_hash": sha256_digest(metadata) if metadata else None,
                    "effect_hash": sha256_digest(task.get("effect_key")) if task.get("effect_key") else None,
                    "receipt_hash": sha256_digest(receipt_value) if receipt_value else None,
                    "evidence_refs": [f"source:{source_ref}"] if source_ref else [],
                    "reason": str(trace.get("event") or "UNKNOWN")[:200],
                    "occurred_at": trace.get("at"),
                }
            )
            if result["idempotent_replay"]:
                replayed += 1
            else:
                captured += 1
        candidate_case = self.cases.observe(task)
        return {
            "ok": True,
            "captured": captured,
            "replayed": replayed,
            "task_id": task_id,
            "candidate_case_id": candidate_case.get("case_id") if candidate_case else None,
        }

    def run_precomputed_evaluation(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        case_values = payload.get("cases") or []
        baseline_results = dict(payload.get("baseline_results") or {})
        candidate_results = dict(payload.get("candidate_results") or {})
        cases = tuple(
            EvaluationCase(
                case_id=str(item["case_id"]),
                input=dict(item.get("input") or {}),
                input_ref=item.get("input_ref"),
                input_hash=item.get("input_hash"),
                expected_terminal_state=item.get("expected_terminal_state"),
                expected_action=item.get("expected_action"),
                hard_constraints=tuple(item.get("hard_constraints") or ("price_boundary", "illegal_transition", "duplicate_effect")),
                strata=dict(item.get("strata") or {}),
            )
            for item in case_values
        )
        dataset = EvaluationDataset(
            dataset_id=str(payload.get("dataset_id") or "pricing-regression"),
            version=str(payload.get("dataset_version") or "1"),
            cases=cases,
        )
        policy = EvaluationPolicy(**dict(payload.get("policy") or {}))
        runner = EvaluationRunner(policy)
        artifact = runner.evaluate(
            dataset,
            lambda case: baseline_results[case.case_id],
            lambda case: candidate_results[case.case_id],
            baseline_version=str(payload.get("baseline_version") or "baseline"),
            candidate_version=str(payload.get("candidate_version") or "candidate"),
        )
        self.evaluations_dir.mkdir(parents=True, exist_ok=True)
        runner.write_artifact(self.evaluations_dir / f"{artifact['evaluation_id']}.json", artifact)
        return artifact

    def read_evaluation(self, evaluation_id: str) -> Dict[str, Any]:
        safe_id = str(evaluation_id or "").strip()
        if not re.fullmatch(r"eval-[0-9a-f]{32}", safe_id):
            raise ValueError("invalid evaluation_id")
        path = self.evaluations_dir / f"{safe_id}.json"
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if not EvaluationRunner.verify_artifact(artifact):
            raise ValueError("evaluation artifact digest mismatch")
        return artifact

    @staticmethod
    def release_summary(artifact: Mapping[str, Any]) -> Dict[str, Any]:
        violations = dict((artifact.get("candidate") or {}).get("hard_violations") or {})
        return {
            "passed": artifact.get("decision") == "promote",
            "sample_count": int((artifact.get("candidate") or {}).get("count") or 0),
            "constraint_violations": int(violations.get("price_boundary") or 0),
            "illegal_transitions": int(violations.get("illegal_transition") or 0),
            "duplicate_effects": int(violations.get("duplicate_effect") or 0),
        }

    def propose_release(
        self,
        *,
        evaluation_id: str,
        candidate_pins: Mapping[str, str],
        baseline_pins: Mapping[str, str],
        environment: str,
        actor: str,
        release_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        artifact = self.read_evaluation(evaluation_id)
        candidate = self.registry.resolve_bundle(environment, pins=candidate_pins, allow_candidate=True)
        baseline = self.registry.resolve_bundle(environment, pins=baseline_pins)
        return self.releases.propose(
            candidate_bundle_id=candidate["bundle_hash"],
            baseline_bundle_id=baseline["bundle_hash"],
            candidate_pins=candidate["pins"],
            baseline_pins=baseline["pins"],
            evaluation_id=evaluation_id,
            evaluation_summary=self.release_summary(artifact),
            environment=environment,
            actor=actor,
            release_key=release_key,
        )

    def promote_release(self, release_id: str, *, actor: str, approved_by: str) -> Dict[str, Any]:
        current = self.releases.get(release_id)
        self.registry.resolve_bundle(
            str(current["environment"]),
            pins=dict(current.get("candidate_pins") or {}),
            allow_candidate=True,
        )
        promoted = self.releases.promote(release_id, actor=actor, approved_by=approved_by)
        try:
            bundle = self.registry.activate_bundle(
                dict(promoted.get("candidate_pins") or {}),
                environment=str(promoted["environment"]),
                actor=actor,
                reason=f"release {release_id} promoted",
            )
        except Exception:
            self.releases.rollback(release_id, reason="registry activation failed", actor="control-plane")
            raise
        return {**promoted, "active_bundle": bundle}

    def record_release_window(
        self,
        release_id: str,
        *,
        window_id: str,
        metrics: Mapping[str, Any],
        actor: str,
    ) -> Dict[str, Any]:
        before = self.releases.get(release_id)
        result = self.releases.record_window(
            release_id,
            window_id=window_id,
            metrics=metrics,
            actor=actor,
        )
        if before.get("phase") == "active" and result.get("phase") == "rolled_back":
            bundle = self.registry.activate_bundle(
                dict(result.get("baseline_pins") or {}),
                environment=str(result["environment"]),
                actor="release-kill-switch",
                reason=f"release {release_id} metric gate rollback",
            )
            return {**result, "active_bundle": bundle}
        return result

    def rollback_release(self, release_id: str, *, reason: str, actor: str) -> Dict[str, Any]:
        current = self.releases.get(release_id)
        bundle = self.registry.activate_bundle(
            dict(current.get("baseline_pins") or {}),
            environment=str(current["environment"]),
            actor=actor,
            reason=f"release {release_id} rollback: {reason}",
        )
        rolled_back = self.releases.rollback(release_id, reason=reason, actor=actor)
        return {**rolled_back, "active_bundle": bundle}
