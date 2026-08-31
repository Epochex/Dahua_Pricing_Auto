from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd

from backend.engine.core.classifier import (
    MappingRuleMatch,
    collect_mapping_matches,
    detect_strong_model_evidence,
)


STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_BLOCK = "BLOCK"

_SEVERITY_RANK = {"warning": 1, "hard": 2}


def _category(value: Any) -> str:
    return str(value or "").strip().upper() or "UNKNOWN"


@dataclass(frozen=True)
class MappingVerificationSignal:
    code: str
    severity: str
    message: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError("severity must be warning or hard")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MappingVerificationResult:
    status: str
    selected_category: str
    selected_price_group: Optional[str]
    data_version: Optional[str]
    signals: tuple[MappingVerificationSignal, ...]
    matches_by_source: Dict[str, tuple[MappingRuleMatch, ...]]
    recommended_action: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "selected_category": self.selected_category,
            "selected_price_group": self.selected_price_group,
            "data_version": self.data_version,
            "signals": [item.to_dict() for item in self.signals],
            "matches_by_source": {
                source: [item.to_dict() for item in matches]
                for source, matches in self.matches_by_source.items()
            },
            "recommended_action": self.recommended_action,
        }


class MappingVerifier:
    """Independent, deterministic verifier for one product-line decision.

    The verifier flags unknowns, conflicting decision-table matches, strong
    model-prefix contradictions, and family outliers.  It never chooses a new
    category and never mutates a mapping table.
    """

    def __init__(self, *, family_min_size: int = 3, family_majority_ratio: float = 0.8):
        if family_min_size < 3:
            raise ValueError("family_min_size must be at least 3")
        if not 0.5 < family_majority_ratio <= 1.0:
            raise ValueError("family_majority_ratio must be in (0.5, 1.0]")
        self.family_min_size = int(family_min_size)
        self.family_majority_ratio = float(family_majority_ratio)

    @staticmethod
    def _matches(row: Optional[pd.Series], mapping: pd.DataFrame) -> tuple[MappingRuleMatch, ...]:
        if row is None:
            return ()
        return tuple(collect_mapping_matches(row, mapping))

    @staticmethod
    def _rule_refs(source: str, matches: Iterable[MappingRuleMatch]) -> tuple[str, ...]:
        return tuple(f"mapping-rule:{source}:{item.rule_id}" for item in matches)

    def verify(
        self,
        *,
        selected_category: str,
        selected_price_group: Optional[str],
        country_row: Optional[pd.Series],
        system_row: Optional[pd.Series],
        country_mapping: pd.DataFrame,
        system_mapping: pd.DataFrame,
        family_categories: Sequence[str] = (),
        data_version: Optional[str] = None,
    ) -> MappingVerificationResult:
        selected = _category(selected_category)
        country_matches = self._matches(country_row, country_mapping)
        system_matches = self._matches(system_row, system_mapping)
        matches_by_source = {
            "country": country_matches,
            "system": system_matches,
        }
        signals: List[MappingVerificationSignal] = []

        if selected == "UNKNOWN":
            signals.append(
                MappingVerificationSignal(
                    code="missing_mapping",
                    severity="hard",
                    message="No product-line mapping was selected.",
                )
            )

        for source, matches in matches_by_source.items():
            categories = {_category(item.category) for item in matches if _category(item.category) != "UNKNOWN"}
            if len(categories) > 1:
                signals.append(
                    MappingVerificationSignal(
                        code="intra_source_rule_conflict",
                        severity="warning",
                        message=f"{source} mapping contains matches for multiple product lines.",
                        evidence_refs=self._rule_refs(source, matches),
                    )
                )

        country_top = _category(country_matches[0].category) if country_matches else None
        system_top = _category(system_matches[0].category) if system_matches else None
        if country_top and system_top and country_top != system_top:
            signals.append(
                MappingVerificationSignal(
                    code="cross_source_mapping_conflict",
                    severity="warning",
                    message="The top mapping result differs across price-data sources.",
                    evidence_refs=(
                        f"mapping-rule:country:{country_matches[0].rule_id}",
                        f"mapping-rule:system:{system_matches[0].rule_id}",
                    ),
                )
            )

        strong_model_evidence = detect_strong_model_evidence(country_row, system_row)
        if strong_model_evidence is not None:
            expected_category = _category(strong_model_evidence[0])
            if selected != "UNKNOWN" and selected != expected_category:
                signals.append(
                    MappingVerificationSignal(
                        code="strong_model_evidence_conflict",
                        severity="hard",
                        message="The selected product line conflicts with deterministic model evidence.",
                        evidence_refs=(f"model-evidence:{expected_category}",),
                    )
                )

        matched_categories = {
            _category(item.category)
            for matches in matches_by_source.values()
            for item in matches
            if _category(item.category) != "UNKNOWN"
        }
        if (
            selected != "UNKNOWN"
            and matched_categories
            and selected not in matched_categories
            and (strong_model_evidence is None or selected != _category(strong_model_evidence[0]))
        ):
            signals.append(
                MappingVerificationSignal(
                    code="selected_category_has_no_supporting_rule",
                    severity="warning",
                    message="The selected product line is not supported by any matched rule.",
                )
            )

        if selected != "UNKNOWN" and not matched_categories and strong_model_evidence is None:
            signals.append(
                MappingVerificationSignal(
                    code="unverifiable_mapping",
                    severity="warning",
                    message="The selected product line has no rule or strong model evidence.",
                )
            )

        peers = [_category(item) for item in family_categories if _category(item) != "UNKNOWN"]
        if len(peers) >= self.family_min_size and selected != "UNKNOWN":
            counts = Counter(peers)
            majority_category, majority_count = counts.most_common(1)[0]
            majority_ratio = majority_count / len(peers)
            if majority_ratio >= self.family_majority_ratio and selected != majority_category:
                signals.append(
                    MappingVerificationSignal(
                        code="family_category_outlier",
                        severity="warning",
                        message=(
                            f"The selected product line differs from the {majority_category} "
                            f"family majority ({majority_count}/{len(peers)})."
                        ),
                        evidence_refs=(f"product-family-majority:{majority_category}",),
                    )
                )

        highest = max((_SEVERITY_RANK[item.severity] for item in signals), default=0)
        if highest >= _SEVERITY_RANK["hard"]:
            status = STATUS_BLOCK
            action = "hold_for_human_triage"
        elif highest:
            status = STATUS_WARN
            action = "request_human_triage"
        else:
            status = STATUS_PASS
            action = "continue_deterministic_pricing"

        return MappingVerificationResult(
            status=status,
            selected_category=selected,
            selected_price_group=str(selected_price_group).strip() if selected_price_group else None,
            data_version=str(data_version).strip() if data_version else None,
            signals=tuple(signals),
            matches_by_source=matches_by_source,
            recommended_action=action,
        )
