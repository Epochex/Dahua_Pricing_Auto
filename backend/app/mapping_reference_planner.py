from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Mapping, Optional, Sequence


_DOCUMENT_AUTHORITY_WEIGHT = {
    "official_product_document": 5.0,
    "approved_human_case": 4.0,
    "product_catalog": 3.0,
    "price_data": 2.0,
    "historical_quote": 1.5,
    "agent_observation": 0.5,
}


def _category(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text if text and text != "UNKNOWN" else None


class ReferenceEvidencePlanner:
    """Deterministic benchmark planner for the evidence investigation chain.

    This planner provides a reproducible baseline and an outage fallback.  A
    model planner can be evaluated against exactly the same tool boundary and
    benchmark cases.
    """

    @staticmethod
    def _seen(context: Mapping[str, Any]) -> set[str]:
        return {
            str(item.get("tool_name") or "")
            for item in context.get("observations") or []
            if isinstance(item, Mapping)
        } | {str(item) for item in context.get("tool_history") or []}

    @staticmethod
    def _result(context: Mapping[str, Any], tool_name: str) -> Dict[str, Any]:
        for item in reversed(list(context.get("observations") or [])):
            if not isinstance(item, Mapping) or item.get("tool_name") != tool_name:
                continue
            result = item.get("result")
            return dict(result) if isinstance(result, Mapping) else {}
        return {}

    @staticmethod
    def _tool(name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        return {"type": "tool", "tool_name": name, "arguments": dict(arguments)}

    def _votes(self, context: Mapping[str, Any]) -> tuple[Dict[str, float], Dict[str, list[str]]]:
        votes: Dict[str, float] = defaultdict(float)
        refs: Dict[str, list[str]] = defaultdict(list)

        attributes = self._result(context, "catalog.get_product_attributes")
        attribute_category = _category(attributes.get("selected_category"))
        if attribute_category:
            votes[attribute_category] += 3.0
            refs[attribute_category].extend(str(item) for item in attributes.get("evidence_refs") or [])

        history = self._result(context, "history.search_approved_cases")
        for item in history.get("entries") or []:
            if not isinstance(item, Mapping):
                continue
            category = _category(item.get("resolved_category"))
            if not category:
                continue
            votes[category] += 4.0
            entry_id = str(item.get("entry_id") or "")
            if entry_id:
                refs[category].append(f"approved-case:{entry_id}")

        documents = self._result(context, "documents.search_product_documents")
        for hit in documents.get("hits") or []:
            if not isinstance(hit, Mapping):
                continue
            record = hit.get("record")
            if not isinstance(record, Mapping):
                continue
            category = _category(dict(record.get("attributes") or {}).get("product_line"))
            if not category:
                continue
            authority = str(record.get("authority") or "agent_observation")
            votes[category] += _DOCUMENT_AUTHORITY_WEIGHT.get(authority, 0.5)
            evidence_id = str(record.get("evidence_id") or "")
            if evidence_id:
                refs[category].append(evidence_id)

        similar = self._result(context, "catalog.search_similar_products")
        for item in similar.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            category = _category(item.get("product_line"))
            if not category:
                continue
            similarity = float(item.get("similarity") or 0.0)
            votes[category] += min(2.0, max(0.0, similarity * 2.0))
            evidence_ref = str(item.get("evidence_ref") or "")
            if evidence_ref:
                refs[category].append(evidence_ref)

        return dict(votes), dict(refs)

    @staticmethod
    def _clear_candidate(votes: Mapping[str, float]) -> Optional[str]:
        ranked = sorted(votes.items(), key=lambda item: item[1], reverse=True)
        if not ranked or ranked[0][1] < 4.0:
            return None
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        if ranked[0][1] - runner_up < 2.0:
            return None
        return ranked[0][0]

    def __call__(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        investigation = dict(context.get("investigation") or {})
        pn = str(investigation.get("pn") or "").strip()
        subject_ref = str(investigation.get("subject_ref") or "").strip()
        family_ref = str(investigation.get("family_ref") or "").strip() or None
        data_version = str(investigation.get("data_version") or "").strip() or None
        document_source_version = (
            str(investigation.get("document_source_version") or "").strip() or None
        )
        query = str(investigation.get("query") or pn).strip()
        family_categories = list(investigation.get("family_categories") or [])
        if not pn or not subject_ref:
            return {
                "type": "complete",
                "candidate_category": None,
                "recommended_action": "retain_current_hold",
                "stop_reason": "missing_investigation_identity",
                "evidence_refs": [],
                "counter_evidence_refs": [],
                "unresolved_codes": ["missing_investigation_identity"],
            }

        seen = self._seen(context)
        if "mapping.get_candidates" not in seen:
            return self._tool(
                "mapping.get_candidates",
                {"pn": pn, "family_categories": family_categories},
            )
        if "catalog.get_product_attributes" not in seen:
            return self._tool("catalog.get_product_attributes", {"pn": pn})

        verification = dict(
            self._result(context, "mapping.get_candidates").get("verification") or {}
        )
        signal_codes = {
            str(item.get("code") or "")
            for item in verification.get("signals") or []
            if isinstance(item, Mapping)
        } | {str(item) for item in context.get("observed_signal_codes") or []}
        if (
            {"cross_source_mapping_conflict", "intra_source_rule_conflict"} & signal_codes
            and "price_data.compare_sources" not in seen
        ):
            return self._tool("price_data.compare_sources", {"pn": pn})

        if "history.search_approved_cases" not in seen:
            history_args: Dict[str, Any] = {"subject_ref": subject_ref}
            if family_ref:
                history_args["family_ref"] = family_ref
            if data_version:
                history_args["data_version"] = data_version
            return self._tool("history.search_approved_cases", history_args)

        votes, refs = self._votes(context)
        candidate = self._clear_candidate(votes)
        if candidate:
            counter_refs = [ref for category, values in refs.items() if category != candidate for ref in values]
            if "catalog.search_similar_products" in seen:
                stop_reason = "combined_evidence_sufficient"
            elif "documents.search_product_documents" in seen:
                stop_reason = "document_evidence_sufficient"
            else:
                stop_reason = "approved_case_and_current_attributes_agree"
            return {
                "type": "complete",
                "candidate_category": candidate,
                "recommended_action": "use_candidate_for_current_request",
                "stop_reason": stop_reason,
                "evidence_refs": list(dict.fromkeys(refs.get(candidate, []))),
                "counter_evidence_refs": list(dict.fromkeys(counter_refs)),
                "unresolved_codes": sorted(signal_codes),
            }

        if "documents.search_product_documents" not in seen:
            document_args: Dict[str, Any] = {
                "subject_ref": subject_ref,
                "query": query,
                "limit": 8,
            }
            if family_ref:
                document_args["family_ref"] = family_ref
            if document_source_version:
                document_args["source_version"] = document_source_version
            return self._tool("documents.search_product_documents", document_args)

        votes, refs = self._votes(context)
        candidate = self._clear_candidate(votes)
        if candidate:
            counter_refs = [ref for category, values in refs.items() if category != candidate for ref in values]
            return {
                "type": "complete",
                "candidate_category": candidate,
                "recommended_action": "use_candidate_for_current_request",
                "stop_reason": "document_evidence_sufficient",
                "evidence_refs": list(dict.fromkeys(refs.get(candidate, []))),
                "counter_evidence_refs": list(dict.fromkeys(counter_refs)),
                "unresolved_codes": sorted(signal_codes),
            }

        if "catalog.search_similar_products" not in seen:
            return self._tool("catalog.search_similar_products", {"pn": pn, "limit": 8})

        votes, refs = self._votes(context)
        candidate = self._clear_candidate(votes)
        if candidate:
            counter_refs = [ref for category, values in refs.items() if category != candidate for ref in values]
            return {
                "type": "complete",
                "candidate_category": candidate,
                "recommended_action": "use_candidate_for_current_request",
                "stop_reason": "combined_evidence_sufficient",
                "evidence_refs": list(dict.fromkeys(refs.get(candidate, []))),
                "counter_evidence_refs": list(dict.fromkeys(counter_refs)),
                "unresolved_codes": sorted(signal_codes),
            }

        all_refs = [ref for values in refs.values() for ref in values]
        return {
            "type": "complete",
            "candidate_category": None,
            "recommended_action": "retain_current_hold",
            "stop_reason": "insufficient_or_conflicting_evidence",
            "evidence_refs": [],
            "counter_evidence_refs": list(dict.fromkeys(all_refs)),
            "unresolved_codes": sorted(signal_codes | {"insufficient_evidence"}),
        }
