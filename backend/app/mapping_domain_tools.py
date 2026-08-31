from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import pandas as pd

from backend.app.mapping_agent import ToolDefinition, ToolRegistry
from backend.app.mapping_case_memory import MappingCaseMemory
from backend.app.mapping_evidence import EvidenceRecord, TieredEvidenceRetriever
from backend.app.historical_pricing_evidence import HistoricalPricingEvidenceIndex
from backend.engine.core.classifier import classify_category_and_price_group
from backend.engine.core.pricing_engine import resolve_product_rows
from backend.engine.engine import PricingEngine


_PRODUCT_FIELDS = (
    "Part No.",
    "Part Num",
    "PN",
    "Internal Model",
    "External Model",
    "Series",
    "系列",
    "Description",
    "Product Name",
    "Product Name(CN)",
    "First Level Product Category",
    "Second Level Product Category",
    "First Product Line",
    "Second Product Line",
    "Catelog Name",
    "Sales Type",
    "Sales Status",
    "Release Status",
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:  # noqa: BLE001
        pass
    return str(value).strip()


def _pn(arguments: Mapping[str, Any]) -> str:
    value = _text(arguments.get("pn"))
    if not value or len(value) > 500:
        raise ValueError("pn is required and must be at most 500 characters")
    return value


def _limit(arguments: Mapping[str, Any], *, default: int = 10, maximum: int = 20) -> int:
    try:
        value = int(arguments.get("limit", default))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def _evidence_ref(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"mapping-evidence:{digest}"


def _row_payload(row: Optional[pd.Series]) -> Dict[str, str]:
    if row is None:
        return {}
    return {
        field: value
        for field in _PRODUCT_FIELDS
        if (value := _text(row.get(field)))
    }


def _pick_pn_column(df: pd.DataFrame) -> Optional[str]:
    for name in ("Part No.", "Part No", "Part Num", "PN", "P/N", "Part Number", "PartNumber"):
        if name in df.columns:
            return name
    return None


def _normalize_model(value: Any) -> str:
    text = re.sub(r"[^A-Z0-9]+", "", _text(value).upper())
    for prefix in ("DHI", "DH"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text


def _model_values(row: Optional[pd.Series]) -> list[str]:
    if row is None:
        return []
    return [
        value
        for field in ("Internal Model", "External Model")
        if (value := _normalize_model(row.get(field)))
    ]


def _similarity(query_models: Sequence[str], candidate_models: Sequence[str]) -> float:
    best = 0.0
    for query in query_models:
        for candidate in candidate_models:
            if not query or not candidate:
                continue
            ratio = SequenceMatcher(None, query, candidate).ratio()
            prefix = 0
            for left, right in zip(query, candidate):
                if left != right:
                    break
                prefix += 1
            prefix_score = prefix / max(1, min(len(query), len(candidate)))
            contains_score = 0.9 if query in candidate or candidate in query else 0.0
            best = max(best, ratio, prefix_score, contains_score)
    return best


def build_mapping_domain_tools(
    *,
    engine: PricingEngine,
    memory: MappingCaseMemory,
    document_records: Iterable[EvidenceRecord] = (),
    historical_index: Optional[HistoricalPricingEvidenceIndex] = None,
) -> ToolRegistry:
    """Build concrete, read-only tools backed by current pricing data."""

    def get_candidates(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        pn = _pn(arguments)
        family_categories = [str(item) for item in arguments.get("family_categories") or []]
        verification = engine.verify_mapping(pn, family_categories=family_categories)
        rule_refs = [
            f"mapping-rule:{source}:{match['rule_id']}"
            for source, matches in verification.get("matches_by_source", {}).items()
            for match in matches
        ]
        return {
            "summary_code": "mapping_candidates_loaded",
            "verification": verification,
            "evidence_refs": rule_refs,
        }

    def get_product_attributes(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        pn = _pn(arguments)
        data, data_version = engine.snapshot()
        country_row, system_row, resolution = resolve_product_rows(data, pn)
        sources = []
        evidence_refs = []
        for source, row in (("country", country_row), ("system", system_row)):
            payload = _row_payload(row)
            if not payload:
                continue
            ref = _evidence_ref("product-attributes", data_version, source, pn, payload)
            evidence_refs.append(ref)
            sources.append({"source": source, "evidence_ref": ref, "attributes": payload})
        if country_row is None and system_row is None:
            return {
                "summary_code": "product_not_found",
                "data_version": data_version,
                "sources": [],
                "evidence_refs": [],
            }
        category, price_group = classify_category_and_price_group(
            country_row,
            system_row,
            data.map_fr,
            data.map_sys,
        )
        return {
            "summary_code": "product_attributes_loaded",
            "data_version": data_version,
            "selected_category": category,
            "selected_price_group": price_group,
            "row_resolution": resolution,
            "sources": sources,
            "evidence_refs": evidence_refs,
        }

    def compare_sources(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        pn = _pn(arguments)
        verification = engine.verify_mapping(pn)
        matches = verification.get("matches_by_source", {})
        categories = {
            source: sorted({str(item.get("category") or "UNKNOWN") for item in rows})
            for source, rows in matches.items()
        }
        refs = [
            f"mapping-rule:{source}:{item['rule_id']}"
            for source, rows in matches.items()
            for item in rows
        ]
        return {
            "summary_code": (
                "price_sources_conflict"
                if len({category for rows in categories.values() for category in rows}) > 1
                else "price_sources_consistent"
            ),
            "data_version": verification.get("data_version"),
            "categories_by_source": categories,
            "signals": verification.get("signals") or [],
            "evidence_refs": refs,
        }

    def search_similar_products(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        pn = _pn(arguments)
        limit = _limit(arguments)
        data, data_version = engine.snapshot()
        country_row, system_row, _ = resolve_product_rows(data, pn)
        query_models = _model_values(country_row) + _model_values(system_row)
        if not query_models:
            query_models = [_normalize_model(pn)]
        candidates: Dict[str, Dict[str, Any]] = {}
        for source, df in (("country", data.france_df), ("system", data.sys_df)):
            pn_col = _pick_pn_column(df)
            if not pn_col:
                continue
            for index, row in df.iterrows():
                candidate_pn = _text(row.get(pn_col))
                if not candidate_pn or candidate_pn.upper() == pn.upper():
                    continue
                score = _similarity(query_models, _model_values(row))
                if score < 0.55:
                    continue
                ref = _evidence_ref("similar-product", data_version, source, candidate_pn, index)
                existing = candidates.get(candidate_pn)
                item = {
                    "pn": candidate_pn,
                    "source": source,
                    "similarity": round(score, 6),
                    "evidence_ref": ref,
                    "attributes": _row_payload(row),
                }
                if existing is None or item["similarity"] > existing["similarity"]:
                    candidates[candidate_pn] = item
        rows = sorted(candidates.values(), key=lambda item: item["similarity"], reverse=True)[:limit]
        for item in rows:
            candidate_country, candidate_system, _ = resolve_product_rows(data, item["pn"])
            category, price_group = classify_category_and_price_group(
                candidate_country,
                candidate_system,
                data.map_fr,
                data.map_sys,
            )
            item["product_line"] = category
            item["price_group"] = price_group
        return {
            "summary_code": "similar_products_found" if rows else "similar_products_not_found",
            "data_version": data_version,
            "query_models": query_models,
            "items": rows,
            "evidence_refs": [item["evidence_ref"] for item in rows],
        }

    def search_approved_cases(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        subject_ref = _text(arguments.get("subject_ref")) or None
        family_ref = _text(arguments.get("family_ref")) or None
        data_version = _text(arguments.get("data_version")) or None
        if not subject_ref and not family_ref:
            raise ValueError("subject_ref or family_ref is required")
        selected: Dict[str, Dict[str, Any]] = {}
        if subject_ref:
            for item in memory.search(
                subject_ref=subject_ref,
                data_version=data_version,
            )["entries"]:
                selected[str(item["entry_id"])] = item
        if family_ref:
            for item in memory.search(
                family_ref=family_ref,
                data_version=data_version,
            )["entries"]:
                selected[str(item["entry_id"])] = item
        entries = list(selected.values())
        entries.sort(key=lambda item: str(item.get("valid_from") or ""), reverse=True)
        refs = [f"approved-case:{item['entry_id']}" for item in entries]
        return {
            "summary_code": "approved_cases_found" if refs else "approved_cases_not_found",
            "count": len(entries),
            "entries": entries,
            "evidence_refs": refs,
        }

    def search_documents(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        subject_ref = _text(arguments.get("subject_ref"))
        family_ref = _text(arguments.get("family_ref")) or None
        query = _text(arguments.get("query"))
        source_version = _text(arguments.get("source_version")) or None
        limit = _limit(arguments, default=8)
        if not subject_ref or not query:
            raise ValueError("subject_ref and query are required")
        records = tuple(document_records) + tuple(memory.as_evidence_records())
        hits = TieredEvidenceRetriever(records).retrieve(
            subject_ref=subject_ref,
            family_ref=family_ref,
            query=query,
            source_version=source_version,
            limit=limit,
            semantic_limit=0,
        )
        return {
            "summary_code": "document_evidence_found" if hits else "document_evidence_not_found",
            "hits": [item.to_dict() for item in hits],
            "evidence_refs": [item.record.evidence_id for item in hits],
        }

    def search_pricing_results(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if historical_index is None:  # pragma: no cover - registered conditionally
            raise RuntimeError("historical pricing index is unavailable")
        return historical_index.search_pricing_results(
            pn=_text(arguments.get("pn")),
            internal_model=_text(arguments.get("internal_model")),
            as_of=_text(arguments.get("as_of")) or None,
            limit=_limit(arguments, default=10, maximum=50),
        )

    def search_quote_requests(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if historical_index is None:  # pragma: no cover - registered conditionally
            raise RuntimeError("historical pricing index is unavailable")
        return historical_index.search_quote_requests(
            pn=_text(arguments.get("pn")),
            internal_model=_text(arguments.get("internal_model")),
            customer_ref=_text(arguments.get("customer_ref")) or None,
            as_of=_text(arguments.get("as_of")) or None,
            limit=_limit(arguments, default=10, maximum=50),
        )

    def compare_prior_classifications(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if historical_index is None:  # pragma: no cover - registered conditionally
            raise RuntimeError("historical pricing index is unavailable")
        return historical_index.compare_prior_classifications(
            pn=_text(arguments.get("pn")),
            internal_model=_text(arguments.get("internal_model")),
            as_of=_text(arguments.get("as_of")) or None,
        )

    definitions = [
            ToolDefinition(
                name="mapping.get_candidates",
                description="Return all rule matches and independent verification signals for a PN.",
                handler=get_candidates,
            ),
            ToolDefinition(
                name="catalog.get_product_attributes",
                description="Read product attributes from the active price-data release.",
                handler=get_product_attributes,
            ),
            ToolDefinition(
                name="price_data.compare_sources",
                description="Compare product-line evidence across active price-data sources.",
                handler=compare_sources,
            ),
            ToolDefinition(
                name="catalog.search_similar_products",
                description="Find similar models using normalized model strings and attributes.",
                handler=search_similar_products,
            ),
            ToolDefinition(
                name="history.search_approved_cases",
                description="Retrieve resolved mapping cases with explicit scope and receipts.",
                handler=search_approved_cases,
            ),
            ToolDefinition(
                name="documents.search_product_documents",
                description="Retrieve bounded product-document and approved-case evidence.",
                handler=search_documents,
            ),
        ]
    if historical_index is not None:
        identity_schema = {
            "type": "object",
            "properties": {
                "pn": {"type": "string"},
                "internal_model": {"type": "string"},
                "as_of": {"type": "string", "description": "optional exclusive ISO-8601 boundary"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "anyOf": [{"required": ["pn"]}, {"required": ["internal_model"]}],
        }
        definitions.extend(
            [
                ToolDefinition(
                    name="history.search_pricing_results",
                    description=(
                        "Search earlier persisted pricing results for prior classification, "
                        "price-group, warning and failure evidence."
                    ),
                    handler=search_pricing_results,
                    input_schema=identity_schema,
                ),
                ToolDefinition(
                    name="history.search_quote_requests",
                    description=(
                        "Search prior quote-request rows for customer context, requested price "
                        "level, request description and recorded product line."
                    ),
                    handler=search_quote_requests,
                    input_schema={
                        **identity_schema,
                        "properties": {
                            **identity_schema["properties"],
                            "customer_ref": {"type": "string"},
                        },
                    },
                ),
                ToolDefinition(
                    name="history.compare_prior_classifications",
                    description=(
                        "Aggregate earlier outcomes and expose consensus, conflicts, warnings "
                        "and missing-result counts."
                    ),
                    handler=compare_prior_classifications,
                    input_schema={
                        "type": "object",
                        "properties": {
                            key: value
                            for key, value in identity_schema["properties"].items()
                            if key != "limit"
                        },
                        "anyOf": identity_schema["anyOf"],
                    },
                ),
            ]
        )
    return ToolRegistry(definitions)
