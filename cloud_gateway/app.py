from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException

from .message_parser import MessageParseError, parse_pricing_event
from .sheets import GoogleSheetsTaskWriter, SheetAppendError, SheetTaskWriter
from .workflow_dispatch import (
    GoogleWorkflowTaskDispatcher,
    WorkflowDispatchError,
    WorkflowTaskDispatcher,
)


@dataclass(frozen=True)
class GatewaySettings:
    ingress_token: str
    spreadsheet_id: str
    sheet_name: str = "2026.07"
    workflow_execution_url: str = ""

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        return cls(
            ingress_token=os.getenv("HUACHAT_INGRESS_TOKEN", "").strip(),
            spreadsheet_id=os.getenv("GOOGLE_SPREADSHEET_ID", "").strip(),
            sheet_name=os.getenv("GOOGLE_SHEET_NAME", "2026.07").strip() or "2026.07",
            workflow_execution_url=os.getenv("WORKFLOW_EXECUTION_URL", "").strip(),
        )


def create_app(
    *,
    settings: Optional[GatewaySettings] = None,
    writer: Optional[SheetTaskWriter] = None,
    dispatcher: Optional[WorkflowTaskDispatcher] = None,
) -> FastAPI:
    cfg = settings or GatewaySettings.from_env()
    app = FastAPI(title="Pricing Chat-to-Sheet Gateway", version="0.1.0")

    def get_writer() -> SheetTaskWriter:
        nonlocal writer
        if writer is None:
            if not cfg.spreadsheet_id:
                raise HTTPException(status_code=503, detail="GOOGLE_SPREADSHEET_ID is not configured")
            writer = GoogleSheetsTaskWriter(spreadsheet_id=cfg.spreadsheet_id, sheet_name=cfg.sheet_name)
        return writer

    def get_dispatcher() -> WorkflowTaskDispatcher:
        nonlocal dispatcher
        if dispatcher is None:
            if not cfg.workflow_execution_url:
                raise HTTPException(status_code=503, detail="WORKFLOW_EXECUTION_URL is not configured")
            dispatcher = GoogleWorkflowTaskDispatcher(execution_url=cfg.workflow_execution_url)
        return dispatcher

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "ingress_token_configured": bool(cfg.ingress_token),
            "spreadsheet_configured": bool(cfg.spreadsheet_id),
            "sheet_name": cfg.sheet_name,
            "dispatch_mode": "workflow" if (dispatcher is not None or cfg.workflow_execution_url) else "direct_sheet",
        }

    @app.post("/events/huachat")
    def huachat_event(
        payload: Dict[str, Any],
        authorization: Optional[str] = Header(default=None),
        x_huachat_token: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        if not cfg.ingress_token:
            raise HTTPException(status_code=503, detail="HUACHAT_INGRESS_TOKEN is not configured")
        bearer = ""
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()
        supplied = (x_huachat_token or bearer).strip()
        if not supplied or not hmac.compare_digest(supplied, cfg.ingress_token):
            raise HTTPException(status_code=401, detail="invalid ingress token")
        try:
            message = parse_pricing_event(payload)
            row = message.to_sheet_row()
            if dispatcher is not None or cfg.workflow_execution_url:
                execution = get_dispatcher().dispatch(message_id=message.message_id, row=row)
                return {
                    "ok": True,
                    "accepted": True,
                    "queued": True,
                    "message_id": message.message_id,
                    "pn_count": len(message.pns),
                    "execution_name": execution.execution_name,
                    "execution_state": execution.state,
                    "submission_authorized": False,
                }
            result = get_writer().append_if_absent(row)
        except MessageParseError as exc:
            raise HTTPException(status_code=422, detail={"code": exc.code, "message": exc.detail}) from exc
        except SheetAppendError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except WorkflowDispatchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ok": True,
            "accepted": result.appended,
            "duplicate": result.duplicate,
            "message_id": message.message_id,
            "pn_count": len(message.pns),
            "updated_range": result.updated_range,
            "submission_authorized": False,
        }

    return app


app = create_app()
