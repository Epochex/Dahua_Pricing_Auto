from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


class MessageParseError(ValueError):
    """A chat event cannot be converted into a safe pricing request."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PricingMessage:
    message_id: str
    created_at: str
    requester: str
    description: str
    product_line: str
    internal_model: str
    pns: tuple[str, ...]
    price_level: str
    customer_name: str
    deadline: str

    def to_sheet_row(self) -> list[str]:
        """Return the existing inquiry workbook's A:N business row."""

        return [
            self.requester,
            self.description,
            self.product_line,
            ", ".join(self.pns),
            self.internal_model,
            self.price_level,
            self.customer_name,
            "",
            self.deadline,
            "",
            "",
            "尚未开始",
            "收集需求",
            f"[HUACHAT:{self.message_id}] {self.created_at}",
        ]


_LABELS = {
    "pns": ("pn", "pn码", "part number", "part no"),
    "price_level": ("层级", "价格应用层级", "应用层级", "price level"),
    "customer_name": ("客户", "客户名称", "customer"),
    "product_line": ("产品线", "product line"),
    "internal_model": ("内部型号", "internal model"),
    "deadline": ("截止", "截止日期", "deadline"),
    "description": ("需求", "任务", "描述", "需求描述", "任务描述"),
}

_LABEL_TO_FIELD = {
    label.casefold(): field
    for field, labels in _LABELS.items()
    for label in labels
}

_LABEL_PATTERN = re.compile(
    r"^\s*(" + "|".join(re.escape(label) for label in sorted(_LABEL_TO_FIELD, key=len, reverse=True)) + r")\s*[:：]\s*(.*?)\s*$",
    flags=re.IGNORECASE,
)

_PRICING_MARKERS = ("定价", "询价", "报价", "pricing", "price")
_PLACEHOLDER_PNS = {"", "-", "--", "n/a", "na", "none", "null", "待确认", "待补充"}


def parse_pricing_event(payload: Mapping[str, Any], *, now: Optional[datetime] = None) -> PricingMessage:
    """Parse the sandbox HuaChat envelope into one deterministic task row.

    The accepted envelope is deliberately small and provider-neutral.  The
    production HuaChat adapter should verify the provider signature and map the
    official event object to these fields before calling this function.
    """

    if not isinstance(payload, Mapping):
        raise MessageParseError("invalid_payload", "event payload must be an object")

    message_id = _first_text(payload, "message_id", "messageId", "msgId", "event_id", "eventId")
    if not message_id:
        raise MessageParseError("message_id_missing", "a stable message_id is required for idempotency")
    if len(message_id) > 200:
        raise MessageParseError("message_id_too_long", "message_id exceeds 200 characters")

    text = _event_text(payload)
    if not text:
        raise MessageParseError("text_missing", "message text is empty")
    if len(text) > 8000:
        raise MessageParseError("text_too_long", "message text exceeds 8000 characters")
    if not any(marker in text.casefold() for marker in _PRICING_MARKERS):
        raise MessageParseError("intent_not_pricing", "message does not contain a pricing intent marker")

    fields = _parse_labeled_fields(text)
    pns = _normalize_pns(fields.get("pns", ""))
    if not pns:
        raise MessageParseError("pn_missing", "use an explicit 'PN:' line with at least one part number")

    sender = payload.get("sender")
    requester = _first_text(payload, "sender_name", "senderName", "requester")
    if not requester and isinstance(sender, Mapping):
        requester = _first_text(sender, "name", "display_name", "displayName", "staffName")
    if not requester:
        requester = "unknown"

    created_at = _first_text(payload, "created_at", "createdAt", "timestamp", "event_time", "eventTime")
    if not created_at:
        created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    description = fields.get("description") or _compact_text(text, limit=1000)
    return PricingMessage(
        message_id=message_id,
        created_at=created_at,
        requester=requester[:200],
        description=description[:1000],
        product_line=fields.get("product_line", "")[:200],
        internal_model=fields.get("internal_model", "")[:200],
        pns=tuple(pns),
        price_level=fields.get("price_level", "")[:100],
        customer_name=fields.get("customer_name", "")[:200],
        deadline=fields.get("deadline", "")[:100],
    )


def _event_text(payload: Mapping[str, Any]) -> str:
    direct = _first_text(payload, "text", "content", "message_text", "messageText")
    if direct:
        return direct
    message = payload.get("message")
    if isinstance(message, Mapping):
        return _first_text(message, "text", "content")
    return ""


def _parse_labeled_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in re.split(r"[\r\n]+", text):
        match = _LABEL_PATTERN.match(line)
        if not match:
            continue
        field = _LABEL_TO_FIELD[match.group(1).strip().casefold()]
        value = match.group(2).strip()
        if value and field not in fields:
            fields[field] = value
    return fields


def _normalize_pns(value: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,，;；]+", value):
        pn = item.strip()
        key = pn.casefold()
        if key in _PLACEHOLDER_PNS or key in seen:
            continue
        seen.add(key)
        result.append(pn)
    if len(result) > 500:
        raise MessageParseError("too_many_pns", "at most 500 unique PNs are allowed")
    return result


def _first_text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _compact_text(value: str, *, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]
