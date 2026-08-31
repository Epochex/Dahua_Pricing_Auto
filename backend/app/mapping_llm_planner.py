from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from backend.app.mapping_agent import InvalidAgentAction, MappingAgentError


_SYSTEM_PROMPT = """You plan one bounded product-line mapping investigation.
Return one JSON object only. Allowed actions:
1. {"type":"tool","tool_name":"...","arguments":{...}}
2. {"type":"complete","candidate_category":string|null,
   "recommended_action":"use_candidate_for_current_request"|"retain_current_hold",
   "stop_reason":"stable_code","evidence_refs":[],
   "counter_evidence_refs":[],"unresolved_codes":[]}
Use only a listed read-only tool. Cite evidence references returned by tools.
Keep the current request on hold when evidence is absent or materially conflicting.
Never propose publishing a rule or writing a price change."""


class OpenAICompatibleMappingPlanner:
    """Strict JSON planner adapter for an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: Optional[str] = None,
        timeout_seconds: float = 30.0,
        max_output_tokens: int = 800,
        allow_http: bool = False,
    ):
        parsed = urllib.parse.urlparse(str(endpoint or "").strip())
        if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise ValueError("planner endpoint must use HTTPS")
        if not parsed.netloc:
            raise ValueError("planner endpoint must include a host")
        if not str(model or "").strip():
            raise ValueError("model is required")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("timeout_seconds must be in (0, 120]")
        if max_output_tokens < 100 or max_output_tokens > 4000:
            raise ValueError("max_output_tokens must be between 100 and 4000")
        self.endpoint = str(endpoint).strip()
        self.model = str(model).strip()
        self.api_key = str(api_key or "").strip() or None
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_tokens = int(max_output_tokens)

    @staticmethod
    def _validate_response(action: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
        result = dict(action)
        action_type = str(result.get("type") or "").strip().lower()
        if action_type not in {"tool", "complete"}:
            raise InvalidAgentAction("model action type must be tool or complete")
        result["type"] = action_type
        if action_type == "tool":
            allowed = {
                str(item.get("name") or "")
                for item in context.get("available_tools") or []
                if isinstance(item, Mapping) and item.get("read_only") is True
            }
            tool_name = str(result.get("tool_name") or "").strip()
            if tool_name not in allowed:
                raise InvalidAgentAction("model selected an unknown or non-read-only tool")
            if not isinstance(result.get("arguments") or {}, Mapping):
                raise InvalidAgentAction("model tool arguments must be an object")
        else:
            for field in ("evidence_refs", "counter_evidence_refs", "unresolved_codes"):
                if not isinstance(result.get(field) or [], list):
                    raise InvalidAgentAction(f"model {field} must be a list")
        return result

    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, sort_keys=True),
                },
            ],
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2_000_001)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MappingAgentError(f"model planner request failed: {type(exc).__name__}") from exc
        if len(raw) > 2_000_000:
            raise MappingAgentError("model planner response exceeds size limit")
        try:
            body = json.loads(raw.decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            action = json.loads(content) if isinstance(content, str) else content
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise InvalidAgentAction("model planner returned invalid JSON") from exc
        if not isinstance(action, Mapping):
            raise InvalidAgentAction("model planner action must be an object")
        result = self._validate_response(action, context)
        usage = body.get("usage") if isinstance(body, Mapping) else None
        if isinstance(usage, Mapping):
            result["_usage"] = {
                "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                "output_tokens": int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                ),
            }
        return result
