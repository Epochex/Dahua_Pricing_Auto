from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Protocol


class WorkflowDispatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkflowDispatchResult:
    execution_name: str
    state: str


class WorkflowTaskDispatcher(Protocol):
    def dispatch(self, *, message_id: str, row: list[str]) -> WorkflowDispatchResult: ...


class GoogleWorkflowTaskDispatcher:
    """Start one Google Workflows execution for a validated task row."""

    def __init__(self, *, execution_url: str, session: Optional[Any] = None) -> None:
        self.execution_url = str(execution_url or "").strip().rstrip("/")
        self.session = session or self._authorized_session()
        if not self.execution_url.endswith("/executions"):
            raise ValueError("execution_url must be a Workflow Executions collection URL")

    @staticmethod
    def _authorized_session() -> Any:
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:  # pragma: no cover - covered by Cloud Run image
            raise RuntimeError("google-auth is required for Google Workflows access") from exc
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(credentials)

    def dispatch(self, *, message_id: str, row: list[str]) -> WorkflowDispatchResult:
        if len(row) != 14:
            raise ValueError("workflow task row must contain exactly 14 values (A:N)")
        response = self.session.post(
            self.execution_url,
            json={
                "argument": json.dumps(
                    {"message_id": message_id, "row": row},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "executionHistoryLevel": "EXECUTION_HISTORY_BASIC",
            },
            timeout=20,
        )
        if int(getattr(response, "status_code", 0)) // 100 != 2:
            raise WorkflowDispatchError(
                f"Google Workflows dispatch failed with HTTP {getattr(response, 'status_code', 'unknown')}"
            )
        payload = response.json() if hasattr(response, "json") else {}
        execution_name = str(payload.get("name") or "") if isinstance(payload, dict) else ""
        if not execution_name:
            raise WorkflowDispatchError("Google Workflows response did not include an execution name")
        return WorkflowDispatchResult(
            execution_name=execution_name,
            state=str(payload.get("state") or "ACTIVE"),
        )
