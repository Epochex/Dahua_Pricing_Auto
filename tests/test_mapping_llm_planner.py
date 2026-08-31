from __future__ import annotations

import json
from typing import Any

import pytest

from backend.app.mapping_agent import InvalidAgentAction
from backend.app.mapping_llm_planner import OpenAICompatibleMappingPlanner


class _Response:
    def __init__(self, body: dict[str, Any]):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


def _context() -> dict:
    return {
        "available_tools": [
            {"name": "history.search_approved_cases", "read_only": True},
            {"name": "pricing.write", "read_only": False},
        ]
    }


def test_model_planner_parses_strict_action_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "type": "tool",
                                    "tool_name": "history.search_approved_cases",
                                    "arguments": {"subject_ref": "product:1"},
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 123, "completion_tokens": 17},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    planner = OpenAICompatibleMappingPlanner(
        endpoint="http://127.0.0.1:9000/v1/chat/completions",
        model="mock-model",
        allow_http=True,
    )
    action = planner(_context())

    assert action["tool_name"] == "history.search_approved_cases"
    assert action["_usage"] == {"input_tokens": 123, "output_tokens": 17}
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_model_planner_rejects_write_tool_even_if_model_requests_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"type":"tool","tool_name":"pricing.write","arguments":{}}'
                        }
                    }
                ]
            }
        ),
    )
    planner = OpenAICompatibleMappingPlanner(
        endpoint="http://127.0.0.1:9000/v1/chat/completions",
        model="mock-model",
        allow_http=True,
    )

    with pytest.raises(InvalidAgentAction, match="non-read-only"):
        planner(_context())
