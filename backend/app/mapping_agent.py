from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from backend.app.mapping_investigation import MappingInvestigationStore


class MappingAgentError(RuntimeError):
    pass


class ToolNotAllowed(MappingAgentError):
    pass


class InvalidAgentAction(MappingAgentError):
    pass


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


ToolHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Planner = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    handler: ToolHandler
    read_only: bool = True
    input_schema: Optional[Mapping[str, Any]] = None


class ToolRegistry:
    """Small typed boundary for custom domain tools.

    The registry deliberately carries no model-provider or MCP dependency.
    The same handlers can later be exposed through HTTP or MCP without
    changing investigation semantics.
    """

    def __init__(self, tools: Sequence[ToolDefinition] = ()):
        self._tools: Dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        name = str(tool.name or "").strip()
        if not name or name in self._tools:
            raise ValueError(f"invalid or duplicate tool name: {name!r}")
        self._tools[name] = tool

    def specs(self) -> list[Dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "read_only": tool.read_only,
                **(
                    {"input_schema": json.loads(json.dumps(dict(tool.input_schema)))}
                    if tool.input_schema is not None
                    else {}
                ),
            }
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]

    def invoke_read_only(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        tool = self._tools.get(str(name))
        if tool is None:
            raise ToolNotAllowed(f"unknown tool: {name}")
        if not tool.read_only:
            raise ToolNotAllowed(f"tool is not read-only: {name}")
        result = tool.handler(dict(arguments))
        if not isinstance(result, Mapping):
            raise MappingAgentError(f"tool {name} returned a non-object result")
        return json.loads(json.dumps(dict(result), ensure_ascii=False))


class MappingInvestigationAgent:
    """Bounded observe/act loop for one human-triaged mapping case."""

    def __init__(
        self,
        *,
        store: MappingInvestigationStore,
        tools: ToolRegistry,
        max_tool_calls: int = 6,
        time_budget_seconds: float = 60.0,
        max_planner_context_chars: int = 12000,
    ):
        if max_tool_calls < 1 or max_tool_calls > 20:
            raise ValueError("max_tool_calls must be between 1 and 20")
        if time_budget_seconds <= 0 or time_budget_seconds > 300:
            raise ValueError("time_budget_seconds must be in (0, 300]")
        if max_planner_context_chars < 2000 or max_planner_context_chars > 100000:
            raise ValueError("max_planner_context_chars must be between 2000 and 100000")
        self.store = store
        self.tools = tools
        self.max_tool_calls = int(max_tool_calls)
        self.time_budget_seconds = float(time_budget_seconds)
        self.max_planner_context_chars = int(max_planner_context_chars)

    @staticmethod
    def _validate_action(action: Mapping[str, Any]) -> str:
        action_type = str(action.get("type") or "").strip().lower()
        if action_type not in {"tool", "complete"}:
            raise InvalidAgentAction("action type must be tool or complete")
        return action_type

    @staticmethod
    def _compact_value(value: Any, *, depth: int = 0) -> Any:
        # Evidence records commonly nest as observations -> result -> hits ->
        # record -> attributes.  Keep that decision-bearing path intact while
        # still rejecting unbounded provider payloads.
        if depth >= 10:
            return "<depth-limited>"
        if isinstance(value, Mapping):
            return {
                str(key): MappingInvestigationAgent._compact_value(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                MappingInvestigationAgent._compact_value(item, depth=depth + 1)
                for item in value[:12]
            ]
        if isinstance(value, str):
            return value[:1200]
        return value

    def _bounded_planner_context(self, context: Mapping[str, Any]) -> Dict[str, Any]:
        compact = self._compact_value(dict(context))
        if not isinstance(compact, dict):  # pragma: no cover
            raise MappingAgentError("planner context must be an object")
        encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(encoded) <= self.max_planner_context_chars:
            return compact
        tool_history: list[str] = []
        observed_signal_codes: list[str] = []
        for observation in compact.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            tool_name = str(observation.get("tool_name") or "")
            if tool_name:
                tool_history.append(tool_name)
            result = dict(observation.get("result") or {})
            verification = dict(result.get("verification") or {})
            for signal in list(result.get("signals") or []) + list(
                verification.get("signals") or []
            ):
                if isinstance(signal, Mapping) and signal.get("code"):
                    observed_signal_codes.append(str(signal["code"]))
            observation["result"] = {
                key: result[key]
                for key in (
                    "summary_code",
                    "selected_category",
                    "evidence_refs",
                    "signals",
                    "entries",
                    "hits",
                    "items",
                    "verification",
                )
                if key in result
            }
        compact["tool_history"] = list(dict.fromkeys(tool_history))
        compact["observed_signal_codes"] = list(dict.fromkeys(observed_signal_codes))
        encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(encoded) > self.max_planner_context_chars:
            compact["context_truncated"] = True
            compact["observations"] = list(compact.get("observations") or [])[-4:]
            encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(encoded) > self.max_planner_context_chars:
            compact["observations"] = list(compact.get("observations") or [])[-2:]
            encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(encoded) > self.max_planner_context_chars:
            # The identity and tool sequence remain available.  Results are
            # reduced to fields that determine a classification decision.
            for observation in compact.get("observations") or []:
                if not isinstance(observation, dict):
                    continue
                result = observation.get("result")
                if not isinstance(result, dict):
                    continue
                decision_result = {
                    key: result[key]
                    for key in ("summary_code", "selected_category")
                    if key in result
                }
                if "evidence_refs" in result:
                    decision_result["evidence_refs"] = [
                        str(item)[:250] for item in list(result.get("evidence_refs") or [])[:3]
                    ]
                if "signals" in result:
                    decision_result["signals"] = [
                        {
                            "code": str(item.get("code") or "")[:120],
                            "severity": str(item.get("severity") or "")[:40],
                        }
                        for item in list(result.get("signals") or [])[:6]
                        if isinstance(item, Mapping)
                    ]
                tool_name = str(observation.get("tool_name") or "")
                if tool_name == "history.search_approved_cases":
                    decision_result["entries"] = [
                        {
                            "entry_id": str(item.get("entry_id") or ""),
                            "resolved_category": str(item.get("resolved_category") or ""),
                        }
                        for item in list(result.get("entries") or [])[:3]
                        if isinstance(item, Mapping)
                    ]
                elif tool_name == "documents.search_product_documents":
                    decision_result["hits"] = [
                        {
                            "record": {
                                "evidence_id": str(record.get("evidence_id") or ""),
                                "authority": str(record.get("authority") or ""),
                                "attributes": {
                                    "product_line": str(
                                        dict(record.get("attributes") or {}).get("product_line") or ""
                                    )
                                },
                            }
                        }
                        for item in list(result.get("hits") or [])[:3]
                        if isinstance(item, Mapping)
                        and isinstance((record := item.get("record")), Mapping)
                    ]
                elif tool_name == "catalog.search_similar_products":
                    decision_result["items"] = [
                        {
                            "product_line": str(item.get("product_line") or ""),
                            "similarity": float(item.get("similarity") or 0.0),
                            "evidence_ref": str(item.get("evidence_ref") or ""),
                        }
                        for item in list(result.get("items") or [])[:3]
                        if isinstance(item, Mapping)
                    ]
                elif tool_name in {
                    "history.search_pricing_results",
                    "history.search_quote_requests",
                }:
                    decision_result["entries"] = [
                        {
                            key: item[key]
                            for key in (
                                "evidence_ref",
                                "occurred_at",
                                "observed_at",
                                "status",
                                "category",
                                "price_group",
                                "recorded_product_line",
                                "price_level",
                                "same_customer",
                                "warnings",
                            )
                            if key in item
                        }
                        for item in list(result.get("entries") or [])[:5]
                        if isinstance(item, Mapping)
                    ]
                observation["result"] = decision_result
            compact["investigation"] = {
                key: value
                for key, value in dict(compact.get("investigation") or {}).items()
                if key
                in {
                    "pn",
                    "subject_ref",
                    "family_ref",
                    "data_version",
                    "document_source_version",
                    "query",
                    "family_categories",
                    "anomaly_codes",
                    "as_of",
                    "customer_ref",
                    "price_level",
                    "request_description",
                    "investigation_goal",
                }
            }
            compact["available_tools"] = [
                {
                    "name": str(item.get("name") or ""),
                    "read_only": bool(item.get("read_only", False)),
                }
                for item in compact.get("available_tools") or []
                if isinstance(item, Mapping)
            ]
            encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
        if len(encoded) > self.max_planner_context_chars:
            compact["observations"] = list(compact.get("observations") or [])[-1:]
            compact["investigation"].pop("query", None)
            compact["investigation"].pop("family_categories", None)
        return compact

    @staticmethod
    def _final_metrics(metrics: Mapping[str, Any], *, started: float) -> Dict[str, Any]:
        return {
            **dict(metrics),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        }

    def _complete_budget_exhausted(
        self,
        case_id: str,
        revision: int,
        *,
        metrics: Mapping[str, Any],
        started: float,
    ) -> Dict[str, Any]:
        return self.store.complete_agent_result(
            case_id,
            candidate_category=None,
            recommended_action="retain_current_hold",
            stop_reason="tool_budget_exhausted",
            evidence_refs=[],
            counter_evidence_refs=[],
            unresolved_codes=["tool_budget_exhausted"],
            expected_revision=revision,
            metrics=self._final_metrics(metrics, started=started),
        )

    def run(
        self,
        case_id: str,
        *,
        planner: Planner,
        initial_context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        case = self.store.get(case_id)
        if case["state"] != "investigating":
            raise MappingAgentError("case must be human-triaged to investigate before agent execution")
        revision = int(case["revision"])
        started = time.monotonic()
        context: Dict[str, Any] = {
            "case_id": case_id,
            "investigation": json.loads(json.dumps(dict(initial_context), ensure_ascii=False)),
            "available_tools": self.tools.specs(),
            "observations": [],
            "remaining_tool_calls": self.max_tool_calls,
            "time_budget_seconds": self.time_budget_seconds,
        }
        metrics: Dict[str, Any] = {
            "planner_calls": 0,
            "tool_calls": 0,
            "planner_context_chars_total": 0,
            "planner_context_chars_max": 0,
            "tool_result_chars_total": 0,
            "provider_input_tokens": 0,
            "provider_output_tokens": 0,
            "duplicate_tool_requests": 0,
            "discarded_evidence_refs": 0,
            "decision_guard_interventions": 0,
        }
        invoked_inputs: set[str] = set()

        for step in range(1, self.max_tool_calls + 2):
            elapsed = time.monotonic() - started
            if elapsed >= self.time_budget_seconds:
                return self._complete_budget_exhausted(
                    case_id,
                    revision,
                    metrics=metrics,
                    started=started,
                )

            planner_context = self._bounded_planner_context(context)
            planner_context_chars = len(
                json.dumps(planner_context, ensure_ascii=False, sort_keys=True)
            )
            metrics["planner_calls"] = int(metrics["planner_calls"]) + 1
            metrics["planner_context_chars_total"] = (
                int(metrics["planner_context_chars_total"]) + planner_context_chars
            )
            metrics["planner_context_chars_max"] = max(
                int(metrics["planner_context_chars_max"]), planner_context_chars
            )
            action = dict(planner(json.loads(json.dumps(planner_context, ensure_ascii=False))))
            usage = action.pop("_usage", None)
            if isinstance(usage, Mapping):
                metrics["provider_input_tokens"] = int(metrics["provider_input_tokens"]) + int(
                    usage.get("input_tokens") or 0
                )
                metrics["provider_output_tokens"] = int(metrics["provider_output_tokens"]) + int(
                    usage.get("output_tokens") or 0
                )
            action_type = self._validate_action(action)
            if action_type == "complete":
                observed_refs = {
                    str(ref)
                    for observation in context["observations"]
                    if isinstance(observation, Mapping)
                    for ref in dict(observation.get("result") or {}).get("evidence_refs") or []
                }
                evidence_tools: Dict[str, set[str]] = {}
                for observation in context["observations"]:
                    if not isinstance(observation, Mapping):
                        continue
                    observed_tool = str(observation.get("tool_name") or "")
                    for ref in dict(observation.get("result") or {}).get("evidence_refs") or []:
                        evidence_tools.setdefault(str(ref), set()).add(observed_tool)
                requested_evidence = [str(item) for item in action.get("evidence_refs") or []]
                requested_counter = [
                    str(item) for item in action.get("counter_evidence_refs") or []
                ]
                evidence_refs = [item for item in requested_evidence if item in observed_refs]
                counter_refs = [item for item in requested_counter if item in observed_refs]
                discarded = (
                    len(requested_evidence)
                    + len(requested_counter)
                    - len(evidence_refs)
                    - len(counter_refs)
                )
                metrics["discarded_evidence_refs"] = int(
                    metrics["discarded_evidence_refs"]
                ) + discarded
                unresolved = list(action.get("unresolved_codes") or [])
                candidate_category = action.get("candidate_category")
                recommended_action = str(
                    action.get("recommended_action") or "retain_current_hold"
                )
                stop_reason = str(action.get("stop_reason") or "planner_completed")
                if candidate_category and not evidence_refs:
                    candidate_category = None
                    recommended_action = "retain_current_hold"
                    stop_reason = "planner_cited_unobserved_evidence"
                    unresolved = list(dict.fromkeys([*unresolved, stop_reason]))
                historical_conflict = any(
                    str(dict(observation.get("result") or {}).get("summary_code") or "")
                    == "prior_classification_conflict"
                    for observation in context["observations"]
                    if isinstance(observation, Mapping)
                )
                independently_supported = any(
                    any(not tool.startswith("history.") for tool in evidence_tools.get(ref, set()))
                    for ref in evidence_refs
                )
                if candidate_category and historical_conflict and not independently_supported:
                    candidate_category = None
                    recommended_action = "retain_current_hold"
                    stop_reason = "historical_conflict_requires_current_corroboration"
                    unresolved = list(dict.fromkeys([*unresolved, stop_reason]))
                    counter_refs = list(dict.fromkeys([*counter_refs, *evidence_refs]))
                    evidence_refs = []
                    metrics["decision_guard_interventions"] = int(
                        metrics["decision_guard_interventions"]
                    ) + 1
                return self.store.complete_agent_result(
                    case_id,
                    candidate_category=candidate_category,
                    recommended_action=recommended_action,
                    stop_reason=stop_reason,
                    evidence_refs=evidence_refs,
                    counter_evidence_refs=counter_refs,
                    unresolved_codes=unresolved,
                    finding_codes=list(action.get("finding_codes") or []),
                    investigation_summary=str(action.get("investigation_summary") or ""),
                    recommended_next_steps=list(action.get("recommended_next_steps") or []),
                    expected_revision=revision,
                    metrics=self._final_metrics(metrics, started=started),
                )

            if step > self.max_tool_calls:
                return self._complete_budget_exhausted(
                    case_id,
                    revision,
                    metrics=metrics,
                    started=started,
                )

            tool_name = str(action.get("tool_name") or "").strip()
            arguments = action.get("arguments") or {}
            if not isinstance(arguments, Mapping):
                raise InvalidAgentAction("tool arguments must be an object")
            input_hash = _digest({"tool": tool_name, "arguments": dict(arguments)})
            idempotency_key = f"{case_id}:{step}:{input_hash[:20]}"
            if input_hash in invoked_inputs:
                result = {
                    "error_type": "DuplicateToolRequest",
                    "summary_code": "duplicate_tool_request",
                    "evidence_refs": [],
                }
                status = "failed"
                summary_code = "duplicate_tool_request"
                metrics["duplicate_tool_requests"] = int(metrics["duplicate_tool_requests"]) + 1
            else:
                invoked_inputs.add(input_hash)
                try:
                    result = self.tools.invoke_read_only(tool_name, arguments)
                    status = "succeeded"
                    summary_code = str(result.get("summary_code") or "tool_succeeded")
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "error_type": type(exc).__name__,
                        "summary_code": "tool_failed",
                        "evidence_refs": [],
                    }
                    status = "failed"
                    summary_code = "tool_failed"
            metrics["tool_calls"] = int(metrics["tool_calls"]) + 1
            metrics["tool_result_chars_total"] = int(metrics["tool_result_chars_total"]) + len(
                json.dumps(result, ensure_ascii=False, sort_keys=True)
            )

            evidence_refs = [str(item) for item in (result.get("evidence_refs") or [])]
            output_refs = evidence_refs or [f"tool-result:sha256:{_digest(result)}"]
            case = self.store.append_checkpoint(
                case_id,
                tool_name=tool_name or "invalid_tool",
                status=status,
                summary_code=summary_code,
                input_refs=[f"tool-input:sha256:{input_hash}"],
                output_refs=output_refs,
                idempotency_key=idempotency_key,
                expected_revision=revision,
                planner_decision={
                    "hypothesis": str(action.get("hypothesis") or "")[:1000],
                    "reason": str(action.get("reason") or "")[:1000],
                    "tool_arguments": dict(arguments),
                },
                observation_summary={
                    key: result[key]
                    for key in (
                        "summary_code",
                        "selected_category",
                        "count",
                        "successful_count",
                        "not_found_count",
                        "warning_count",
                        "category_counts",
                        "price_group_counts",
                        "signals",
                        "evidence_refs",
                    )
                    if key in result
                },
            )
            revision = int(case["revision"])
            context["observations"].append(
                {
                    "step": step,
                    "tool_name": tool_name,
                    "status": status,
                    "result": result,
                }
            )
            context["remaining_tool_calls"] = self.max_tool_calls - step
            context["elapsed_seconds"] = time.monotonic() - started

        return self._complete_budget_exhausted(
            case_id,
            revision,
            metrics=metrics,
            started=started,
        )
