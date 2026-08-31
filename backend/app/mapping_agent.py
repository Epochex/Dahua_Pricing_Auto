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
    ):
        if max_tool_calls < 1 or max_tool_calls > 20:
            raise ValueError("max_tool_calls must be between 1 and 20")
        if time_budget_seconds <= 0 or time_budget_seconds > 300:
            raise ValueError("time_budget_seconds must be in (0, 300]")
        self.store = store
        self.tools = tools
        self.max_tool_calls = int(max_tool_calls)
        self.time_budget_seconds = float(time_budget_seconds)

    @staticmethod
    def _validate_action(action: Mapping[str, Any]) -> str:
        action_type = str(action.get("type") or "").strip().lower()
        if action_type not in {"tool", "complete"}:
            raise InvalidAgentAction("action type must be tool or complete")
        return action_type

    def _complete_budget_exhausted(self, case_id: str, revision: int) -> Dict[str, Any]:
        return self.store.complete_agent_result(
            case_id,
            candidate_category=None,
            recommended_action="retain_current_hold",
            stop_reason="tool_budget_exhausted",
            evidence_refs=[],
            counter_evidence_refs=[],
            unresolved_codes=["tool_budget_exhausted"],
            expected_revision=revision,
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

        for step in range(1, self.max_tool_calls + 2):
            elapsed = time.monotonic() - started
            if elapsed >= self.time_budget_seconds:
                return self._complete_budget_exhausted(case_id, revision)

            action = dict(planner(json.loads(json.dumps(context, ensure_ascii=False))))
            action_type = self._validate_action(action)
            if action_type == "complete":
                evidence_refs = list(action.get("evidence_refs") or [])
                counter_refs = list(action.get("counter_evidence_refs") or [])
                unresolved = list(action.get("unresolved_codes") or [])
                return self.store.complete_agent_result(
                    case_id,
                    candidate_category=action.get("candidate_category"),
                    recommended_action=str(action.get("recommended_action") or "retain_current_hold"),
                    stop_reason=str(action.get("stop_reason") or "planner_completed"),
                    evidence_refs=evidence_refs,
                    counter_evidence_refs=counter_refs,
                    unresolved_codes=unresolved,
                    expected_revision=revision,
                )

            if step > self.max_tool_calls:
                return self._complete_budget_exhausted(case_id, revision)

            tool_name = str(action.get("tool_name") or "").strip()
            arguments = action.get("arguments") or {}
            if not isinstance(arguments, Mapping):
                raise InvalidAgentAction("tool arguments must be an object")
            input_hash = _digest({"tool": tool_name, "arguments": dict(arguments)})
            idempotency_key = f"{case_id}:{step}:{input_hash[:20]}"
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

        return self._complete_budget_exhausted(case_id, revision)
