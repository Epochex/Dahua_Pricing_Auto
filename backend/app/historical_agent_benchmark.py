from __future__ import annotations

import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from backend.app.historical_pricing_evidence import HistoricalPricingEvidenceIndex
from backend.app.mapping_agent import MappingInvestigationAgent, Planner, ToolDefinition, ToolRegistry
from backend.app.mapping_investigation import MappingInvestigationStore


RESOLVABLE_EPISODE_TYPES = {"consistent_history", "warning_history"}
UNRESOLVABLE_EPISODE_TYPES = {"historical_conflict", "recovered_after_not_found"}


def _limit(arguments: Mapping[str, Any], default: int = 10) -> int:
    try:
        value = int(arguments.get("limit", default))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if value < 1 or value > 50:
        raise ValueError("limit must be between 1 and 50")
    return value


def build_temporal_history_tools(
    index: HistoricalPricingEvidenceIndex,
    *,
    enforced_identity: Optional[Mapping[str, Any]] = None,
) -> ToolRegistry:
    """Expose history without current price tables for leakage-free replay."""

    identity_schema = {
        "type": "object",
        "properties": {
            "pn": {"type": "string"},
            "internal_model": {"type": "string"},
            "as_of": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "anyOf": [{"required": ["pn"]}, {"required": ["internal_model"]}],
    }

    def identity(arguments: Mapping[str, Any]) -> Dict[str, Any]:
        source = dict(enforced_identity or arguments)
        return {
            "pn": str(source.get("pn") or ""),
            "internal_model": str(source.get("internal_model") or ""),
            "as_of": str(source.get("as_of") or "") or None,
        }

    def pricing_results(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        query = identity(arguments)
        return index.search_pricing_results(
            **query,
            limit=_limit(arguments),
        )

    def quote_requests(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        query = identity(arguments)
        return index.search_quote_requests(
            **query,
            customer_ref=str(arguments.get("customer_ref") or "") or None,
            limit=_limit(arguments),
        )

    def classification_summary(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return index.compare_prior_classifications(**identity(arguments))

    return ToolRegistry(
        [
            ToolDefinition(
                "history.compare_prior_classifications",
                "Aggregate strictly earlier category outcomes, conflicts, warnings and missing results.",
                classification_summary,
                input_schema={
                    **identity_schema,
                    "properties": {
                        key: value
                        for key, value in identity_schema["properties"].items()
                        if key != "limit"
                    },
                },
            ),
            ToolDefinition(
                "history.search_pricing_results",
                "Inspect strictly earlier individual pricing outcomes and warnings.",
                pricing_results,
                input_schema=identity_schema,
            ),
            ToolDefinition(
                "history.search_quote_requests",
                "Inspect earlier request descriptions, price levels and customer-context matches.",
                quote_requests,
                input_schema={
                    **identity_schema,
                    "properties": {
                        **identity_schema["properties"],
                        "customer_ref": {"type": "string"},
                    },
                },
            ),
        ]
    )


def select_temporal_episodes(
    episodes: Sequence[Mapping[str, Any]],
    *,
    per_type: int = 5,
) -> list[Dict[str, Any]]:
    if per_type < 1 or per_type > 100:
        raise ValueError("per_type must be between 1 and 100")
    selected: list[Dict[str, Any]] = []
    seen_products: set[str] = set()
    counts: Counter[str] = Counter()
    # Prefer recent targets while retaining at most one target per product.
    for raw in reversed(list(episodes)):
        item = dict(raw)
        episode_type = str(item.get("episode_type") or "")
        product_key = str(item.get("pn") or "").upper()
        if episode_type not in RESOLVABLE_EPISODE_TYPES | UNRESOLVABLE_EPISODE_TYPES:
            continue
        if counts[episode_type] >= per_type or product_key in seen_products:
            continue
        selected.append(item)
        seen_products.add(product_key)
        counts[episode_type] += 1
    selected.sort(key=lambda item: (str(item["episode_type"]), str(item["case_id"])))
    return selected


def _run_case(
    episode: Mapping[str, Any],
    *,
    planner: Planner,
    index: HistoricalPricingEvidenceIndex,
    work_dir: Path,
    max_tool_calls: int,
    time_budget_seconds: float,
) -> Dict[str, Any]:
    store = MappingInvestigationStore(work_dir, durable_writes=False)
    verification = {
        "status": "WARN",
        "selected_category": "UNKNOWN",
        "signals": [{"code": "historical_evidence_required", "severity": "WARN"}],
    }
    created = store.create(
        subject_ref=f"temporal-replay:{episode['case_id']}",
        data_version="historical-temporal-replay-v1",
        verification=verification,
    )
    investigating = store.triage(
        created["case_id"],
        action="investigate",
        reviewer="benchmark:temporal-label",
        rationale_code="historical_evidence_required",
        expected_revision=int(created["revision"]),
    )
    started = time.monotonic()
    tools = build_temporal_history_tools(
        index,
        enforced_identity={
            "pn": episode["pn"],
            "internal_model": episode.get("internal_model"),
            "as_of": episode["as_of"],
        },
    )
    completed = MappingInvestigationAgent(
        store=store,
        tools=tools,
        max_tool_calls=max_tool_calls,
        time_budget_seconds=time_budget_seconds,
        max_planner_context_chars=16000,
    ).run(
        investigating["case_id"],
        planner=planner,
        initial_context={
            "pn": episode["pn"],
            "internal_model": episode.get("internal_model"),
            "subject_ref": f"temporal-replay:{episode['case_id']}",
            "as_of": episode["as_of"],
            "anomaly_codes": ["historical_evidence_required"],
            "investigation_goal": "resolve_product_line_anomaly_from_strictly_prior_evidence",
        },
    )
    result = dict(completed.get("agent_result") or {})
    sequence = [
        str(item.get("tool_name") or "") for item in completed.get("checkpoints") or []
    ]
    resolvable = str(episode["episode_type"]) in RESOLVABLE_EPISODE_TYPES
    selected_category = str(result.get("candidate_category") or "").upper() or None
    expected_category = str(episode.get("expected_category") or "").upper() or None
    if resolvable:
        passed = (
            selected_category == expected_category
            and result.get("recommended_action") == "use_candidate_for_current_request"
        )
    else:
        passed = (
            selected_category is None
            and result.get("recommended_action") == "retain_current_hold"
        )
    return {
        "case_id": episode["case_id"],
        "episode_type": episode["episode_type"],
        "resolvable_from_prior_history": resolvable,
        "passed": passed,
        "category_correct": selected_category == expected_category if selected_category else False,
        "selected_category": selected_category,
        "expected_category": expected_category,
        "recommended_action": result.get("recommended_action"),
        "stop_reason": result.get("stop_reason"),
        "tool_sequence": sequence,
        "tool_calls": len(sequence),
        "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
        "metrics": result.get("metrics") or {},
    }


def _summarize(cases: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    resolvable = [item for item in cases if item["resolvable_from_prior_history"]]
    unresolved = [item for item in cases if not item["resolvable_from_prior_history"]]
    sequence_counts = Counter(" > ".join(item["tool_sequence"]) or "<no-tool>" for item in cases)
    by_type: Dict[str, Dict[str, Any]] = {}
    for episode_type, rows in _group_by_type(cases).items():
        by_type[episode_type] = {
            "count": len(rows),
            "passed": sum(bool(item["passed"]) for item in rows),
            "pass_rate": sum(bool(item["passed"]) for item in rows) / len(rows),
            "tool_sequences": sorted({tuple(item["tool_sequence"]) for item in rows}),
        }
    return {
        "case_count": len(cases),
        "passed": sum(bool(item["passed"]) for item in cases),
        "pass_rate": sum(bool(item["passed"]) for item in cases) / len(cases) if cases else 0.0,
        "resolvable_category_accuracy": (
            sum(bool(item["category_correct"]) for item in resolvable) / len(resolvable)
            if resolvable
            else 0.0
        ),
        "safe_hold_rate": (
            sum(bool(item["passed"]) for item in unresolved) / len(unresolved)
            if unresolved
            else 0.0
        ),
        "distinct_tool_sequences": len(sequence_counts),
        "tool_sequence_counts": dict(sequence_counts),
        "by_episode_type": by_type,
    }


def _group_by_type(cases: Sequence[Mapping[str, Any]]) -> Dict[str, list[Mapping[str, Any]]]:
    grouped: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in cases:
        grouped[str(item["episode_type"])].append(item)
    return grouped


def run_temporal_history_benchmark(
    *,
    index: HistoricalPricingEvidenceIndex,
    planner: Planner,
    per_type: int = 5,
    max_tool_calls: int = 4,
    time_budget_seconds: float = 60.0,
    work_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    episodes = select_temporal_episodes(index.temporal_episodes(), per_type=per_type)
    if not episodes:
        raise ValueError("no temporal replay episodes found")
    owned_temp: Optional[tempfile.TemporaryDirectory[str]] = None
    if work_dir is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="pricing-agent-temporal-")
        root = Path(owned_temp.name)
    else:
        root = Path(work_dir)
    try:
        cases = [
            _run_case(
                episode,
                planner=planner,
                index=index,
                work_dir=root / "with-history" / str(episode["case_id"]),
                max_tool_calls=max_tool_calls,
                time_budget_seconds=time_budget_seconds,
            )
            for episode in episodes
        ]
    finally:
        if owned_temp is not None:
            owned_temp.cleanup()

    # The no-history baseline has no evidence that can justify a category. Its
    # correct behavior is a hold, which quantifies the coverage supplied by
    # historical retrieval without spending a second set of provider calls.
    without_history = []
    for episode in episodes:
        resolvable = str(episode["episode_type"]) in RESOLVABLE_EPISODE_TYPES
        without_history.append(
            {
                "case_id": episode["case_id"],
                "episode_type": episode["episode_type"],
                "resolvable_from_prior_history": resolvable,
                "passed": not resolvable,
                "category_correct": False,
                "tool_sequence": [],
            }
        )
    return {
        "benchmark": "historical_temporal_replay_v1",
        "selection": {
            "per_type": per_type,
            "episode_counts": dict(Counter(str(item["episode_type"]) for item in episodes)),
            "strict_as_of_boundary": True,
            "one_target_per_product": True,
        },
        "with_history": {**_summarize(cases), "cases": cases},
        "without_history": _summarize(without_history),
        "history_gain": {
            "pass_rate_delta": _summarize(cases)["pass_rate"]
            - _summarize(without_history)["pass_rate"],
            "resolvable_category_accuracy_delta": _summarize(cases)[
                "resolvable_category_accuracy"
            ]
            - _summarize(without_history)["resolvable_category_accuracy"],
        },
    }
