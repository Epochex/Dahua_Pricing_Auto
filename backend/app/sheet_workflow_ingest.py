from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from .pricing_workflow import PricingWorkflowCreateReq

try:  # Linux production lock; tests still use the in-process lock.
    import fcntl
except ImportError:  # pragma: no cover - backend is deployed on Linux
    fcntl = None  # type: ignore[assignment]


_PLACEHOLDER_PNS = {
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "待补充",
    "待确认",
    "未知",
}

_BUSINESS_FIELDS = (
    "requester",
    "description",
    "product_line",
    "internal_model",
    "price_level",
    "customer_name",
    "deadline",
    "owner",
)


@dataclass(frozen=True)
class SheetWorkflowDecision:
    """One auditable decision for one parsed spreadsheet row.

    ``create`` only means that the caller may hand ``request`` to
    :meth:`PricingWorkflowStore.create`.  This module never calls the pricing
    engine, the Windows agent, GSP, Google APIs, or a notification channel.
    """

    locator: str
    spreadsheet_id: str
    sheet: str
    row_index: int
    business_hash: str
    action: str
    reason: str
    request: Optional[PricingWorkflowCreateReq] = None


@dataclass(frozen=True)
class SheetWorkflowIngestPlan:
    decisions: List[SheetWorkflowDecision]

    @property
    def create_requests(self) -> List[PricingWorkflowCreateReq]:
        return [item.request for item in self.decisions if item.request is not None]

    @property
    def observed_rows(self) -> Dict[str, str]:
        """Return locator -> business hash for explicit caller-side persistence.

        The planner deliberately does not persist this mapping.  In particular,
        a caller must not acknowledge a ``create`` decision until durable
        ``PricingWorkflowStore.create`` has returned.  Replaying the same
        request before acknowledgement is safe because its idempotency key is
        deterministic.
        """

        return {item.locator: item.business_hash for item in self.decisions}


