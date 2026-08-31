from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalized(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", _text(value).upper())


def _timestamp(value: Any, *, fallback: float = 0.0) -> datetime:
    raw = _text(value)
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(max(0.0, fallback), tz=timezone.utc)


def _ref(kind: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"historical-{kind}:{digest}"


def _bounded_text(value: Any, *, limit: int = 500) -> str:
    return _text(value)[:limit]


class HistoricalPricingEvidenceIndex:
    """Read-only index over persisted pricing jobs and quote-request snapshots.

    The index is deliberately derived from the existing files in the pricing
    service directory. It does not copy those records into case memory and it
    supports an ``as_of`` boundary so temporal replay cannot retrieve the
    held-out answer or later evidence.
    """

    def __init__(self, runtime_dir: Path):
        self.runtime_dir = Path(runtime_dir)
        self.outputs_dir = self.runtime_dir / "outputs"
        self.sheet_parsed_dir = self.runtime_dir / "agent" / "sheet_parsed"
        self._lock = threading.RLock()
        self._signature: Optional[tuple[int, int, int, int]] = None
        self._pricing_records: tuple[Dict[str, Any], ...] = ()
        self._quote_records: tuple[Dict[str, Any], ...] = ()
        self._load_stats: Dict[str, Any] = {}

    @staticmethod
    def _directory_signature(path: Path, pattern: str) -> tuple[int, int]:
        count = 0
        newest = 0
        if not path.is_dir():
            return count, newest
        for item in path.glob(pattern):
            try:
                stat = item.stat()
            except OSError:
                continue
            count += 1
            newest = max(newest, int(stat.st_mtime_ns))
        return count, newest

    def _current_signature(self) -> tuple[int, int, int, int]:
        output_count, output_newest = self._directory_signature(self.outputs_dir, "*/state.json")
        sheet_count, sheet_newest = self._directory_signature(self.sheet_parsed_dir, "*.json")
        return output_count, output_newest, sheet_count, sheet_newest

    @staticmethod
    def _read_json(path: Path) -> Optional[Mapping[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, Mapping) else None

    def _load_pricing_records(self) -> tuple[list[Dict[str, Any]], int]:
        records: list[Dict[str, Any]] = []
        skipped = 0
        for path in sorted(self.outputs_dir.glob("*/state.json")):
            state = self._read_json(path)
            if state is None:
                skipped += 1
                continue
            report = state.get("report")
            if not isinstance(report, Mapping):
                continue
            occurred = _timestamp(state.get("created_at"), fallback=path.stat().st_mtime)
            job_ref = _ref("job", path.parent.name, state.get("created_at"))
            for index, raw in enumerate(report.get("items") or []):
                if not isinstance(raw, Mapping):
                    continue
                pn = _text(raw.get("pn"))
                if not pn:
                    continue
                evidence_ref = _ref(
                    "pricing-result",
                    path.parent.name,
                    index,
                    pn,
                    raw.get("category"),
                    raw.get("price_group"),
                )
                warnings = [
                    _bounded_text(item, limit=300)
                    for item in list(raw.get("warnings") or [])[:10]
                    if _text(item)
                ]
                records.append(
                    {
                        "evidence_ref": evidence_ref,
                        "job_ref": job_ref,
                        "occurred_at": occurred.isoformat(),
                        "_occurred_at": occurred,
                        "pn": pn,
                        "_pn": _normalized(pn),
                        "internal_model": _bounded_text(raw.get("internal_model")),
                        "_internal_model": _normalized(raw.get("internal_model")),
                        "external_model": _bounded_text(raw.get("external_model")),
                        "status": _bounded_text(raw.get("status"), limit=80).lower(),
                        "category": _bounded_text(raw.get("category"), limit=100).upper(),
                        "price_group": _bounded_text(raw.get("price_group"), limit=100).upper(),
                        "pricing_rule_name": _bounded_text(raw.get("pricing_rule_name"), limit=200),
                        "price_source": _bounded_text(raw.get("price_source"), limit=100),
                        "primary_match_mode": _bounded_text(raw.get("fr_match_mode"), limit=80),
                        "secondary_match_mode": _bounded_text(raw.get("sys_match_mode"), limit=80),
                        "warnings": warnings,
                    }
                )
        records.sort(key=lambda item: (item["_occurred_at"], item["evidence_ref"]), reverse=True)
        return records, skipped

    def _load_quote_records(self) -> tuple[list[Dict[str, Any]], int]:
        # Sheet pushes are snapshots. Keep the latest state of each business row
        # so a repeatedly pushed sheet does not manufacture duplicate evidence.
        latest_by_row: Dict[tuple[str, int], Dict[str, Any]] = {}
        skipped = 0
        for path in sorted(self.sheet_parsed_dir.glob("*.json")):
            payload = self._read_json(path)
            if payload is None:
                skipped += 1
                continue
            observed = _timestamp(payload.get("received_at"), fallback=path.stat().st_mtime)
            push_ref = _ref("sheet-push", payload.get("push_id"), path.name)
            for raw in payload.get("tasks") or []:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    row_index = int(raw.get("row_index"))
                except (TypeError, ValueError):
                    continue
                sheet = _bounded_text(raw.get("sheet"), limit=300)
                key = (sheet, row_index)
                record = {
                    "evidence_ref": _ref("quote-request", sheet, row_index, observed.isoformat()),
                    "push_ref": push_ref,
                    "observed_at": observed.isoformat(),
                    "_observed_at": observed,
                    "pn": _bounded_text(raw.get("pn")),
                    "_pn": _normalized(raw.get("pn")),
                    "internal_model": _bounded_text(raw.get("internal_model")),
                    "_internal_model": _normalized(raw.get("internal_model")),
                    "customer_ref": _ref("customer", raw.get("customer_name"))
                    if _text(raw.get("customer_name"))
                    else None,
                    "price_level": _bounded_text(raw.get("price_level"), limit=100),
                    "recorded_product_line": _bounded_text(raw.get("product_line"), limit=100).upper(),
                    "request_description": _bounded_text(raw.get("description"), limit=800),
                    "request_note": _bounded_text(raw.get("note"), limit=800),
                    "status": _bounded_text(raw.get("normalized_status") or raw.get("status"), limit=100),
                }
                current = latest_by_row.get(key)
                if current is None or record["_observed_at"] >= current["_observed_at"]:
                    latest_by_row[key] = record
        records = sorted(
            latest_by_row.values(),
            key=lambda item: (item["_observed_at"], item["evidence_ref"]),
            reverse=True,
        )
        return records, skipped

    def refresh(self, *, force: bool = False) -> Dict[str, Any]:
        signature = self._current_signature()
        with self._lock:
            if not force and signature == self._signature:
                return dict(self._load_stats)
            pricing, skipped_outputs = self._load_pricing_records()
            quotes, skipped_sheets = self._load_quote_records()
            self._pricing_records = tuple(pricing)
            self._quote_records = tuple(quotes)
            self._signature = signature
            self._load_stats = {
                "pricing_records": len(pricing),
                "quote_records": len(quotes),
                "skipped_output_files": skipped_outputs,
                "skipped_sheet_files": skipped_sheets,
            }
            return dict(self._load_stats)

    @staticmethod
    def _before(value: Optional[str]) -> Optional[datetime]:
        if not _text(value):
            return None
        parsed = _timestamp(value)
        if parsed.timestamp() == 0 and not _text(value).startswith("1970"):
            raise ValueError("as_of must be an ISO-8601 timestamp")
        return parsed

    @staticmethod
    def _public(record: Mapping[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in record.items() if not key.startswith("_")}

    @staticmethod
    def _matches(
        record: Mapping[str, Any],
        *,
        pn: str,
        internal_model: str,
    ) -> bool:
        query_pn = _normalized(pn)
        query_model = _normalized(internal_model)
        return bool(
            (query_pn and record.get("_pn") == query_pn)
            or (query_model and record.get("_internal_model") == query_model)
        )

    def search_pricing_results(
        self,
        *,
        pn: str = "",
        internal_model: str = "",
        as_of: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        if not _normalized(pn) and not _normalized(internal_model):
            raise ValueError("pn or internal_model is required")
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        self.refresh()
        before = self._before(as_of)
        with self._lock:
            selected = [
                item
                for item in self._pricing_records
                if self._matches(item, pn=pn, internal_model=internal_model)
                and (before is None or item["_occurred_at"] < before)
            ][:limit]
        rows = [self._public(item) for item in selected]
        return {
            "summary_code": "pricing_history_found" if rows else "pricing_history_not_found",
            "count": len(rows),
            "entries": rows,
            "evidence_refs": [item["evidence_ref"] for item in rows],
        }

    def search_quote_requests(
        self,
        *,
        pn: str = "",
        internal_model: str = "",
        customer_ref: Optional[str] = None,
        as_of: Optional[str] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        if not _normalized(pn) and not _normalized(internal_model):
            raise ValueError("pn or internal_model is required")
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        self.refresh()
        before = self._before(as_of)
        with self._lock:
            selected = [
                item
                for item in self._quote_records
                if self._matches(item, pn=pn, internal_model=internal_model)
                and (before is None or item["_observed_at"] < before)
            ]
        expected_customer = _text(customer_ref)
        rows = []
        for item in selected[:limit]:
            row = self._public(item)
            if expected_customer:
                row["same_customer"] = row.get("customer_ref") == expected_customer
            rows.append(row)
        return {
            "summary_code": "quote_history_found" if rows else "quote_history_not_found",
            "count": len(rows),
            "entries": rows,
            "evidence_refs": [item["evidence_ref"] for item in rows],
        }

    def compare_prior_classifications(
        self,
        *,
        pn: str = "",
        internal_model: str = "",
        as_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        history = self.search_pricing_results(
            pn=pn,
            internal_model=internal_model,
            as_of=as_of,
            limit=50,
        )
        successful = [
            item
            for item in history["entries"]
            if item.get("status") == "ok" and item.get("category") not in {"", "UNKNOWN"}
        ]
        category_counts = Counter(str(item["category"]) for item in successful)
        price_group_counts = Counter(
            str(item["price_group"]) for item in successful if item.get("price_group")
        )
        not_found_count = sum(item.get("status") == "not_found" for item in history["entries"])
        warning_count = sum(bool(item.get("warnings")) for item in history["entries"])
        ranked = category_counts.most_common()
        consensus = ranked[0][0] if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]) else None
        conflict = len(category_counts) > 1
        return {
            "summary_code": (
                "prior_classification_conflict"
                if conflict
                else "prior_classification_consensus"
                if consensus
                else "prior_classification_absent"
            ),
            "selected_category": consensus,
            "successful_count": len(successful),
            "not_found_count": not_found_count,
            "warning_count": warning_count,
            "category_counts": dict(category_counts),
            "price_group_counts": dict(price_group_counts),
            "signals": ([{"code": "historical_category_conflict", "severity": "WARN"}] if conflict else []),
            "evidence_refs": [item["evidence_ref"] for item in successful],
        }

    def temporal_episodes(self) -> Sequence[Dict[str, Any]]:
        """Return held-out episodes with a strict prior-history boundary."""

        self.refresh()
        by_pn: Dict[str, list[Dict[str, Any]]] = {}
        with self._lock:
            for item in reversed(self._pricing_records):
                if not item.get("_pn"):
                    continue
                by_pn.setdefault(str(item["_pn"]), []).append(item)
        episodes: list[Dict[str, Any]] = []
        for records in by_pn.values():
            for index, target in enumerate(records):
                if target.get("status") != "ok" or target.get("category") in {"", "UNKNOWN"}:
                    continue
                # Duplicate requests inside one batch share a timestamp. They
                # are not prior knowledge and must not create replay episodes.
                prior = [
                    item
                    for item in records[:index]
                    if item["_occurred_at"] < target["_occurred_at"]
                ]
                if not prior:
                    continue
                prior_success = [
                    item
                    for item in prior
                    if item.get("status") == "ok" and item.get("category") not in {"", "UNKNOWN"}
                ]
                if not prior_success:
                    episode_type = "recovered_after_not_found"
                elif len({item["category"] for item in prior_success}) > 1:
                    episode_type = "historical_conflict"
                elif any(item.get("warnings") for item in prior_success):
                    episode_type = "warning_history"
                else:
                    episode_type = "consistent_history"
                episodes.append(
                    {
                        "case_id": _ref("benchmark-case", target["evidence_ref"]),
                        "episode_type": episode_type,
                        "pn": target["pn"],
                        "internal_model": target["internal_model"],
                        "as_of": target["occurred_at"],
                        "expected_category": target["category"],
                        "expected_price_group": target["price_group"],
                        "target_evidence_ref": target["evidence_ref"],
                        "prior_record_count": len(prior),
                    }
                )
        episodes.sort(key=lambda item: (item["as_of"], item["case_id"]))
        return tuple(episodes)
