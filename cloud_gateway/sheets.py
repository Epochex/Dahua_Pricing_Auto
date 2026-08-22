from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import quote


class SheetAppendError(RuntimeError):
    pass


@dataclass(frozen=True)
class SheetAppendResult:
    appended: bool
    duplicate: bool
    updated_range: str = ""


class SheetTaskWriter(Protocol):
    def append_if_absent(self, row: list[str]) -> SheetAppendResult: ...


class GoogleSheetsTaskWriter:
    """Append sandbox tasks with a bounded message-id duplicate check.

    Cloud Run should use a single instance and concurrency=1 for this sandbox.
    A production multi-instance deployment should replace this check with a
    transactional idempotency store such as Firestore.
    """

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        sheet_name: str,
        session: Optional[Any] = None,
        duplicate_scan_last_row: int = 1000,
    ) -> None:
        self.spreadsheet_id = str(spreadsheet_id or "").strip()
        self.sheet_name = str(sheet_name or "").strip()
        self.session = session or self._authorized_session()
        self.duplicate_scan_last_row = max(5, min(int(duplicate_scan_last_row), 50000))
        if not self.spreadsheet_id:
            raise ValueError("spreadsheet_id is required")
        if not self.sheet_name:
            raise ValueError("sheet_name is required")

    @staticmethod
    def _authorized_session() -> Any:
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:  # pragma: no cover - covered by Cloud Run image
            raise RuntimeError("google-auth is required for Google Sheets access") from exc
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return AuthorizedSession(credentials)

    def append_if_absent(self, row: list[str]) -> SheetAppendResult:
        if len(row) != 14:
            raise ValueError("sheet task row must contain exactly 14 values (A:N)")
        event_note = str(row[13] or "").strip()
        marker_start = event_note.find("[HUACHAT:")
        marker_end = event_note.find("]", marker_start + 9)
        marker = event_note[marker_start : marker_end + 1] if marker_start >= 0 and marker_end > marker_start else ""
        if not marker:
            raise ValueError("HuaChat event marker is required in column N")
        if self._message_id_exists(marker):
            return SheetAppendResult(appended=False, duplicate=True)

        encoded_range = quote(f"'{self.sheet_name}'!A:N", safe="")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            f"/values/{encoded_range}:append"
            "?valueInputOption=RAW&insertDataOption=INSERT_ROWS&includeValuesInResponse=false"
        )
        response = self.session.post(
            url,
            json={"range": f"'{self.sheet_name}'!A:N", "majorDimension": "ROWS", "values": [row]},
            timeout=20,
        )
        if int(getattr(response, "status_code", 0)) // 100 != 2:
            raise SheetAppendError(f"Google Sheets append failed with HTTP {getattr(response, 'status_code', 'unknown')}")
        payload = response.json() if hasattr(response, "json") else {}
        updates = payload.get("updates") if isinstance(payload, dict) else {}
        updated_range = str((updates or {}).get("updatedRange") or "")
        return SheetAppendResult(appended=True, duplicate=False, updated_range=updated_range)

    def _message_id_exists(self, marker: str) -> bool:
        encoded_range = quote(f"'{self.sheet_name}'!N5:N{self.duplicate_scan_last_row}", safe="")
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            f"/values/{encoded_range}?majorDimension=COLUMNS&valueRenderOption=UNFORMATTED_VALUE"
        )
        response = self.session.get(url, timeout=20)
        if int(getattr(response, "status_code", 0)) // 100 != 2:
            raise SheetAppendError(f"Google Sheets duplicate check failed with HTTP {getattr(response, 'status_code', 'unknown')}")
        payload = response.json() if hasattr(response, "json") else {}
        values = payload.get("values") if isinstance(payload, dict) else []
        note_column = values[0] if isinstance(values, list) and values and isinstance(values[0], list) else []
        return any(marker in str(value) for value in note_column)