class SheetWorkflowIngestCoordinator:
    """Persist the safe row baseline and hand approved candidates to a store.

    The callback is expected to be idempotent (``PricingWorkflowStore.create``
    is).  A row is acknowledged only after that callback returns successfully.
    Invalid or changed rows remain unacknowledged so correcting them cannot
    silently bypass review.
    """

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(".lock")
        self._lock = threading.RLock()

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.lock_path.open("a+b") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def process(
        self,
        parsed: Mapping[str, Any],
        *,
        spreadsheet_id: str,
        allow_new_rows: bool,
        create_workflow: Any,
    ) -> Dict[str, Any]:
        spreadsheet_hash = hashlib.sha256(_clean_text(spreadsheet_id).encode("utf-8")).hexdigest()
        with self._locked_state():
            state = self._read_state()
            bootstrapped = bool(state.get("bootstrapped"))
            accepted_hash = _clean_text(state.get("spreadsheet_id_hash"))
            if bootstrapped and accepted_hash and accepted_hash != spreadsheet_hash:
                return {
                    "ok": False,
                    "blocked": "spreadsheet_identity_changed",
                    "counts": {},
                    "created": [],
                    "failed": [],
                    "decisions": [],
                }
            previous_rows = state.get("rows") if bootstrapped else None
            if not isinstance(previous_rows, dict) and bootstrapped:
                previous_rows = {}
            plan = plan_sheet_workflow_ingest(
                parsed,
                spreadsheet_id=spreadsheet_id,
                previous_rows=previous_rows,
                allow_new_rows=allow_new_rows,
            )
            if not bootstrapped:
                state = {
                    "schema_version": 1,
                    "bootstrapped": True,
                    "spreadsheet_id_hash": spreadsheet_hash,
                    "rows": plan.observed_rows,
                }
                self._write_state(state)
                return self._result(plan, created=[], failed=[])

        created: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        acknowledged: Dict[str, str] = {}
        for decision in plan.decisions:
            if decision.action != "create" or decision.request is None:
                continue
            try:
                task = create_workflow(decision.request)
                task_id = _clean_text(task.get("task_id")) if isinstance(task, Mapping) else ""
                created.append(
                    {
                        "locator": decision.locator,
                        "sheet": decision.sheet,
                        "row_index": decision.row_index,
                        "task_id": task_id,
                        "state": _clean_text(task.get("state")) if isinstance(task, Mapping) else "",
                    }
                )
                acknowledged[decision.locator] = decision.business_hash
            except Exception as exc:
                failed.append(
                    {
                        "locator": decision.locator,
                        "sheet": decision.sheet,
                        "row_index": decision.row_index,
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )

        if acknowledged:
            with self._locked_state():
                latest = self._read_state()
                rows = latest.get("rows") if isinstance(latest.get("rows"), dict) else {}
                rows.update(acknowledged)
                latest["schema_version"] = 1
                latest["bootstrapped"] = True
                latest["rows"] = rows
                self._write_state(latest)
        return self._result(plan, created=created, failed=failed)

    def backfill(
        self,
        parsed: Mapping[str, Any],
        *,
        spreadsheet_id: str,
        expected_manifest_hash: Optional[str],
        apply: bool,
        limit: int,
        create_workflow: Any,
    ) -> Dict[str, Any]:
        """Preview or idempotently materialize existing safe pending rows.

        Normal ingest never acts on an initial snapshot.  Backfill is the
        explicit two-step escape hatch: callers first obtain a manifest hash,
        then submit that exact hash to create pricing-only/manual-review tasks.
        Generated requests still carry no GSP payload, notification intent, or
        submission authorization.
        """

        plan = plan_sheet_workflow_backfill(parsed, spreadsheet_id=spreadsheet_id, limit=limit)
        manifest_hash = sheet_backfill_manifest_hash(plan)
        candidates = [item for item in plan.decisions if item.request is not None]
        preview = {
            "ok": True,
            "apply": bool(apply),
            "manifest_hash": manifest_hash,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "locator": item.locator,
                    "sheet": item.sheet,
                    "row_index": item.row_index,
                    "business_hash": item.business_hash,
                    "reason": item.reason,
                }
                for item in candidates
            ],
            "submission_authorized": False,
            "notifications_allowed": False,
            "windows_agent_called": False,
        }
        if not apply:
            return preview
        if not expected_manifest_hash or expected_manifest_hash != manifest_hash:
            return {
                **preview,
                "ok": False,
                "blocked": "manifest_hash_mismatch",
                "created": [],
                "failed": [],
            }

        created: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for decision in candidates:
            try:
                task = create_workflow(decision.request)
                created.append(
                    {
                        "locator": decision.locator,
                        "task_id": _clean_text(task.get("task_id")) if isinstance(task, Mapping) else "",
                        "state": _clean_text(task.get("state")) if isinstance(task, Mapping) else "",
                    }
                )
            except Exception as exc:
                failed.append(
                    {
                        "locator": decision.locator,
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )
        return {**preview, "ok": not failed, "created": created, "failed": failed}

    def _read_state(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, value: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def _result(
        plan: SheetWorkflowIngestPlan,
        *,
        created: List[Dict[str, Any]],
        failed: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        decisions: List[Dict[str, Any]] = []
        for item in plan.decisions:
            counts[item.action] = counts.get(item.action, 0) + 1
            decisions.append(
                {
                    "locator": item.locator,
                    "sheet": item.sheet,
                    "row_index": item.row_index,
                    "action": item.action,
                    "reason": item.reason,
                }
            )
        return {
            "ok": not failed,
            "counts": counts,
            "created": created,
            "failed": failed,
            "decisions": decisions,
        }


def plan_sheet_workflow_ingest(
    parsed: Mapping[str, Any],
    *,
    spreadsheet_id: str,
    previous_rows: Optional[Mapping[str, str]] = None,
    allow_new_rows: bool = False,
) -> SheetWorkflowIngestPlan:
    """Plan safe pricing-only workflow creation from parsed sheet tasks.

    Safety defaults are intentional:

    * ``previous_rows=None`` is a bootstrap snapshot and creates nothing;
    * ``allow_new_rows`` must be explicitly enabled after bootstrapping;
    * a known row whose business fields changed is quarantined for review;
    * completed, blocked/decision, already-running, PLA-bearing, or PN-less
      rows never produce a request;
    * generated requests always set ``submission_authorized=False`` and contain
      no GSP submission payload.

    ``previous_rows`` is the previously accepted locator -> business hash
    mapping.  Persist/acknowledge a new candidate only after the workflow store
    durably accepts its deterministic request.
    """

    spreadsheet = _clean_text(spreadsheet_id)
    if not spreadsheet:
        raise ValueError("spreadsheet_id is required")
    if not isinstance(parsed, Mapping):
        raise TypeError("parsed must be a mapping")

    tasks = parsed.get("tasks") or []
    if not isinstance(tasks, list):
        raise TypeError("parsed.tasks must be a list")

    bootstrap = previous_rows is None
    known = dict(previous_rows or {})
    decisions: List[SheetWorkflowDecision] = []
    seen_locators: set[str] = set()

    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        sheet = _clean_text(task.get("sheet"))
        row_index = _positive_int(task.get("row_index"))
        if not sheet or row_index is None:
            # Without an immutable-enough row address we cannot safely dedupe.
            continue

        locator = sheet_row_locator(spreadsheet, sheet, row_index)
        if locator in seen_locators:
            # A malformed parsed payload must not create two candidates for the
            # same physical row.  Keep the first decision deterministically.
            continue
        seen_locators.add(locator)

        pns = normalize_pns(task.get("pn"))
        business_hash = sheet_row_business_hash(task, pns=pns)
        previous_hash = _clean_text(known.get(locator))

        if previous_hash and previous_hash != business_hash:
            decisions.append(
                _decision(
                    spreadsheet,
                    sheet,
                    row_index,
                    locator,
                    business_hash,
                    "manual_review",
                    "row_business_fields_changed",
                )
            )
            continue

        if previous_hash == business_hash:
            decisions.append(
                _decision(
                    spreadsheet,
                    sheet,
                    row_index,
                    locator,
                    business_hash,
                    "ignore",
                    "row_already_seen",
                )
            )
            continue

        rejection = _new_row_rejection_reason(task, pns)
        if rejection:
            decisions.append(
                _decision(
                    spreadsheet,
                    sheet,
                    row_index,
                    locator,
                    business_hash,
                    "ignore",
                    rejection,
                )
            )
            continue

        if bootstrap:
            decisions.append(
                _decision(
                    spreadsheet,
                    sheet,
                    row_index,
                    locator,
                    business_hash,
                    "baseline",
                    "initial_snapshot_never_auto_creates",
                )
            )
            continue

        if not allow_new_rows:
            decisions.append(
                _decision(
                    spreadsheet,
                    sheet,
                    row_index,
                    locator,
                    business_hash,
                    "ignore",
                    "automatic_creation_not_enabled",
                )
            )
            continue

        idempotency_key = sheet_row_idempotency_key(
            spreadsheet,
            sheet,
            row_index,
            business_hash,
        )
        request = PricingWorkflowCreateReq(
            pns=pns,
            source="google_sheet_row",
            notify=False,
            idempotency_key=idempotency_key,
            gsp_payload={},
            submission_authorized=False,
        )
        decisions.append(
            SheetWorkflowDecision(
                locator=locator,
                spreadsheet_id=spreadsheet,
                sheet=sheet,
                row_index=row_index,
                business_hash=business_hash,
                action="create",
                reason="valid_new_row",
                request=request,
            )
        )

    return SheetWorkflowIngestPlan(decisions=decisions)


def plan_sheet_workflow_backfill(
    parsed: Mapping[str, Any],
    *,
    spreadsheet_id: str,
    limit: int = 500,
) -> SheetWorkflowIngestPlan:
    """Select a bounded, deterministic set of safe rows from an existing snapshot."""

    bounded = max(1, min(int(limit), 500))
    regular = plan_sheet_workflow_ingest(
        parsed,
        spreadsheet_id=spreadsheet_id,
        previous_rows={},
        allow_new_rows=True,
    )
    selected: List[SheetWorkflowDecision] = []
    for decision in regular.decisions:
        if decision.action != "create" or decision.request is None:
            continue
        request_data = (
            decision.request.model_copy(update={"source": "sheet_backfill", "notify": False, "submission_authorized": False, "gsp_payload": {}})
            if hasattr(decision.request, "model_copy")
            else decision.request.copy(update={"source": "sheet_backfill", "notify": False, "submission_authorized": False, "gsp_payload": {}})
        )
        selected.append(
            SheetWorkflowDecision(
                locator=decision.locator,
                spreadsheet_id=decision.spreadsheet_id,
                sheet=decision.sheet,
                row_index=decision.row_index,
                business_hash=decision.business_hash,
                action="create",
                reason="explicit_safe_backfill",
                request=request_data,
            )
        )
        if len(selected) >= bounded:
            break
    return SheetWorkflowIngestPlan(decisions=selected)


def sheet_backfill_manifest_hash(plan: SheetWorkflowIngestPlan) -> str:
    manifest = [
        {
            "locator": item.locator,
            "business_hash": item.business_hash,
            "idempotency_key": item.request.idempotency_key if item.request is not None else None,
        }
        for item in plan.decisions
    ]
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_pns(value: Any) -> List[str]:
    """Normalize one PN cell without guessing slash- or space-delimited PNs."""

    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    result: List[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in re.split(r"[\n\r,，;；]+", _clean_text(raw)):
            pn = part.strip()
            key = pn.casefold()
            if not pn or key in _PLACEHOLDER_PNS or key in seen:
                continue
            seen.add(key)
            result.append(pn)
    return result


def sheet_row_locator(spreadsheet_id: str, sheet: str, row_index: int) -> str:
    """Return a stable, non-secret key for the physical sheet row."""

    spreadsheet_digest = hashlib.sha256(_clean_text(spreadsheet_id).encode("utf-8")).hexdigest()[:16]
    sheet_digest = hashlib.sha256(_clean_text(sheet).encode("utf-8")).hexdigest()[:16]
    return f"gsheet-row-v1:{spreadsheet_digest}:{sheet_digest}:r{int(row_index)}"


def sheet_row_business_hash(task: Mapping[str, Any], *, pns: Optional[List[str]] = None) -> str:
    """Hash fields whose change could alter pricing or submission semantics."""

    normalized = {
        "pns": [pn.casefold() for pn in (pns if pns is not None else normalize_pns(task.get("pn")))],
        **{field: _canonical_text(task.get(field)) for field in _BUSINESS_FIELDS},
    }
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sheet_row_idempotency_key(
    spreadsheet_id: str,
    sheet: str,
    row_index: int,
    business_hash: str,
) -> str:
    """Bind workflow identity to spreadsheet, sheet, row, and business values."""

    spreadsheet_digest = hashlib.sha256(_clean_text(spreadsheet_id).encode("utf-8")).hexdigest()[:16]
    sheet_digest = hashlib.sha256(_clean_text(sheet).encode("utf-8")).hexdigest()[:16]
    return (
        f"gsheet-workflow-v1:{spreadsheet_digest}:{sheet_digest}:"
        f"r{int(row_index)}:{_clean_text(business_hash)}"
    )


def _new_row_rejection_reason(task: Mapping[str, Any], pns: List[str]) -> str:
    if not pns:
        return "pn_missing_or_placeholder"
    if task.get("done_checked") is True:
        return "row_completed"
    if _clean_text(task.get("pla_no")) or task.get("pla_numbers"):
        return "pla_already_exists"

    status = _clean_text(task.get("normalized_status")).casefold()
    combined = " ".join(
        _clean_text(task.get(field)).casefold() for field in ("status", "stage", "note")
    )

    # Negative forms must be evaluated before the generic Chinese substring
    # "完成", which is also present in "未完成".
    pending_markers = ("未完成", "待处理", "待定价", "pending", "new", "新建")
    if any(marker in combined for marker in pending_markers):
        status = "pending"

    blocked_markers = (
        "待决策",
        "待确认",
        "待补充",
        "阻塞",
        "异常",
        "无法",
        "manual_review",
        "manual review",
        "blocked",
        "error",
    )
    if status == "blocked" or any(marker in combined for marker in blocked_markers):
        return "row_requires_decision_or_review"

    completed_markers = ("已完成", "已结束", "closed", "done", "terminé", "termine")
    if status == "completed" or any(marker in combined for marker in completed_markers):
        return "row_completed"

    running_markers = (
        "处理中",
        "进行中",
        "已接收",
        "pricing",
        "validating",
        "submitting",
        "verifying",
        "under_approval",
        "running",
        "progress",
    )
    if status == "running" or any(marker in combined for marker in running_markers):
        return "row_already_started"

    if status not in {"", "pending"}:
        return "row_status_not_safe_for_automatic_creation"
    return ""


def _decision(
    spreadsheet_id: str,
    sheet: str,
    row_index: int,
    locator: str,
    business_hash: str,
    action: str,
    reason: str,
) -> SheetWorkflowDecision:
    return SheetWorkflowDecision(
        locator=locator,
        spreadsheet_id=spreadsheet_id,
        sheet=sheet,
        row_index=row_index,
        business_hash=business_hash,
        action=action,
        reason=reason,
    )


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean_text(value)).casefold()
