from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence


_AUTHORITY_WEIGHT = {
    "official_product_document": 5,
    "approved_human_case": 4,
    "product_catalog": 4,
    "price_data": 3,
    "historical_quote": 2,
    "agent_observation": 1,
}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^A-Z0-9]+", str(value or "").upper())
        if len(token) >= 2
    }


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source_type: str
    source_version: str
    authority: str
    subject_ref: str
    family_ref: Optional[str]
    content: str
    attributes: Mapping[str, str]
    approved: bool = False

    def __post_init__(self) -> None:
        for name in ("evidence_id", "source_type", "source_version", "authority", "subject_ref"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        if self.authority not in _AUTHORITY_WEIGHT:
            raise ValueError(f"unsupported authority: {self.authority}")

    @property
    def product_line(self) -> Optional[str]:
        return _optional_text(self.attributes.get("product_line"))

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["attributes"] = dict(self.attributes)
        return value


@dataclass(frozen=True)
class EvidenceHit:
    record: EvidenceRecord
    retrieval_method: str
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "retrieval_method": self.retrieval_method,
            "score": self.score,
        }


SemanticSearch = Callable[[str, int], Sequence[EvidenceHit]]


class TieredEvidenceRetriever:
    """Exact, family, lexical, then optional semantic evidence retrieval."""

    def __init__(
        self,
        records: Iterable[EvidenceRecord],
        *,
        semantic_search: Optional[SemanticSearch] = None,
    ):
        self.records = tuple(records)
        self.semantic_search = semantic_search

    @staticmethod
    def _dedupe(hits: Iterable[EvidenceHit], *, limit: int) -> list[EvidenceHit]:
        selected: Dict[str, EvidenceHit] = {}
        for hit in hits:
            existing = selected.get(hit.record.evidence_id)
            if existing is None or hit.score > existing.score:
                selected[hit.record.evidence_id] = hit
        rows = list(selected.values())
        rows.sort(
            key=lambda item: (
                item.score,
                _AUTHORITY_WEIGHT[item.record.authority],
                item.record.approved,
            ),
            reverse=True,
        )
        return rows[:limit]

    def retrieve(
        self,
        *,
        subject_ref: str,
        family_ref: Optional[str],
        query: str,
        source_version: Optional[str],
        limit: int = 12,
        semantic_limit: int = 4,
    ) -> list[EvidenceHit]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if semantic_limit < 0 or semantic_limit > limit:
            raise ValueError("semantic_limit must be between 0 and limit")

        allowed = [
            item
            for item in self.records
            if source_version is None or item.source_version == source_version
        ]
        hits: list[EvidenceHit] = []
        for item in allowed:
            authority = _AUTHORITY_WEIGHT[item.authority]
            if item.subject_ref == subject_ref:
                hits.append(EvidenceHit(item, "exact_subject", 100.0 + authority))
                continue
            if family_ref and item.family_ref == family_ref:
                hits.append(EvidenceHit(item, "exact_family", 70.0 + authority))

        query_tokens = _tokens(query)
        if query_tokens:
            for item in allowed:
                document_tokens = _tokens(
                    " ".join(
                        [
                            item.subject_ref,
                            item.family_ref or "",
                            item.content,
                            json.dumps(dict(item.attributes), ensure_ascii=False),
                        ]
                    )
                )
                overlap = len(query_tokens & document_tokens)
                if overlap:
                    lexical_score = 30.0 * overlap / len(query_tokens)
                    lexical_score += _AUTHORITY_WEIGHT[item.authority]
                    hits.append(EvidenceHit(item, "lexical", lexical_score))

        if self.semantic_search is not None and semantic_limit:
            for hit in self.semantic_search(query, semantic_limit):
                if source_version is not None and hit.record.source_version != source_version:
                    continue
                hits.append(
                    EvidenceHit(
                        hit.record,
                        "semantic",
                        min(float(hit.score), 29.0),
                    )
                )
        return self._dedupe(hits, limit=limit)


class EvidenceContextAssembler:
    """Build a bounded evidence bundle while preserving contradictions."""

    def __init__(self, *, max_records: int = 12, max_content_chars: int = 6000):
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if max_content_chars < 500:
            raise ValueError("max_content_chars must be at least 500")
        self.max_records = int(max_records)
        self.max_content_chars = int(max_content_chars)

    def assemble(
        self,
        *,
        case_id: str,
        subject_ref: str,
        selected_category: Optional[str],
        candidate_category: Optional[str],
        anomaly_codes: Sequence[str],
        hits: Sequence[EvidenceHit],
        remaining_tool_budget: int,
    ) -> Dict[str, Any]:
        candidate = str(candidate_category or selected_category or "").strip().upper() or None
        selected_hits = list(hits[: self.max_records])
        content_used = 0
        supporting: list[Dict[str, Any]] = []
        counter: list[Dict[str, Any]] = []
        neutral: list[Dict[str, Any]] = []
        omitted: list[str] = []

        for hit in selected_hits:
            record = hit.record
            content = record.content
            remaining = self.max_content_chars - content_used
            if remaining <= 0:
                omitted.append(record.evidence_id)
                continue
            clipped = content[:remaining]
            content_used += len(clipped)
            item = {
                "evidence_id": record.evidence_id,
                "source_type": record.source_type,
                "source_version": record.source_version,
                "authority": record.authority,
                "approved": record.approved,
                "product_line": record.product_line,
                "content": clipped,
                "attributes": dict(record.attributes),
                "retrieval_method": hit.retrieval_method,
                "retrieval_score": hit.score,
            }
            product_line = str(record.product_line or "").strip().upper() or None
            if candidate and product_line == candidate:
                supporting.append(item)
            elif candidate and product_line and product_line != candidate:
                counter.append(item)
            else:
                neutral.append(item)

        missing_evidence: list[str] = []
        if not supporting:
            missing_evidence.append("supporting_product_line_evidence")
        if not any(item["authority"] == "official_product_document" for item in supporting + counter):
            missing_evidence.append("official_product_document")

        return {
            "case_id": str(case_id),
            "subject_ref": str(subject_ref),
            "selected_category": _optional_text(selected_category),
            "candidate_category": candidate,
            "anomaly_codes": [str(item) for item in anomaly_codes],
            "supporting_evidence": supporting,
            "counter_evidence": counter,
            "neutral_evidence": neutral,
            "missing_evidence": missing_evidence,
            "omitted_evidence_refs": omitted,
            "remaining_tool_budget": max(0, int(remaining_tool_budget)),
            "context_limits": {
                "max_records": self.max_records,
                "max_content_chars": self.max_content_chars,
                "content_chars_used": content_used,
            },
        }
